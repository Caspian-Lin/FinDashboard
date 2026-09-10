"""``finboard.data_write.*`` / ``finboard.etf.*`` 工具 —— 数据写操作(issue #137)。

把 #124 只读数据查询之外的数据准备能力暴露给外置 Agent(OpenCode),补全
「数据→因子→策略」闭环的第一步:agent 能拉 K 线 / 发布数据集 / 修复质量 /
同步 ETF 元数据 / 读写调度器配置。共 12 个工具,对应 13 个 REST 端点
(``GET /api/data/bulk-download/status`` 已随 #117 任务化下线,状态查询走
``finboard_job_get``)。

实现策略(复用现有 service / repository,不裸 SQL,不连 broker):

* **任务化长耗时工具(4)** —— ``data_sync`` / ``bulk_download`` /
  ``quality_repair`` / ``dataset_publish`` 复用 ``BackgroundJobRepository.create_or_get``
  登记 ``queued`` 任务并立即返回 202 + ``job_id``,与 REST 语义端点口径一致
  (idempotency_key 公式相同 → agent 与 REST 提交同一任务命中同一 job_id)。
  进度 / 状态 / 取消统一走 ``finboard_job_get`` / ``finboard_job_cancel``(#136)。
* **同步短任务(1)** —— ``data_fetch`` 拉单个标的(主源失败 fallback),直接写
  ``ParquetCache``,不进队列(与 REST 一致)。
* **配置读写(2)** —— ``config_get``(只读,读 ``data_config.json``)/
  ``config_update``(写,合并字段)。
* **ETF 元数据闭环(4)** —— ``etf_sync``(默认 dry_run 预览)/ ``etf_batch_confirm``
  / ``etf_update``(人工覆盖,写审计流水)/ ``etf_review_queue``(只读)。

权限:写工具受 ``_require_write_enabled``(``mcp_readonly_only`` 开关)守卫;
``config_get`` / ``etf_review_queue`` 只读,自动允许。不触及交易安全红线
(不连 broker / 账户 / 订单 / 持仓)。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from sqlalchemy import select

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_persistence import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
    EtfMetadataModel,
    EtfMetadataRepository,
    InstrumentModel,
    ReleaseSymbolSourceError,
    resolve_release_symbols,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)

# 与 finboard_api.routes.data 一致:缓存目录 / 标的池 / 调度配置文件路径。
_CACHE_DIR = "data_cache"
_SYMBOLS_FILE = "symbols.yaml"
_CONFIG_FILE = "data_config.json"
_SUPPORTED_BAR_PROVIDERS = {"akshare", "tushare", "yfinance"}


async def _require_write_enabled(app: McpAppContext) -> None:
    """写操作前置检查:``mcp_readonly_only`` 开启时拒绝。"""

    if not app.write_tools_enabled:
        raise McpToolError(
            "permission_denied",
            "MCP server 处于只读模式(mcp_readonly_only=true),写操作不可用",
        )


def _payload_checksum(payload: dict[str, Any]) -> str:
    """计算 background_jobs payload checksum(与 routes/jobs.py 口径一致)。"""

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _job_out(row: Any) -> dict[str, Any]:
    """把 ``BackgroundJobModel`` 行序列化为 ``JobOut`` 兼容的 JSON dict。

    与 REST ``JobOut.model_validate(row)`` 口径一致(含时间戳),
    再经 ``to_jsonable`` 归一为 JSON 兼容结构。
    """

    from finboard_api.job_schemas import JobOut

    return cast(
        dict[str, Any],
        to_jsonable(JobOut.model_validate(row).model_dump(mode="json")),
    )


async def _enqueue_data_job(
    app: McpAppContext,
    *,
    kind: str,
    idempotency_key: str,
    payload: dict[str, Any],
    requested_by: str,
) -> dict[str, Any]:
    """登记一个 ``queued`` 数据任务,返回 ``JobOut`` dict + ``created`` 标记。

    复刻 ``finboard_api.job_helpers.enqueue_job`` 但去掉 ``Response`` 参数
    (MCP 无 HTTP 响应)。conflict → :class:`McpToolError` conflict。
    """

    from sqlalchemy.exc import IntegrityError

    checksum = _payload_checksum(payload)
    async with app.session_maker() as session:
        try:
            row, created = await BackgroundJobRepository(
                session
            ).create_or_get(
                job_id=generate_background_job_id(),
                idempotency_key=idempotency_key,
                kind=kind,
                queue="data",
                status=BackgroundJobStatus.QUEUED.value,
                priority=0,
                payload=payload,
                payload_checksum=checksum,
                max_attempts=3,
                requested_by=requested_by,
            )
            await session.commit()
        except BackgroundJobPersistenceConflictError as exc:
            await session.rollback()
            raise McpToolError("conflict", str(exc)) from exc
        except IntegrityError as exc:
            await session.rollback()
            raise McpToolError("conflict", "重复 idempotency_key") from exc
        result = _job_out(row)
        result["created"] = created
        return result


# --------------------------------------------------------------------------- #
# 行情源解析(复用 finboard_api.routes.data 口径,避免未知值静默落 yfinance)
# --------------------------------------------------------------------------- #


def _resolve_provider_name(
    source: str | None, *, settings: Any
) -> str:
    """解析并校验行情源,避免未知值静默落到 yfinance。"""

    configured = settings.data_provider
    provider_name = (
        (source or configured).strip().lower()
    )
    if provider_name not in _SUPPORTED_BAR_PROVIDERS:
        supported = ", ".join(sorted(_SUPPORTED_BAR_PROVIDERS))
        raise McpToolError(
            "invalid_argument",
            f"不支持的行情源: {provider_name}; 可用: {supported}",
        )
    return provider_name


def _fallback_provider_name(primary: str, *, settings: Any) -> str | None:
    """选择备用行情源;可用 settings.data_fallback_provider 覆盖。"""

    configured = (settings.data_fallback_provider or "").strip().lower()
    fallback = configured or ("yfinance" if primary == "akshare" else "akshare")
    if fallback not in _SUPPORTED_BAR_PROVIDERS or fallback == primary:
        return None
    return fallback


def _get_provider(source: str, *, settings: Any) -> Any:
    """构造行情 provider(复用 finboard_api.routes.data._get_provider 口径)。"""

    from finboard_data import AkShareProvider, TushareBarProvider, YFinanceProvider

    if source == "akshare":
        return AkShareProvider(use_cache=False)
    if source == "tushare":
        return TushareBarProvider(
            token=settings.tushare_token,
            use_cache=False,
            requests_per_minute=settings.tushare_requests_per_minute,
            daily_request_limit=settings.tushare_daily_request_limit,
            usage_file=settings.tushare_usage_file,
        )
    return YFinanceProvider(use_cache=False)


async def _store_fetched_bars(
    cache: Any,
    symbol: Any,
    period: Any,
    adjust: str,
    bars: list[Any],
    source: str,
) -> None:
    """写入行情并避免把不同数据源静默混进同一个可发布缓存。"""

    if not bars:
        return
    existing = await cache.read(symbol, period, adjust)
    known_sources = {bar.source for bar in existing if bar.source}
    if known_sources and known_sources != {source}:
        await cache.write(symbol, period, adjust, bars)
        return
    await cache.merge(
        symbol,
        period,
        adjust,
        bars,
        existing_bars=existing,
    )


async def _persist_tushare_lifecycle_events(
    session: Any, events: list[Any]
) -> int:
    """幂等写入 Tushare 停复牌事件,返回本次新增数量。

    实现收敛到 finboard_persistence 单一事实源(#393):与 bulk_download
    执行器 / REST fetch 共用同一行形状与幂等键。
    """

    from finboard_persistence import persist_tushare_lifecycle_events

    return await persist_tushare_lifecycle_events(session, events)


# --------------------------------------------------------------------------- #
# ETF 行映射(复用 finboard_api.routes.instruments._etf_to_out 字段清单)
# --------------------------------------------------------------------------- #


def _etf_row_to_dict(row: EtfMetadataModel) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "code": row.code,
                "fund_code": row.fund_code,
                "category": row.category,
                "execution_profile": row.execution_profile,
                "underlying_market": row.underlying_market,
                "strategy_type": row.strategy_type,
                "underlying_index": row.underlying_index,
                "underlying_asset_class": row.underlying_asset_class,
                "management_fee_rate": row.management_fee_rate,
                "custody_fee_rate": row.custody_fee_rate,
                "tracking_error": row.tracking_error,
                "inception_date": row.inception_date,
                "listing_date": row.listing_date,
                "delisting_date": row.delisting_date,
                "iopv_available": row.iopv_available,
                "allows_t_plus_0": row.allows_t_plus_0,
                "dividend_policy": row.dividend_policy,
                "source": row.source,
                "rule_version": row.rule_version,
                "confidence": row.confidence,
                "review_status": row.review_status,
                "evidence": [str(e) for e in (row.evidence or [])],
                "manual_override": row.manual_override,
            }
        ),
    )


def _config_out_dict(cfg: dict[str, Any], *, settings: Any) -> dict[str, Any]:
    """构造 SchedulerConfigOut 兼容 dict(data_provider 取自 settings)。"""

    return {
        "sync_enabled": cfg.get("sync_enabled", True),
        "sync_time": cfg.get("sync_time", "15:35"),
        "download_enabled": cfg.get("download_enabled", True),
        "download_time": cfg.get("download_time", "15:45"),
        "download_lookback_days": cfg.get("download_lookback_days", 5),
        "download_markets": cfg.get("download_markets", ["a_share"]),
        "download_types": cfg.get("download_types", ["stock", "etf"]),
        "data_provider": settings.data_provider,
    }


def _load_config_sync() -> dict[str, Any]:
    """同步读取 data_config.json(不存在返回空 dict)。在 to_thread 内调用。"""

    p = Path(_CONFIG_FILE)
    if p.exists():
        return cast(dict[str, Any], json.loads(p.read_text(encoding="utf-8")))
    return {}


def _save_config_sync(cfg: dict[str, Any]) -> None:
    """同步写入 data_config.json。在 to_thread 内调用。"""

    Path(_CONFIG_FILE).write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# 1. data_fetch(同步,单标的)
# --------------------------------------------------------------------------- #


async def data_fetch(
    app: McpAppContext,
    *,
    symbol: str,
    start: str,
    end: str,
    adjust: str = "qfq",
    source: str | None = None,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_data.cache import ParquetCache, make_symbol
        from finboard_shared.types import BarPeriod

        settings = app.settings
        primary_name = _resolve_provider_name(source, settings=settings)
        fallback_name = _fallback_provider_name(primary_name, settings=settings)
        sym = make_symbol(symbol)
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
        cache = ParquetCache(_CACHE_DIR)
        errors: list[str] = []
        actual_source: str | None = None
        actual_provider: Any | None = None
        bars: list[Any] = []

        for source_name in (primary_name, fallback_name):
            if source_name is None:
                continue
            provider = _get_provider(source_name, settings=settings)
            try:
                candidate = await provider.fetch_bars(
                    sym, BarPeriod.D1, start_date, end_date, adjust=adjust
                )
                if candidate:
                    bars = candidate
                    actual_source = source_name
                    actual_provider = provider
                    break
                errors.append(f"{source_name}: 返回空数据")
            except Exception as exc:  # 与 REST 一致:逐源容错,记录后继续 fallback
                errors.append(f"{source_name}: {exc}")

        if not bars or actual_source is None:
            detail = "; ".join(errors) or "所有行情源均未返回数据"
            raise McpToolError(
                "unavailable",
                f"数据拉取失败(主源及备用源均不可用): {detail}",
            )

        await _store_fetched_bars(
            cache, sym, BarPeriod.D1, adjust, bars, actual_source
        )

        lifecycle_events = 0
        lifecycle_sync_failed = False
        lifecycle_sync_error: str | None = None
        if actual_source == "tushare" and actual_provider is not None:
            async with app.session_maker() as session:
                try:
                    events = await actual_provider.fetch_suspension_events(
                        sym, start_date, end_date
                    )
                    lifecycle_events = await _persist_tushare_lifecycle_events(
                        session, events
                    )
                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    lifecycle_sync_failed = True
                    lifecycle_sync_error = type(exc).__name__

        used_fallback = actual_source != primary_name
        return {
            "symbol": symbol,
            "bar_count": len(bars),
            "first_date": str(bars[0].timestamp.date()) if bars else None,
            "last_date": str(bars[-1].timestamp.date()) if bars else None,
            "source": actual_source,
            "fallback_used": used_fallback,
            "fallback_source": actual_source if used_fallback else None,
            "lifecycle_events": lifecycle_events,
            "lifecycle_sync_failed": lifecycle_sync_failed,
            "lifecycle_sync_error": lifecycle_sync_error,
        }

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data.fetch",
        arguments={
            "symbol": symbol,
            "start": start,
            "end": end,
            "adjust": adjust,
            "source": source,
        },
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 2. data_sync_universe(任务化,kind=data_sync)
# --------------------------------------------------------------------------- #


async def data_sync_universe(app: McpAppContext) -> ToolEnvelope:
    """登记全市场标的同步任务,返回 202 + job_id(不等待执行)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        payload: dict[str, Any] = {"as_of": date.today().isoformat()}
        idempotency_key = f"data_sync:{date.today().isoformat()}"
        return await _enqueue_data_job(
            app,
            kind="data_sync",
            idempotency_key=idempotency_key,
            payload=payload,
            requested_by="mcp:data_sync",
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data.sync_universe",
        arguments={},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 4. data_bulk_download_start(任务化,kind=bulk_download)
# --------------------------------------------------------------------------- #


async def data_bulk_download_start(
    app: McpAppContext,
    *,
    market: str = "a_share",
    instrument_type: str | None = None,
    exchange: str | None = None,
    listing_boards: list[str] | None = None,
    start: str = "2015-01-01",
    source: str | None = None,
    symbols: list[str] | None = None,
) -> ToolEnvelope:
    """登记批量历史数据拉取任务,返回 202 + job_id(不等待执行)。

    ``symbols``(#347):可选子集重跑 —— 与 market/instrument_type/exchange/
    listing_boards 过滤叠加(交集为空执行器按 no_instruments 拒),失败清单
    可直接回填;入队期 payload 契约校验(#347,#260 风格)非法参数秒级
    ``invalid_argument``。
    """

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_backtest.background_jobs.payload_contracts import (
            PayloadContractError,
            validate_job_payload,
        )

        payload: dict[str, Any] = {
            "market": market,
            "source": source or "",
            "start": start,
            "instrument_type": instrument_type,
            "exchange": exchange,
            "listing_boards": list(listing_boards or []),
        }
        # symbols 只在显式提供时进入 payload:缺省 payload 与 #347 之前逐字节
        # 一致(payload checksum 稳定,同 idempotency_key 重提交不因新增键冲突)。
        symbols_digest = ""
        if symbols:
            deduped = list(dict.fromkeys(symbols))
            payload["symbols"] = deduped
            symbols_digest = hashlib.sha256(
                ",".join(deduped).encode("utf-8")
            ).hexdigest()[:16]
        # 入队期契约(#347):与 REST 语义化端点共用同一校验函数。
        try:
            validate_job_payload("bulk_download", payload)
        except PayloadContractError as exc:
            raise McpToolError(
                "invalid_argument",
                f"payload 契约校验失败[{exc.code}]: {exc.summary}",
            ) from exc
        idempotency_key = (
            f"bulk_download:{market}:{source or 'auto'}:{start}:"
            f"{instrument_type or 'all'}"
        )
        if symbols_digest:
            # 子集重跑的幂等键带 symbols 摘要:不同子集不互相命中旧任务。
            idempotency_key += f":sub:{symbols_digest}"
        return await _enqueue_data_job(
            app,
            kind="bulk_download",
            idempotency_key=idempotency_key,
            payload=payload,
            requested_by="mcp:bulk_download",
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data.bulk_download_start",
        arguments={
            "market": market,
            "instrument_type": instrument_type,
            "exchange": exchange,
            "listing_boards": listing_boards,
            "start": start,
            "source": source,
            "symbols": symbols,
        },
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 5. data_quality_repair(任务化,kind=quality_repair)
# --------------------------------------------------------------------------- #


async def data_quality_repair(
    app: McpAppContext,
    *,
    symbols: list[str],
    source: str = "akshare",
    adjust: str = "qfq",
) -> ToolEnvelope:
    """登记批量缓存异常 bar 修复任务,返回 202 + job_id(不等待执行)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        if not symbols:
            raise McpToolError("invalid_argument", "至少指定一个标的代码")
        deduped = list(dict.fromkeys(symbols))
        payload: dict[str, Any] = {
            "symbols": deduped,
            "source": source,
            "adjust": adjust,
        }
        symbols_digest = hashlib.sha256(
            ",".join(deduped).encode("utf-8")
        ).hexdigest()[:16]
        idempotency_key = (
            f"quality_repair:{symbols_digest}:{source}:{adjust}"
        )
        return await _enqueue_data_job(
            app,
            kind="quality_repair",
            idempotency_key=idempotency_key,
            payload=payload,
            requested_by="mcp:quality_repair",
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data.quality_repair",
        arguments={"symbols": symbols, "source": source, "adjust": adjust},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 6. dataset_release_publish(任务化 ⭐,kind=dataset_publish)
# --------------------------------------------------------------------------- #


async def dataset_release_publish(
    app: McpAppContext,
    *,
    release_id: str,
    symbols: list[str] | None = None,
    version: str,
    start_date: str,
    end_date: str,
    dataset_name: str = "multi_asset_daily_bars",
    release_kind: str = "a_share_tushare",
    source: str | None = None,
    adjustment: str = "qfq",
    required_capabilities: list[str] | None = None,
    symbols_from_release: str | None = None,
    full_market: bool = False,
    exchange: str | None = None,
    listing_boards: list[str] | None = None,
    consistency_baseline_release_id: str | None = None,
    consistency_fail_on_mismatch: bool = False,
) -> ToolEnvelope:
    """登记数据集冻结发布任务,返回 202 + job_id(研究闭环关键节点)。

    成功后 worker 把 ``result_ref = release_id``;agent 用 ``finboard_job_get``
    轮询拿到 release_id 后再查发布详情(``finboard_dataset_release_get``)。

    ``symbols`` / ``symbols_from_release`` / ``full_market`` 三选一(#261):
    后两者在入队期解析成具体 symbols 进任务 payload(来源发布须可用;
    full_market 按 kind 语义展开),执行器零改动;来源缺失 / 不可用 /
    展开为空入队即 ``invalid_argument`` 具名拒绝。

    ``exchange`` / ``listing_boards``(#385,与 bulk_download 同词表)只对
    ``full_market`` 展开生效(exchange 实际取值 SSE/SZSE/BSE/CFFEX;
    listing_board 实际取值 sse_main/szse_main/star/chinext/bse/cdr,
    ETF/指数/转债恒为 unknown,过滤后想保留它们须显式含 unknown);
    与 symbols / symbols_from_release 混用入队即 ``invalid_argument``。

    ``consistency_baseline_release_id``(#252):指定基线发布(如 bars 主发布)
    做标的集一致性校验,差集具名;默认只 warning,``fail_on_mismatch`` 时秒级
    失败(code=symbol_set_mismatch),避免并集不一致拖到执行期才暴露。
    """

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_api.schemas import ResearchDatasetReleaseCreate

        normalized_symbols = [
            s.strip().upper() for s in (symbols or []) if s.strip()
        ] or None
        body = ResearchDatasetReleaseCreate(
            release_id=release_id,
            dataset_name=dataset_name,
            release_kind=release_kind,  # type: ignore[arg-type]
            source=source,  # type: ignore[arg-type]
            version=version,
            symbols=normalized_symbols,
            symbols_from_release=symbols_from_release,
            full_market=full_market,
            exchange=exchange,
            listing_boards=list(listing_boards or []),
            start_date=date.fromisoformat(start_date),
            end_date=date.fromisoformat(end_date),
            adjustment=adjustment,  # type: ignore[arg-type]
            required_capabilities=list(required_capabilities or []),  # type: ignore[arg-type]
            consistency_baseline_release_id=consistency_baseline_release_id,
            consistency_fail_on_mismatch=consistency_fail_on_mismatch,
        )
        async with app.session_maker() as session:
            try:
                resolved_symbols = await resolve_release_symbols(
                    session,
                    release_kind=body.release_kind,
                    symbols=body.symbols,
                    symbols_from_release=body.symbols_from_release,
                    full_market=body.full_market,
                    exchange=body.exchange,
                    listing_boards=body.listing_boards,
                )
            except ReleaseSymbolSourceError as exc:
                raise McpToolError(
                    "invalid_argument", f"{exc.code}: {exc.summary}"
                ) from exc

        if body.symbols_from_release is not None:
            symbols_source: dict[str, Any] = {
                "mode": "from_release",
                "release_id": body.symbols_from_release,
            }
        elif body.full_market:
            symbols_source = {"mode": "full_market"}
            # #385:板块/交易所过滤溯源(归一化后记录;未声明保持旧形状)。
            if body.exchange is not None:
                symbols_source["exchange"] = body.exchange
            if body.listing_boards:
                symbols_source["listing_boards"] = list(body.listing_boards)
        else:
            symbols_source = {"mode": "inline"}
        payload: dict[str, Any] = {
            "release_id": body.release_id,
            "dataset_name": body.dataset_name,
            "release_kind": body.release_kind,
            "version": body.version,
            "start_date": body.start_date.isoformat(),
            "end_date": body.end_date.isoformat(),
            "adjustment": body.adjustment,
            "symbols": resolved_symbols,
            "required_capabilities": list(body.required_capabilities),
            "consistency_baseline_release_id": (
                body.consistency_baseline_release_id
            ),
            "consistency_fail_on_mismatch": body.consistency_fail_on_mismatch,
            # 执行器忽略未知键,仅供审计/排查。
            "symbols_source": symbols_source,
        }
        idempotency_key = f"publish:{body.release_id}"
        return await _enqueue_data_job(
            app,
            kind="dataset_publish",
            idempotency_key=idempotency_key,
            payload=payload,
            requested_by="mcp:dataset_publish",
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.dataset_release.publish",
        arguments={
            "release_id": release_id,
            "symbols": symbols,
            "symbols_from_release": symbols_from_release,
            "full_market": full_market,
            "exchange": exchange,
            "listing_boards": listing_boards,
            "version": version,
            "start_date": start_date,
            "end_date": end_date,
        },
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 7. data_config_get(只读,读 data_config.json)
# --------------------------------------------------------------------------- #


async def data_config_get(app: McpAppContext) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        cfg = await asyncio.to_thread(_load_config_sync)
        return _config_out_dict(cfg, settings=app.settings)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data.config_get",
        arguments={},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 8. data_config_update(写,合并字段)
# --------------------------------------------------------------------------- #


async def data_config_update(
    app: McpAppContext,
    *,
    sync_enabled: bool | None = None,
    sync_time: str | None = None,
    download_enabled: bool | None = None,
    download_time: str | None = None,
    download_lookback_days: int | None = None,
    download_markets: list[str] | None = None,
    download_types: list[str] | None = None,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        cfg = await asyncio.to_thread(_load_config_sync)
        updates = {
            "sync_enabled": sync_enabled,
            "sync_time": sync_time,
            "download_enabled": download_enabled,
            "download_time": download_time,
            "download_lookback_days": download_lookback_days,
            "download_markets": download_markets,
            "download_types": download_types,
        }
        cfg.update({k: v for k, v in updates.items() if v is not None})
        await asyncio.to_thread(_save_config_sync, cfg)
        return _config_out_dict(cfg, settings=app.settings)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data.config_update",
        arguments={
            "sync_enabled": sync_enabled,
            "sync_time": sync_time,
            "download_enabled": download_enabled,
            "download_time": download_time,
            "download_lookback_days": download_lookback_days,
            "download_markets": download_markets,
            "download_types": download_types,
        },
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 9. etf_sync(写,默认 dry_run=True 预览)
# --------------------------------------------------------------------------- #


async def etf_sync(
    app: McpAppContext,
    *,
    dry_run: bool = True,
    enrich_codes: list[str] | None = None,
) -> ToolEnvelope:
    """批量同步 ETF 元数据(akshare → 分类 → 写库 / 预览)。

    ``dry_run=True``(默认)只预览不写入;``dry_run=False`` 实际写库。
    """

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_data.assets import (
            AkShareEtfMetadataSource,
            EtfClassifier,
            EtfMetadataSync,
        )

        try:
            sync = EtfMetadataSync(AkShareEtfMetadataSource(), EtfClassifier())
        except ImportError as exc:
            raise McpToolError(
                "unavailable",
                f"数据源依赖未安装,无法同步 ETF 元数据: {exc}",
            ) from exc
        try:
            classifications = await sync.discover_and_classify(
                enrich_codes=enrich_codes or None,
            )
        except Exception as exc:
            raise McpToolError(
                "unavailable", f"ETF 元数据同步失败: {exc}"
            ) from exc

        async with app.session_maker() as session:
            repo = EtfMetadataRepository(session)
            if dry_run:
                preview = await repo.preview_upsert(classifications)
                return {
                    "total": preview.total,
                    "to_insert": preview.to_insert,
                    "to_update": preview.to_update,
                    "skipped_override": preview.skipped_override,
                    "needs_review": preview.needs_review,
                    "auto_adopted": preview.auto_adopted,
                    "dry_run": True,
                }
            inserted, updated, skipped = await repo.upsert_batch(classifications)
            await session.commit()
            return {
                "total": len(classifications),
                "to_insert": inserted,
                "to_update": updated,
                "skipped_override": skipped,
                "needs_review": sum(
                    1
                    for c in classifications
                    if c.review_status.value == "needs_review"
                ),
                "auto_adopted": sum(
                    1
                    for c in classifications
                    if c.review_status.value == "auto_adopted"
                ),
                "dry_run": False,
            }

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.etf.sync",
        arguments={"dry_run": dry_run, "enrich_codes": enrich_codes},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 10. etf_batch_confirm(写,needs_review → manually_confirmed)
# --------------------------------------------------------------------------- #


async def etf_batch_confirm(
    app: McpAppContext,
    *,
    codes: list[str],
    reason: str = "",
) -> ToolEnvelope:
    """批量确认待复核 ETF(needs_review → manually_confirmed)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        if not codes:
            raise McpToolError("invalid_argument", "至少指定一个 ETF 代码")
        async with app.session_maker() as session:
            confirmed = await EtfMetadataRepository(session).batch_confirm(
                codes, reason=reason
            )
            await session.commit()
        return {"confirmed": confirmed}

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.etf.batch_confirm",
        arguments={"codes": codes, "reason": reason},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 11. etf_update(写,人工覆盖分类 + 审计流水)
# --------------------------------------------------------------------------- #


async def etf_update(
    app: McpAppContext,
    *,
    code: str,
    reason: str,
    execution_profile: str | None = None,
    underlying_market: str | None = None,
    strategy_type: str | None = None,
    underlying_index: str | None = None,
) -> ToolEnvelope:
    """人工修正研究用 ETF 多维分类(写审计流水,设 manual_override=True)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_shared.types import EtfExecutionProfile

        normalized = code.strip().upper()
        async with app.session_maker() as session:
            instrument = (
                await session.execute(
                    select(InstrumentModel).where(
                        InstrumentModel.code == normalized
                    )
                )
            ).scalar_one_or_none()
            if instrument is None:
                raise McpToolError(
                    "not_found", f"未找到标的: {normalized}"
                )
            if instrument.instrument_type != "etf":
                raise McpToolError(
                    "conflict",
                    f"{normalized} 不是 ETF,不能写入 ETF 分类",
                )
            profile: EtfExecutionProfile | None = None
            if execution_profile is not None:
                profile = EtfExecutionProfile(execution_profile)
            row = await EtfMetadataRepository(session).apply_manual_override(
                normalized,
                execution_profile=profile,
                underlying_market=underlying_market,
                strategy_type=strategy_type,
                underlying_index=underlying_index,
                reason=reason,
            )
            await session.commit()
            return _etf_row_to_dict(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.etf.update",
        arguments={"code": code, "reason": reason},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 12. etf_review_queue(只读)
# --------------------------------------------------------------------------- #


async def etf_review_queue(
    app: McpAppContext,
    *,
    review_status: str | None = "needs_review",
    limit: int = 200,
) -> ToolEnvelope:
    """ETF 元数据待复核队列(默认查 needs_review)。只读。"""

    async def _do() -> list[dict[str, Any]]:
        from finboard_shared.types import ReviewStatus

        safe_limit = max(1, min(2000, limit))
        status = ReviewStatus(review_status) if review_status else None
        async with app.session_maker() as session:
            rows = await EtfMetadataRepository(session).list_by_review_status(
                status, limit=safe_limit
            )
            return [_etf_row_to_dict(r) for r in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.etf.review_queue",
        arguments={"review_status": review_status, "limit": limit},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# register
# --------------------------------------------------------------------------- #


def register(mcp: MCPServer) -> None:
    """把数据写操作工具注册到 MCP server(10 写 + 2 只读)。"""

    @mcp.tool(
        name="finboard_data_fetch",
        description=(
            "[写] 拉取单个标的的日 K 线并写入缓存(主源失败自动用备用源重试)。"
            "参数:symbol(标的代码)/ start / end(ISO 日期 YYYY-MM-DD)/ "
            "adjust(默认 qfq)/ source(可选 akshare|tushare|yfinance,默认 settings)。"
            "返回 {symbol, bar_count, first_date, last_date, source, fallback_used, "
            "fallback_source, lifecycle_events, lifecycle_sync_failed, "
            "lifecycle_sync_error}。"
            "主源及备用源均不可用返回 unavailable。同步执行,不进队列。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _data_fetch(
        symbol: str,
        start: str,
        end: str,
        adjust: str = "qfq",
        source: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_fetch(
            app_context(ctx),
            symbol=symbol,
            start=start,
            end=end,
            adjust=adjust,
            source=source,
        )

    @mcp.tool(
        name="finboard_data_sync_universe",
        description=(
            "[写] 登记全市场标的同步任务(akshare 发现 → 写 instruments 表,"
            "自动包含基准指数登记 instrument_type=index,#256;#265 起同时从"
            "东财可转债一览登记 instrument_type=convertible;#267 起同时从"
            "受控登记表登记 IF/IH/IC/IM 期货主连 instrument_type=futures,"
            "主连仅研究信号/基准、不可当作可成交合约),"
            "返回 202 + job_id。实际执行由 worker 消费 kind=data_sync 任务;"
            "进度/状态/取消用 finboard_job_get(job_id) 轮询。无参数。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _data_sync_universe(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_sync_universe(app_context(ctx))

    @mcp.tool(
        name="finboard_data_bulk_download_start",
        description=(
            "[写] 登记批量历史数据拉取任务(按市场/类型/交易所筛选标的池),"
            "返回 202 + job_id。实际执行由 worker 消费 kind=bulk_download 任务;"
            "进度/状态/取消用 finboard_job_get(job_id) 轮询。"
            "参数:market(默认 a_share;期货用 future)/ instrument_type(stock|etf|index|"
            "convertible|futures;index=#256 登记的基准指数,akshare 源走指数接口、"
            "tushare 源走 index_daily(#341,2000 积分档实测可调);"
            "convertible=#265 转债,走 tushare cb_daily 专属接口,akshare 源"
            "fail-visible 拒绝;futures=#267 期货主连(如 IF0.CFFEX),需配"
            " market=future,走 akshare 新浪 futures_main_sina,tushare 源"
            "fail-visible 拒绝;主连仅研究信号/基准,不可当作可成交合约)/ exchange / "
            "listing_boards(列表)/ start(默认 2015-01-01)/ source(可选;"
            "ETF 与期货仅 akshare|yfinance,tushare 源报 tushare_scope_mismatch;"
            "股票/转债/指数 tushare 放行)/ symbols(可选,#347 子集重跑:与"
            " market/instrument_type 过滤叠加,交集为空按 no_instruments 拒,"
            "部分失败任务的 error_summary 清单可直接回填)。"
            "#347 起入队期 payload 契约校验(#260 风格):未知 source / 非法日期 / "
            "tushare x etf|futures 等非法参数秒级 invalid_argument;部分标的失败"
            "仍 succeeded,失败标的与原因看 finboard_job_get 的 error_summary"
            "(phase 形如 bulk_download:partial N failed)。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _data_bulk_download_start(
        market: str = "a_share",
        instrument_type: str | None = None,
        exchange: str | None = None,
        listing_boards: list[str] | None = None,
        start: str = "2015-01-01",
        source: str | None = None,
        symbols: list[str] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_bulk_download_start(
            app_context(ctx),
            market=market,
            instrument_type=instrument_type,
            exchange=exchange,
            listing_boards=listing_boards,
            start=start,
            source=source,
            symbols=symbols,
        )

    @mcp.tool(
        name="finboard_data_quality_repair",
        description=(
            "[写] 登记批量缓存异常 bar 修复任务,返回 202 + job_id。"
            "实际执行由 worker 消费 kind=quality_repair 任务(读缓存→质量检查→"
            "拉取修复→重写);进度/状态/取消用 finboard_job_get(job_id) 轮询。"
            "参数:symbols(标的代码列表,必填)/ source(默认 akshare)/ "
            "adjust(默认 qfq)。写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _data_quality_repair(
        symbols: list[str],
        source: str = "akshare",
        adjust: str = "qfq",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_quality_repair(
            app_context(ctx), symbols=symbols, source=source, adjust=adjust
        )

    @mcp.tool(
        name="finboard_dataset_release_publish",
        description=(
            "[写] ⭐ 登记数据集冻结发布任务(研究闭环关键节点),返回 202 + job_id。"
            "实际执行(原子 rename + DB 登记 + 标的资产类型校验)由 worker 消费 "
            "kind=dataset_publish 任务;成功后 result_ref=release_id,用 "
            "finboard_job_get(job_id) 拿到 release_id 后再 finboard_dataset_release_get "
            "查发布详情。"
            "标的集三选一(#261,互斥):symbols(内联列表)/ "
            "symbols_from_release(复制既有可用发布的冻结标的集,免手工维护"
            "全市场清单)/ full_market=true(instruments 表全活跃标的按 kind "
            "展开:股票单源只取 A 股股票,multi_asset_mixed 取股票+ETF+指数+"
            "转债(#265)+期货主连(#267),convertible_metrics 只取转债)。"
            "full_market 可叠加 exchange / listing_boards 过滤(#385,仅该模式"
            "生效,与 symbols/symbols_from_release 混用拒绝;exchange 取值 "
            "SSE/SZSE/BSE/CFFEX,listing_board 取值 sse_main/szse_main/star/"
            "chinext/bse/cdr,ETF/指数/转债恒为 unknown;股票发布剔除北交所:"
            "listing_boards=[sse_main,szse_main,star,chinext,cdr](不含 bse)。"
            "来源发布不存在/不可用/展开为空入队即 invalid_argument 具名拒绝。"
            "其他参数:release_id / version / start_date / end_date / "
            "dataset_name(默认 multi_asset_daily_bars)/ release_kind"
            "(a_share_tushare|multi_asset_mixed|daily_metrics|financial_indicators|"
            "convertible_metrics)/ "
            "source / adjustment(qfq|hqfq|none;研究数据发布与 convertible_metrics 固定 none)/ "
            "required_capabilities(stock|bond|convertible|futures|etf:index|"
            "etf:cross_border|etf:commodity|etf:bond)/ "
            "consistency_baseline_release_id(#252:基线发布做标的集一致性校验,"
            "差集具名;默认 warning)/ consistency_fail_on_mismatch(默认 false,"
            "true 时不一致秒级失败 code=symbol_set_mismatch)。"
            "release_kind=daily_metrics|financial_indicators 时从 research_* 表"
            "冻结基本面/财务指标发布(issue #187),与 bars 发布联合供因子快照取数。"
            "release_kind=convertible_metrics(#265)只接受 A 股转债标的,从缓存 "
            "bars x 冻结转股价元数据计算转股价值/转股溢价率冻结为带日期观测"
            "(非全历史 PIT;元数据缺失先跑 dataset_sync 的 "
            "convertible_profiles)。"
            "研究数据发布建议带 baseline=同区间 bars 主发布 + fail_on_mismatch=true,"
            "标的集用 symbols_from_release 复制该 bars 主发布。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _dataset_release_publish(
        release_id: str,
        version: str,
        start_date: str,
        end_date: str,
        symbols: list[str] | None = None,
        symbols_from_release: str | None = None,
        full_market: bool = False,
        exchange: str | None = None,
        listing_boards: list[str] | None = None,
        dataset_name: str = "multi_asset_daily_bars",
        release_kind: str = "a_share_tushare",
        source: str | None = None,
        adjustment: str = "qfq",
        required_capabilities: list[str] | None = None,
        consistency_baseline_release_id: str | None = None,
        consistency_fail_on_mismatch: bool = False,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await dataset_release_publish(
            app_context(ctx),
            release_id=release_id,
            symbols=symbols,
            symbols_from_release=symbols_from_release,
            full_market=full_market,
            exchange=exchange,
            listing_boards=listing_boards,
            version=version,
            start_date=start_date,
            end_date=end_date,
            dataset_name=dataset_name,
            release_kind=release_kind,
            source=source,
            adjustment=adjustment,
            required_capabilities=required_capabilities,
            consistency_baseline_release_id=consistency_baseline_release_id,
            consistency_fail_on_mismatch=consistency_fail_on_mismatch,
        )

    @mcp.tool(
        name="finboard_data_config_get",
        description=(
            "查询定时任务调度器配置(读 data_config.json)。返回 "
            "{sync_enabled, sync_time, download_enabled, download_time, "
            "download_lookback_days, download_markets, download_types, "
            "data_provider}。只读。"
        ),
    )
    async def _data_config_get(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_config_get(app_context(ctx))

    @mcp.tool(
        name="finboard_data_config_update",
        description=(
            "[写] 更新定时任务调度器配置(合并写入 data_config.json,只更新"
            "非空字段)。参数:sync_enabled / sync_time / download_enabled / "
            "download_time / download_lookback_days / download_markets(列表)/ "
            "download_types(列表),均可选。返回更新后的完整配置。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _data_config_update(
        sync_enabled: bool | None = None,
        sync_time: str | None = None,
        download_enabled: bool | None = None,
        download_time: str | None = None,
        download_lookback_days: int | None = None,
        download_markets: list[str] | None = None,
        download_types: list[str] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_config_update(
            app_context(ctx),
            sync_enabled=sync_enabled,
            sync_time=sync_time,
            download_enabled=download_enabled,
            download_time=download_time,
            download_lookback_days=download_lookback_days,
            download_markets=download_markets,
            download_types=download_types,
        )

    @mcp.tool(
        name="finboard_etf_sync",
        description=(
            "[写] 批量同步 ETF 元数据(akshare → 分类 → 写库 / 预览)。"
            "参数:dry_run(默认 True 只预览不写入,显式 False 才写库)/ "
            "enrich_codes(额外拉单基金档案补充的标的列表)。"
            "返回 {total, to_insert, to_update, skipped_override, needs_review, "
            "auto_adopted, dry_run}。写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _etf_sync(
        dry_run: bool = True,
        enrich_codes: list[str] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await etf_sync(
            app_context(ctx), dry_run=dry_run, enrich_codes=enrich_codes
        )

    @mcp.tool(
        name="finboard_etf_batch_confirm",
        description=(
            "[写] 批量确认待复核 ETF(needs_review → manually_confirmed)。"
            "参数:codes(ETF 代码列表,1-2000)/ reason(可选)。"
            "返回 {confirmed: 确认数量}。写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _etf_batch_confirm(
        codes: list[str],
        reason: str = "",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await etf_batch_confirm(
            app_context(ctx), codes=codes, reason=reason
        )

    @mcp.tool(
        name="finboard_etf_update",
        description=(
            "[写] 人工修正研究用 ETF 多维分类(写审计流水,设 manual_override=True,"
            "后续自动同步不再覆盖)。"
            "参数:code(标的代码)/ reason(必填,变更理由)/ execution_profile"
            "(domestic_equity_etf|cross_border_etf|bond_etf|money_market_etf|"
            "commodity_etf)/ underlying_market(domestic|hk|overseas|global)/ "
            "strategy_type(index|active)/ underlying_index。"
            "未找到标的 → not_found;非 ETF → conflict。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _etf_update(
        code: str,
        reason: str,
        execution_profile: str | None = None,
        underlying_market: str | None = None,
        strategy_type: str | None = None,
        underlying_index: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await etf_update(
            app_context(ctx),
            code=code,
            reason=reason,
            execution_profile=execution_profile,
            underlying_market=underlying_market,
            strategy_type=strategy_type,
            underlying_index=underlying_index,
        )

    @mcp.tool(
        name="finboard_etf_review_queue",
        description=(
            "查询 ETF 元数据待复核队列(默认查 needs_review)。只读。"
            "参数:review_status(auto_adopted|needs_review|manually_confirmed|"
            "manually_overridden,默认 needs_review,传 None 查全部)/ "
            "limit(1-2000,默认 200)。返回 ETF 元数据 list。"
        ),
    )
    async def _etf_review_queue(
        review_status: str | None = "needs_review",
        limit: int = 200,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await etf_review_queue(
            app_context(ctx), review_status=review_status, limit=limit
        )


__all__ = [
    "data_bulk_download_start",
    "data_config_get",
    "data_config_update",
    "data_fetch",
    "data_quality_repair",
    "data_sync_universe",
    "dataset_release_publish",
    "etf_batch_confirm",
    "etf_review_queue",
    "etf_sync",
    "etf_update",
    "register",
]
