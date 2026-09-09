"""``dataset_sync`` 执行器 —— 数据集驱动统一同步框架(issue #392)。

框架只实现**一次**的横切关注点(全部数据集共用,SyncSpec 不各自手写):

* 进度上报 —— ``dataset_sync:start`` → 逐切片 ``dataset_sync:<dataset>[:<键>]``
  → ``dataset_sync:done``;done/total 为切片计数(逐标的数据集依赖 profiles
  解析 symbol 池时 total 随新切片被发现而递增,#188 同精神);
* #383 job timing —— worker 通用层在 ``_execute_with_heart`` 外统一测量,
  本执行器零额外计时(与全部 kind 同口径);
* 行级质量口径 —— 按 :class:`SyncSpec.shape` 推导的 :class:`RowPolicy` 经
  ``SliceQuery.row_policy`` 透传给 provider(#389 固化:全市场枚举=单行跳过
  +具名告警 ``tushare.dirty_row_skipped``,全脏行整批拒;按 symbol 精确查询
  =整批拒);
* TushareBudget 共享 —— provider 工厂构造 ``TushareResearchDataProvider``,
  其内部经 ``shared_tushare_budget`` 进程内单例限流(RPM 200 / 日 10 万);
* 批次记账 —— 经 ``ResearchDataSyncService`` 写 ``research_sync_batches``
  (accepted_rows / status 可定位执行器死点);上游失败经
  ``record_upstream_failure`` 固化死点切片(与旧路径同形,golden 锁定);
* 断点续跑 —— 切片 dataset_version 确定性,已发布切片同 version 重跑被
  服务层跳过。

scope 四元组(exchange / listing_boards / instrument_type / symbols,#385
语义,与 bulk_download 共享同一解析):``symbols`` 显式声明 > 宇宙过滤
(exchange/boards/type 命中 ``instruments`` 表)> profiles 同步结果(未声明
symbols 且选中 profiles 时,旧路径口径)> 空池(逐标的数据集 fail-visible)。

错误映射(与旧路径一致):预算耗尽 → ``tushare_budget_exhausted``(retryable);
上游数据错误 → ``research_data_upstream``(配置错误 fail-fast,其余 retryable);
payload 非法 → ``invalid_payload``;symbol 池解析为空 → ``empty_symbol_pool``。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_backtest.background_jobs.dataset_sync.scope import (
    ScopeValueError,
    SyncScope,
    normalize_sync_scope,
)
from finboard_backtest.background_jobs.dataset_sync.spec import (
    SYNC_SPECS,
    PersistContext,
    SliceQuery,
    SyncSpec,
    UnknownDatasetError,
    slices_for_spec,
)
from finboard_backtest.background_jobs.payload_contracts import (
    PayloadContractError,
    validate_job_payload,
)
from finboard_data.research import ResearchDataProvider

if TYPE_CHECKING:
    from finboard_backtest.background_jobs.executors._providers import SettingsFactory
    from finboard_persistence.research_repo import ResearchDataset

logger = structlog.get_logger(__name__)


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


def _select_datasets(raw: object) -> tuple[SyncSpec, ...]:
    """payload ``datasets`` → 注册表声明序的去重规格元组(缺省=全部)。"""

    if raw is None:
        return SYNC_SPECS.specs
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ExecutorError(
            code="invalid_payload",
            summary="datasets 必须是字符串列表",
            retryable=False,
        )
    unknown = sorted({item for item in raw if not SYNC_SPECS.has(item)})
    if unknown:
        raise UnknownDatasetError(unknown[0])
    by_name = {spec.name: spec for spec in SYNC_SPECS.specs}
    return tuple(by_name[name] for name in dict.fromkeys(raw))


def _phase(query: SliceQuery) -> str:
    """切片进度 phase:``dataset_sync:<dataset>[:<交易日|标的>]``。"""

    if query.trade_date is not None:
        return f"dataset_sync:{query.dataset}:{query.trade_date.isoformat()}"
    if query.symbol is not None:
        return f"dataset_sync:{query.dataset}:{query.symbol}"
    return f"dataset_sync:{query.dataset}"


def _research_dataset_for(name: str) -> ResearchDataset | None:
    """数据集名 → 批次记账用的 ResearchDataset 枚举;主数据集返回 None。"""

    from finboard_persistence.research_repo import ResearchDataset

    return {
        "profiles": ResearchDataset.INSTRUMENT_PROFILES,
        "daily_metrics": ResearchDataset.DAILY_METRICS,
        "financial_indicators": ResearchDataset.FINANCIAL_INDICATORS,
        "industry_memberships": ResearchDataset.INDUSTRY_MEMBERSHIPS,
    }.get(name)


async def _resolve_pool_from_instruments(
    session_maker: async_sessionmaker[AsyncSession], scope: SyncScope
) -> tuple[str, ...]:
    """按 scope 宇宙过滤(exchange / listing_boards / instrument_type)从
    ``instruments`` 表解析逐标的同步池(与 bulk_download 同一 list_active
    过滤语义,#385 口径)。"""

    from finboard_persistence import InstrumentRepository

    async with session_maker() as session:
        instruments, _ = await InstrumentRepository(session).list_active(
            exchange=scope.exchange,
            listing_boards=list(scope.listing_boards),
            instrument_type=scope.instrument_type,
            limit=999999,
        )
        await session.commit()
    return tuple(ins.code for ins in instruments)


class DatasetSyncExecutor:
    """``kind=dataset_sync`` 执行器:按 SyncSpec 注册表编排数据集同步。"""

    KIND = "dataset_sync"

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        settings_factory: SettingsFactory | None = None,
        provider_factory: Callable[[], ResearchDataProvider] | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._settings_factory = settings_factory
        self._provider_factory = provider_factory or _tushare_provider_factory(
            settings_factory
        )

    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        payload = job.payload
        # 入队期契约重放(#260 先例):覆盖旁路入队(直接写库 / 旧版本入队的
        # 存量行),未知键 / 坏日期执行期同样 fail-fast。
        try:
            validate_job_payload(job.kind, payload)
        except PayloadContractError as exc:
            raise ExecutorError(
                code="invalid_payload",
                summary=exc.summary,
                retryable=False,
            ) from exc

        try:
            selected = _select_datasets(payload.get("datasets"))
        except UnknownDatasetError as exc:
            raise ExecutorError(
                code="invalid_payload",
                summary=(
                    f"不支持的 datasets: {exc.args[0]!r};可用: "
                    f"{sorted(SYNC_SPECS.names)}"
                ),
                retryable=False,
            ) from exc
        start_date = _parse_date(payload.get("start_date"), "start_date")
        end_date = _parse_date(payload.get("end_date"), "end_date")
        if start_date > end_date:
            raise ExecutorError(
                code="invalid_payload",
                summary=f"start_date({start_date}) 不能晚于 end_date({end_date})",
                retryable=False,
            )
        try:
            scope = normalize_sync_scope(
                exchange=payload.get("exchange"),
                listing_boards=payload.get("listing_boards"),
                instrument_type=payload.get("instrument_type"),
                symbols=payload.get("symbols"),
            )
        except ScopeValueError as exc:
            raise ExecutorError(
                code="invalid_payload",
                summary=str(exc),
                retryable=False,
            ) from exc

        # 延迟导入:executors 包 __init__ 反向 re-export 本执行器,模块级
        # import 会形成 dataset_sync ↔ executors 循环(#392)。
        from finboard_backtest.background_jobs.executors._runtime import code_version
        from finboard_data.research import ResearchDataError
        from finboard_data.tushare_budget import TushareRequestLimitError
        from finboard_persistence.research_sync import ResearchDataSyncService

        provider = self._provider_factory()
        service = ResearchDataSyncService(self._session_maker)
        source = "tushare"
        code = code_version()

        await progress(0, None, "dataset_sync:start")

        # 逐标的同步池(scope 四元组框架级语义,见模块 docstring)。
        per_symbol_names = [spec.name for spec in selected if spec.is_per_symbol]
        symbol_pool = scope.symbols
        if not symbol_pool and scope.has_universe_filters:
            symbol_pool = await _resolve_pool_from_instruments(
                self._session_maker, scope
            )
            if not symbol_pool and per_symbol_names:
                raise ExecutorError(
                    code="no_instruments",
                    summary=(
                        "scope 宇宙过滤(exchange / listing_boards / "
                        f"instrument_type = {scope.exchange or '-'} / "
                        f"{list(scope.listing_boards) or '-'} / "
                        f"{scope.instrument_type or '-'})在 instruments 表"
                        "解析为空 —— 请先同步标的(data_sync),或放宽过滤条件"
                    ),
                    retryable=False,
                    context={"job_id": job.job_id},
                )
        # profiles 结果兜底池:仅当 payload 未声明 symbols / 宇宙过滤且选中
        # profiles(旧路径口径);profiles 切片完成后回填。
        pool_pending_profiles = not symbol_pool and any(
            spec.name == "profiles" for spec in selected
        )

        current_dataset: ResearchDataset | None = None
        current_version = ""
        done = 0
        try:
            for spec in selected:
                queries = slices_for_spec(
                    spec,
                    start_date=start_date,
                    end_date=end_date,
                    symbol_pool=symbol_pool,
                )
                if spec.is_per_symbol and not queries:
                    # 旧路径同位守卫:在无标的迭代的段落前 fail-visible,
                    # 不留「成功」假象(profiles 返回空池 / 全空字符串)。
                    if per_symbol_names:
                        raise ExecutorError(
                            code="empty_symbol_pool",
                            summary=(
                                f"逐标的数据集 {per_symbol_names} 的 symbol 池"
                                "解析为空(payload.symbols 未提供或为空、scope "
                                "宇宙过滤未命中、profiles 同步也未解析出任何"
                                "标的)—— 请检查 scope 输入或上游 profiles 数据"
                                "后重新入队"
                            ),
                            retryable=False,
                            context={"job_id": job.job_id},
                        )
                    continue
                for query in queries:
                    dataset = _research_dataset_for(spec.name)
                    if dataset is not None:
                        current_dataset = dataset
                    current_version = spec.slice_version(query)
                    await progress(done, None, _phase(query))
                    records = await spec.fetch(provider, query)
                    if not records:
                        # 空切片(非交易日 / 上游空响应):不产生批次行;进度
                        # 统一推进保证 done 可达 total(旧路径空切片跳过计数,
                        # 见 PR 偏差说明)。
                        done += 1
                        continue
                    if spec.name == "profiles" and pool_pending_profiles:
                        symbol_pool = tuple(
                            dict.fromkeys(
                                item.symbol
                                for item in records
                                if getattr(item, "list_status", "L") == "L"
                            )
                        )
                        pool_pending_profiles = False
                    persist_context = PersistContext(
                        session_maker=self._session_maker,
                        source=source,
                        code_version=code,
                        dataset_version=current_version,
                        query=query,
                    )
                    outcome = await spec.persist(persist_context, records)
                    logger.info(
                        "dataset_sync.slice_done",
                        dataset=spec.name,
                        dataset_version=current_version,
                        records=len(records),
                        accepted_rows=outcome.accepted_rows,
                    )
                    done += 1

            await progress(1, 1, "dataset_sync:done")
            return JobResult(status="succeeded", result_ref=None)
        except TushareRequestLimitError as exc:
            await service.record_upstream_failure(
                dataset=current_dataset or _fallback_batch_dataset(),
                source=source,
                dataset_version=current_version or "unknown",
                code_version=code,
                parameters={
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
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
                dataset=current_dataset or _fallback_batch_dataset(),
                source=source,
                dataset_version=current_version or "unknown",
                code_version=code,
                parameters={
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
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


def _fallback_batch_dataset() -> ResearchDataset:
    """错误发生在任何批次切片之前时的记账兜底(旧路径同款:profiles)。"""

    from finboard_persistence.research_repo import ResearchDataset

    return ResearchDataset.INSTRUMENT_PROFILES


def _default_settings_factory() -> object | None:
    from finboard_app.config import load_settings

    try:
        return load_settings()
    except Exception:
        return None


def _tushare_provider_factory(
    settings_factory: Callable[[], object | None] | None,
) -> Callable[[], ResearchDataProvider]:
    """默认 provider 工厂:按 settings 构造 ``TushareResearchDataProvider``。

    未装 SDK / 未配 token 时 provider 构造或首次调用即抛可操作的
    ``ResearchDataDependencyError`` / ``ResearchDataConfigurationError``,
    由 execute 映射为 fail-fast。限流经 provider 内部 ``shared_tushare_budget``
    进程内单例(RPM 200 / 日 10 万默认;与 bulk_download 的 tushare bar 源
    共享同一进程预算)。
    """

    def build() -> ResearchDataProvider:
        from finboard_data import TushareResearchDataProvider

        settings = (settings_factory or _default_settings_factory)()
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


_: type[JobExecutor] = DatasetSyncExecutor

__all__ = ["DatasetSyncExecutor"]
