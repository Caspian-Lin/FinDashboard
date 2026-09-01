"""``research_data_sync`` 执行器 —— research 数据表摄取编排(issue #171)。

按数据集 / 日期范围编排五类研究数据摄取(估值 / 财务 / 行业 / 名称历史)并
通过 ``ResearchDataSyncService`` 写库(质量门 + batch 发布语义):

* ``profiles`` —— ``fetch_instrument_profiles`` 全量一次;#251 起同时拉取
  退市档案(``list_status="D"``)合并进同一批次,补齐 delist_date 上游;
* ``name_changes``(#251)—— ``fetch_name_changes`` 全市场历史名称变更,
  直接重建主数据表 ``instrument_names``(半开区间),供 #213 ST-PIT 消费;
  不走 research_* 批次(名称历史是主数据衍生,无批次语义);
* ``daily_metrics`` —— 逐交易日 ``fetch_daily_metrics`` 截面(非交易日跳过);
* ``financial_indicators`` —— 逐标的 ``fetch_financial_indicators``(报告期范围);
* ``industry_memberships`` —— 逐标的 ``fetch_industry_memberships``。

断点续跑:每个切片使用确定性 ``dataset_version``(``daily:{trade_date}`` /
``financial:{symbol}:{start}:{end}`` / ``industry:{symbol}`` / ``profiles:{date}``),
已发布切片同 version 重跑被 ``ResearchDataSyncService`` 跳过;失败切片重跑
仅补齐未发布部分,不覆盖已发布数据。

限流:provider 内建 ``tushare_budget``(进程内共享限流锁);预算耗尽映射为
``retryable`` 失败。未装 tushare / 未配 token / 积分不足均 fail-fast,报错
信息可操作(指明缺什么、怎么配)。

边界:写 ``research_*`` 独立研究数据表与 ``instrument_names`` 主数据名称历史,
不进入实盘交易内核调度(#136 边界),不连 broker / 不下实盘单 / 不修改持仓。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_backtest.background_jobs.executors._runtime import code_version
from finboard_data.research import ResearchDataProvider

if TYPE_CHECKING:
    pass

#: 支持的数据集白名单(对应 ResearchDataset 枚举的摄取入口;#251 加 name_changes)。
SUPPORTED_DATASETS: frozenset[str] = frozenset(
    {
        "profiles",
        "name_changes",
        "daily_metrics",
        "financial_indicators",
        "industry_memberships",
    }
)
_DEFAULT_DATASETS: tuple[str, ...] = (
    "profiles",
    "name_changes",
    "daily_metrics",
    "financial_indicators",
    "industry_memberships",
)


def _parse_date(value: object, key: str) -> date:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ExecutorError(
                code="invalid_payload",
                summary=f"{key} 必须是 ISO 日期字符串(YYYY-MM-DD),收到: {value!r}",
                retryable=False,
            ) from exc
    if isinstance(value, date):
        return value
    raise ExecutorError(
        code="invalid_payload",
        summary=f"{key} 必须是 ISO 日期字符串(YYYY-MM-DD)",
        retryable=False,
    )


def _parse_symbols(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ExecutorError(
            code="invalid_payload",
            summary="symbols 必须是字符串列表",
            retryable=False,
        )
    return tuple(dict.fromkeys(item for item in value if item))


def _workdays(start: date, end: date) -> tuple[date, ...]:
    if start > end:
        raise ExecutorError(
            code="invalid_payload",
            summary=f"start_date({start}) 不能晚于 end_date({end})",
            retryable=False,
        )
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:  # 周一至周五;真实交易日由上游空响应跳过
            days.append(current)
        current += timedelta(days=1)
    return tuple(days)


class ResearchDataSyncExecutor:
    """``kind=research_data_sync`` 执行器。"""

    KIND = "research_data_sync"

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        settings_factory: Callable[[], object | None] | None = None,
        provider_factory: Callable[[], ResearchDataProvider] | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._settings_factory = settings_factory or _default_settings_factory
        self._provider_factory = provider_factory or _tushare_provider_factory(
            self._settings_factory
        )

    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        payload = job.payload
        raw_datasets = payload.get("datasets")
        if raw_datasets is None:
            datasets = _DEFAULT_DATASETS
        elif isinstance(raw_datasets, list) and all(
            isinstance(item, str) for item in raw_datasets
        ):
            unknown = sorted(set(raw_datasets) - SUPPORTED_DATASETS)
            if unknown:
                raise ExecutorError(
                    code="invalid_payload",
                    summary=(
                        f"不支持的 datasets: {unknown};"
                        f" 可用: {sorted(SUPPORTED_DATASETS)}"
                    ),
                    retryable=False,
                )
            datasets = tuple(dict.fromkeys(raw_datasets))
        else:
            raise ExecutorError(
                code="invalid_payload",
                summary="datasets 必须是字符串列表",
                retryable=False,
            )
        start_date = _parse_date(payload.get("start_date"), "start_date")
        end_date = _parse_date(payload.get("end_date"), "end_date")
        symbols = _parse_symbols(payload.get("symbols"))
        if start_date > end_date:
            raise ExecutorError(
                code="invalid_payload",
                summary=f"start_date({start_date}) 不能晚于 end_date({end_date})",
                retryable=False,
            )

        from finboard_data.research import ResearchDataError
        from finboard_data.tushare_budget import TushareRequestLimitError
        from finboard_persistence.research_repo import ResearchDataset
        from finboard_persistence.research_sync import ResearchDataSyncService

        provider = self._provider_factory()
        service = ResearchDataSyncService(self._session_maker)
        source = "tushare"
        code = code_version()

        await progress(0, None, "research_data_sync:start")

        # 逐标的接口的 symbol 池:payload 未指定时以 profiles 全市场为池。
        resolved_symbols = symbols
        current_dataset = ResearchDataset.INSTRUMENT_PROFILES
        current_version = ""
        try:
            total = 0
            if "profiles" in datasets:
                total += 1
            if "name_changes" in datasets:
                total += 1
            if "daily_metrics" in datasets:
                total += len(_workdays(start_date, end_date))
            if "financial_indicators" in datasets:
                total += len(resolved_symbols)
            if "industry_memberships" in datasets:
                total += len(resolved_symbols)
            done = 0

            if "profiles" in datasets:
                current_dataset = ResearchDataset.INSTRUMENT_PROFILES
                current_version = f"profiles:{date.today().isoformat()}"
                await progress(done, total, "research_data_sync:profiles")
                # #251:在市(L)+ 退市(D)档案合并进同一批次 —— 退市档案携带
                # delist_date,是 instruments 主数据退市日期的唯一结构化上游。
                profile_records = await provider.fetch_instrument_profiles()
                delisted_records = await provider.fetch_instrument_profiles(
                    list_status="D"
                )
                await service.sync_instrument_profiles(
                    source=source,
                    dataset_version=current_version,
                    code_version=code,
                    parameters={"list_status": "L+D"},
                    raw_payload=None,
                    records=[*profile_records, *delisted_records],
                )
                if not resolved_symbols:
                    resolved_symbols = tuple(
                        dict.fromkeys(item.symbol for item in profile_records)
                    )
                done += 1

            if "name_changes" in datasets:
                # #251:历史名称变更重建 instrument_names(主数据表,无批次语义)。
                # 失败映射:上游错误经 ResearchDataError 兜底记录时
                # current_dataset 沿用上一个切片值 —— 名称历史失败审计以
                # current_version 的 ``name_changes:`` 前缀区分。
                current_version = f"name_changes:{date.today().isoformat()}"
                await progress(done, total, "research_data_sync:name_changes")
                from finboard_persistence import InstrumentRepository

                name_changes = await provider.fetch_name_changes()
                if name_changes:
                    async with self._session_maker() as session:
                        await InstrumentRepository(
                            session
                        ).import_name_history(
                            [
                                (
                                    change.symbol,
                                    change.name,
                                    change.start_date,
                                    change.end_date,
                                )
                                for change in name_changes
                            ]
                        )
                        await session.commit()
                done += 1

            if "daily_metrics" in datasets:
                current_dataset = ResearchDataset.DAILY_METRICS
                for day in _workdays(start_date, end_date):
                    current_version = f"daily:{day.isoformat()}"
                    await progress(done, total, f"research_data_sync:daily:{day}")
                    daily_records = await provider.fetch_daily_metrics(
                        trade_date=day
                    )
                    if not daily_records:
                        continue  # 非交易日(节假日),上游返回空
                    await service.sync_daily_metrics(
                        source=source,
                        dataset_version=current_version,
                        code_version=code,
                        parameters={"trade_date": day.isoformat()},
                        raw_payload=None,
                        records=list(daily_records),
                        expected_trade_date=day,
                    )
                    done += 1

            if "financial_indicators" in datasets:
                current_dataset = ResearchDataset.FINANCIAL_INDICATORS
                for symbol in resolved_symbols:
                    current_version = (
                        f"financial:{symbol}:{start_date.isoformat()}:"
                        f"{end_date.isoformat()}"
                    )
                    await progress(
                        done, total, f"research_data_sync:financial:{symbol}"
                    )
                    financial_records = await provider.fetch_financial_indicators(
                        symbol,
                        start_period=start_date,
                        end_period=end_date,
                    )
                    if not financial_records:
                        continue
                    await service.sync_financial_indicators(
                        source=source,
                        dataset_version=current_version,
                        code_version=code,
                        parameters={
                            "symbol": symbol,
                            "start_period": start_date.isoformat(),
                            "end_period": end_date.isoformat(),
                        },
                        raw_payload=None,
                        records=list(financial_records),
                    )
                    done += 1

            if "industry_memberships" in datasets:
                current_dataset = ResearchDataset.INDUSTRY_MEMBERSHIPS
                for symbol in resolved_symbols:
                    current_version = f"industry:{symbol}"
                    await progress(
                        done, total, f"research_data_sync:industry:{symbol}"
                    )
                    industry_records = await provider.fetch_industry_memberships(
                        symbol=symbol,
                        current_only=True,
                    )
                    if not industry_records:
                        continue
                    await service.sync_industry_memberships(
                        source=source,
                        dataset_version=current_version,
                        code_version=code,
                        parameters={"symbol": symbol, "current_only": True},
                        raw_payload=None,
                        records=list(industry_records),
                    )
                    done += 1

            await progress(1, 1, "research_data_sync:done")
            return JobResult(status="succeeded", result_ref=None)
        except TushareRequestLimitError as exc:
            await service.record_upstream_failure(
                dataset=current_dataset,
                source=source,
                dataset_version=current_version or "unknown",
                code_version=code,
                parameters={"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
                error_type="TushareRequestLimit",
            )
            raise ExecutorError(
                code="tushare_budget_exhausted",
                summary=f"Tushare 每日请求预算耗尽,任务将退避重试: {exc}",
                retryable=True,
                context={"job_id": job.job_id},
            ) from exc
        except ResearchDataError as exc:
            await service.record_upstream_failure(
                dataset=current_dataset,
                source=source,
                dataset_version=current_version or "unknown",
                code_version=code,
                parameters={"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
                error_type=type(exc).__name__,
            )
            raise ExecutorError(
                code="research_data_upstream",
                summary=str(exc) or type(exc).__name__,
                retryable=type(exc).__name__ != "ResearchDataConfigurationError",
                context={"job_id": job.job_id},
            ) from exc
        except ExecutorError:
            raise
        except Exception as exc:
            raise ExecutorError(
                code=type(exc).__name__,
                summary=str(exc)[:1000] or type(exc).__name__,
                retryable=False,
                context={"job_id": job.job_id},
            ) from exc


def _default_settings_factory() -> object | None:
    from finboard_app.config import load_settings

    try:
        return load_settings()
    except Exception:
        return None


def _tushare_provider_factory(
    settings_factory: Callable[[], object | None],
) -> Callable[[], ResearchDataProvider]:
    """默认 provider 工厂:按 settings 构造 ``TushareResearchDataProvider``。

    未装 SDK / 未配 token 时 provider 构造或首次调用即抛可操作的
    ``ResearchDataDependencyError`` / ``ResearchDataConfigurationError``,
    由 execute 映射为 fail-fast。
    """

    def build() -> ResearchDataProvider:
        from finboard_data import TushareResearchDataProvider

        settings = settings_factory()
        return TushareResearchDataProvider(
            token=getattr(settings, "tushare_token", None) or None,
            requests_per_minute=int(
                getattr(settings, "tushare_requests_per_minute", 200) or 200
            ),
            daily_request_limit=int(
                getattr(settings, "tushare_daily_request_limit", 100_000) or 100_000
            ),
            usage_file=getattr(
                settings, "tushare_usage_file", "data_cache/tushare_usage.json"
            )
            or "data_cache/tushare_usage.json",
        )

    return build


_: type[JobExecutor] = ResearchDataSyncExecutor

__all__ = ["SUPPORTED_DATASETS", "ResearchDataSyncExecutor"]
