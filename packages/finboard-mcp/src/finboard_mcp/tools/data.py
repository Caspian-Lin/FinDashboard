"""``finboard.instrument.*`` / ``finboard.dataset.*`` / ``finboard.data.*`` /
``finboard.tushare.*`` 工具 —— 数据查询(只读,issue #124)。

复用现有 repository / domain 类,不重复业务逻辑:

* DB-backed(instrument / dataset release / manifest)—— 用
  :class:`~finboard_persistence.InstrumentRepository` /
  :class:`~finboard_persistence.ResearchDatasetReleaseRepository` /
  直接 SQLAlchemy 查询,经 ``app.session_maker()`` 获取 session。
* file-backed(cache status / quality / tushare quota)—— 复用
  ``finboard_data`` 的 ``ParquetCache`` / ``BarQualityChecker`` /
  ``shared_tushare_budget``,在 ``_do()`` 闭包内 lazy import(与
  ``finboard_api.routes.data`` 一致,pyarrow / tushare 为可选依赖)。

权限:只读工具,自动允许,不触及交易安全红线。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from sqlalchemy import select

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_persistence import (
    DatasetManifestModel,
    InstrumentModel,
    InstrumentRepository,
    ResearchDatasetReleaseRepository,
)

if TYPE_CHECKING:
    from finboard_persistence.models import (
        DatasetManifestModel as ManifestRow,
    )
    from finboard_persistence.models import InstrumentModel as InstrumentRow

# 与 finboard_api.routes.data 一致:缓存目录硬编码(当前无 settings 字段)。
_CACHE_DIR = "data_cache"


# ---------------------------------------------------------------------------
# 字段映射(复用 finboard_api.routes.instruments 的 _xxx_to_out 字段清单)
# ---------------------------------------------------------------------------


def _instrument_to_dict(row: InstrumentRow) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "code": row.code,
                "name": row.name,
                "market": row.market,
                "instrument_type": row.instrument_type,
                "exchange": row.exchange,
                "listing_board": row.listing_board,
                "list_date": row.list_date,
                "delist_date": row.delist_date,
                "status": row.status,
                "sector": row.sector,
                "industry": row.industry,
            }
        ),
    )


def _manifest_to_dict(row: ManifestRow) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "id": row.id,
                "dataset_name": row.dataset_name,
                "source": row.source,
                "version": row.version,
                "start_date": row.start_date,
                "end_date": row.end_date,
                "row_count": row.row_count,
                "symbol_count": row.symbol_count,
                "coverage_pct": row.coverage_pct,
                "gaps": row.gaps,
                "checksum": row.checksum,
                "quality_status": row.quality_status,
                "quality_report": row.quality_report,
                "published_at": row.published_at,
                "code_version": row.code_version,
            }
        ),
    )


def _release_summary_to_dict(release: Any) -> dict[str, Any]:
    """把 ResearchDatasetRelease 领域对象映射为 summary dict。

    复用 ``finboard_api.routes.instruments._release_to_summary`` 的字段清单。
    """
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "release_id": release.release_id,
                "dataset_name": release.dataset_name,
                "source": release.source,
                "version": release.version,
                "schema_version": release.schema_version,
                "start_date": release.start_date,
                "end_date": release.end_date,
                "period": release.period.value,
                "adjustment": release.adjustment,
                "code_version": release.code_version,
                "published_at": release.published_at,
                "symbol_count": release.symbol_count,
                "row_count": release.row_count,
                "coverage_pct": release.coverage_pct,
                "capabilities": [
                    {
                        "key": c.key,
                        "status": c.status.value,
                        "symbol_count": c.symbol_count,
                        "ready_count": c.ready_count,
                        "missing_requirements": list(c.missing_requirements),
                    }
                    for c in release.capabilities
                ],
                "quality_status": release.quality_status.value,
                "known_limitations": list(release.known_limitations),
                "metadata_version": release.metadata_version,
                "release_checksum": release.release_checksum,
            }
        ),
    )


def _release_detail_to_dict(release: Any) -> dict[str, Any]:
    """把 ResearchDatasetRelease 映射为详情 dict。

    复用 ``finboard_api.routes.instruments._release_detail_payload``:
    ``as_dict()`` + 补 symbol_count / row_count / coverage_pct。
    """
    payload = cast(dict[str, Any], release.as_dict())
    payload.update(
        {
            "symbol_count": release.symbol_count,
            "row_count": release.row_count,
            "coverage_pct": release.coverage_pct,
        }
    )
    return cast(dict[str, Any], to_jsonable(payload))


# ---------------------------------------------------------------------------
# finboard.instrument.*(DB-backed,InstrumentRepository)
# ---------------------------------------------------------------------------


async def instrument_list(
    app: McpAppContext,
    *,
    market: str | None = None,
    instrument_type: str | None = None,
    exchange: str | None = None,
    listing_board: list[str] | None = None,
    status: str | None = "active",
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            repo = InstrumentRepository(session)
            rows, total = await repo.list_page(
                market=market,
                instrument_type=instrument_type,
                exchange=exchange,
                listing_boards=listing_board,
                status=status,
                q=q,
                limit=limit,
                offset=offset,
            )
            return {
                "items": [_instrument_to_dict(r) for r in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.instrument.list",
        arguments={
            "market": market,
            "instrument_type": instrument_type,
            "exchange": exchange,
            "listing_board": listing_board,
            "status": status,
            "q": q,
            "limit": limit,
            "offset": offset,
        },
        handler=_do,
    )


async def instrument_get(app: McpAppContext, code: str) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            stmt = select(InstrumentModel).where(InstrumentModel.code == code)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                raise McpToolError("not_found", f"未找到标的: {code}")
            return _instrument_to_dict(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.instrument.get",
        arguments={"code": code},
        handler=_do,
    )


async def instrument_search(
    app: McpAppContext,
    q: str,
    *,
    limit: int = 50,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        async with app.session_maker() as session:
            repo = InstrumentRepository(session)
            rows = await repo.search(q, limit=limit)
            return [_instrument_to_dict(r) for r in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.instrument.search",
        arguments={"q": q, "limit": limit},
        handler=_do,
    )


# ---------------------------------------------------------------------------
# finboard.dataset.*(DB-backed,ResearchDatasetReleaseRepository / manifest)
# ---------------------------------------------------------------------------


async def dataset_release_list(
    app: McpAppContext,
    *,
    dataset_name: str | None = None,
    source: str | None = None,
    quality_status: str | None = None,
    limit: int = 50,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        async with app.session_maker() as session:
            repo = ResearchDatasetReleaseRepository(session)
            releases = await repo.list(
                dataset_name=dataset_name,
                source=source,
                quality_status=quality_status,
                limit=limit,
            )
            return [_release_summary_to_dict(r) for r in releases]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.dataset_release.list",
        arguments={
            "dataset_name": dataset_name,
            "source": source,
            "quality_status": quality_status,
            "limit": limit,
        },
        handler=_do,
    )


async def dataset_release_get(
    app: McpAppContext,
    release_id: str,
    *,
    view: str = "summary",
) -> ToolEnvelope:
    """查询单个研究数据发布(issue #206:view 默认 summary,省略逐标的数组)。"""

    async def _do() -> dict[str, Any]:
        if view not in ("summary", "detail"):
            raise McpToolError("invalid_argument", f"未知视图: {view}")
        async with app.session_maker() as session:
            repo = ResearchDatasetReleaseRepository(session)
            release = await repo.get(release_id)
            if release is None:
                raise McpToolError(
                    "not_found", f"未找到研究数据发布: {release_id}"
                )
            if view == "summary":
                return _release_summary_to_dict(release)
            return _release_detail_to_dict(release)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.dataset_release.get",
        arguments={"release_id": release_id, "view": view},
        handler=_do,
    )


async def dataset_manifest_list(
    app: McpAppContext,
    *,
    dataset_name: str | None = None,
    quality_status: str | None = None,
    limit: int = 50,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        async with app.session_maker() as session:
            stmt = select(DatasetManifestModel)
            if dataset_name is not None:
                stmt = stmt.where(
                    DatasetManifestModel.dataset_name == dataset_name
                )
            if quality_status is not None:
                stmt = stmt.where(
                    DatasetManifestModel.quality_status == quality_status
                )
            stmt = (
                stmt.order_by(DatasetManifestModel.published_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [
                _manifest_to_dict(r) for r in result.scalars().all()
            ]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.dataset.manifest_list",
        arguments={
            "dataset_name": dataset_name,
            "quality_status": quality_status,
            "limit": limit,
        },
        handler=_do,
    )


# ---------------------------------------------------------------------------
# finboard.data.* / finboard.tushare.*(file-backed,lazy import)
# ---------------------------------------------------------------------------


async def data_cache_status(
    app: McpAppContext,
    *,
    limit: int = 200,
    offset: int = 0,
    q: str | None = None,
    period: str | None = None,
    adjust: str | None = None,
    listing_board: list[str] | None = None,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        from finboard_data.cache import ParquetCache
        from finboard_data.discovery import infer_a_share_listing_board

        safe_limit = min(max(limit, 1), 500)
        safe_offset = max(offset, 0)
        cache = ParquetCache(_CACHE_DIR, max_io_concurrency=8)
        cache_path = Path(_CACHE_DIR)
        parquet_files = sorted(
            await asyncio.to_thread(lambda: list(cache_path.glob("*.parquet")))
        )
        query = q.strip().upper() if q else None

        def matches(path: Path) -> bool:
            parts = path.stem.rsplit("_", 2)
            if len(parts) != 3:
                return False
            code, period_str, adjustment = parts
            board = infer_a_share_listing_board(code).value
            return (
                (query is None or query in code.upper())
                and (period is None or period_str == period)
                and (adjust is None or adjustment == adjust)
                and (not listing_board or board in listing_board)
            )

        matching_files = [path for path in parquet_files if matches(path)]
        items: list[dict[str, Any]] = []
        for path in matching_files[safe_offset : safe_offset + safe_limit]:
            parts = path.stem.rsplit("_", 2)
            if len(parts) != 3:
                continue
            code, period_str, adj = parts
            metadata = await cache.metadata(path)
            items.append(
                {
                    "symbol": code,
                    "listing_board": infer_a_share_listing_board(code).value,
                    "period": period_str,
                    "adjust": adj,
                    "bar_count": metadata.bar_count,
                    "first_date": (
                        str(metadata.first_date)
                        if metadata.first_date
                        else None
                    ),
                    "last_date": (
                        str(metadata.last_date)
                        if metadata.last_date
                        else None
                    ),
                    "last_close": None,
                    "source": metadata.source,
                }
            )
        return {
            "items": items,
            "total": len(matching_files),
            "limit": safe_limit,
            "offset": safe_offset,
        }

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data.cache_status",
        arguments={
            "limit": limit,
            "offset": offset,
            "q": q,
            "period": period,
            "adjust": adjust,
            "listing_board": listing_board,
        },
        handler=_do,
    )


async def data_quality_check(
    app: McpAppContext,
    *,
    symbols: str | None = None,
    adjust: str = "qfq",
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        from finboard_data.cache import ParquetCache, make_symbol
        from finboard_data.quality import BarQualityChecker
        from finboard_shared.types import BarPeriod

        cache = ParquetCache(_CACHE_DIR)
        checker = BarQualityChecker()

        if symbols:
            codes = [c.strip() for c in symbols.split(",") if c.strip()]
        else:
            all_files = os.listdir(_CACHE_DIR)
            codes = sorted(
                f.rsplit("_", 2)[0]
                for f in all_files
                if f.endswith(f"_{BarPeriod.D1.value}_{adjust}.parquet")
            )

        results: list[dict[str, Any]] = []
        for code in codes:
            try:
                sym = make_symbol(code)
                bars = await cache.read(sym, BarPeriod.D1, adjust)
                qr = checker.check(bars, symbol=code)
                results.append(
                    {
                        "symbol": code,
                        "total_bars": qr.total_bars,
                        "anomaly_count": qr.anomaly_count,
                        "duplicate_count": qr.duplicate_count,
                        "sources": list(qr.sources),
                        "anomalies": [
                            {
                                "date": str(a.date),
                                "source": a.source,
                                "reasons": list(a.reasons),
                            }
                            for a in qr.anomalies[:20]
                        ],
                        "passed": bool(bars) and qr.passed,
                        "primary_source": (
                            qr.sources[0] if qr.sources else ""
                        ),
                    }
                )
            except Exception as exc:  # 与路由一致,逐 code 容错
                results.append(
                    {
                        "symbol": code,
                        "total_bars": 0,
                        "anomaly_count": 0,
                        "duplicate_count": 0,
                        "passed": False,
                        "error": str(exc),
                    }
                )
        return cast(list[dict[str, Any]], to_jsonable(results))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data.quality_check",
        arguments={"symbols": symbols, "adjust": adjust},
        handler=_do,
    )


async def tushare_quota(app: McpAppContext) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        from finboard_data.tushare_budget import shared_tushare_budget

        settings = app.settings
        budget = shared_tushare_budget(
            requests_per_minute=getattr(
                settings, "tushare_requests_per_minute", 200
            ),
            daily_request_limit=getattr(
                settings, "tushare_daily_request_limit", 100_000
            ),
            usage_file=getattr(
                settings, "tushare_usage_file", "data_cache/tushare_usage.json"
            ),
        )
        snapshot = await budget.snapshot()
        return {
            "date": snapshot.date,
            "requests_per_minute": snapshot.requests_per_minute,
            "daily_limit": snapshot.daily_limit,
            "used": snapshot.used,
            "remaining": snapshot.remaining,
        }

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.tushare.quota",
        arguments={},
        handler=_do,
    )


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def register(mcp: MCPServer) -> None:
    """把数据查询工具注册到 MCP server。"""

    @mcp.tool(
        name="finboard_instrument_list",
        description=(
            "列出标的元数据(分页/搜索)。返回 {items, total, limit, offset}。"
            "可选过滤:market(如 a_share)/ instrument_type(如 stock)/ "
            "exchange / listing_board(列表)/ status(默认 active,传 None 查全部)/"
            " q(代码或名称模糊匹配)。用于了解系统有哪些标的。"
        ),
    )
    async def _instrument_list(
        market: str | None = None,
        instrument_type: str | None = None,
        exchange: str | None = None,
        listing_board: list[str] | None = None,
        status: str | None = "active",
        q: str | None = None,
        limit: int = 200,
        offset: int = 0,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await instrument_list(
            app_context(ctx),
            market=market,
            instrument_type=instrument_type,
            exchange=exchange,
            listing_board=listing_board,
            status=status,
            q=q,
            limit=limit,
            offset=offset,
        )

    @mcp.tool(
        name="finboard_instrument_get",
        description=(
            "查询单个标的详情(按代码)。返回 code/name/market/instrument_type/"
            "exchange/listing_board/list_date/delist_date/status/sector/industry。"
            "未找到返回 not_found 错误。"
        ),
    )
    async def _instrument_get(
        code: str, ctx: Context = None  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await instrument_get(app_context(ctx), code)

    @mcp.tool(
        name="finboard_instrument_search",
        description=(
            "模糊搜索标的(按代码或名称,仅 active)。返回标的列表。"
            "用于快速定位标的代码。参数:q(必填)/ limit(默认 50)。"
        ),
    )
    async def _instrument_search(
        q: str,
        limit: int = 50,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await instrument_search(app_context(ctx), q, limit=limit)

    @mcp.tool(
        name="finboard_dataset_release_list",
        description=(
            "列出已发布的研究数据集版本(版本化、时点安全、不可变)。"
            "每条含 release_id/dataset_name/source/version/period/"
            "symbol_count/coverage_pct/quality_status 等。"
            "可选过滤:dataset_name / source / quality_status(passed|warnings)。"
        ),
    )
    async def _dataset_release_list(
        dataset_name: str | None = None,
        source: str | None = None,
        quality_status: str | None = None,
        limit: int = 50,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await dataset_release_list(
            app_context(ctx),
            dataset_name=dataset_name,
            source=source,
            quality_status=quality_status,
            limit=limit,
        )

    @mcp.tool(
        name="finboard_dataset_release_get",
        description=(
            "查询数据集发布详情。view=summary(默认):头部字段 + capabilities"
            " + 覆盖统计(symbol_count/row_count/coverage_pct),**不含逐标的 "
            "instruments 数组**(全市场发布可达几十 MB);view=detail:完整 "
            "as_dict()(含逐标的覆盖、资产规则,诊断用)。未找到返回 not_found。"
        ),
    )
    async def _dataset_release_get(
        release_id: str,
        view: str = "summary",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await dataset_release_get(
            app_context(ctx), release_id, view=view
        )

    @mcp.tool(
        name="finboard_dataset_manifest_list",
        description=(
            "列出数据集发布清单(dataset manifests)。"
            "每条含 dataset_name/version/row_count/symbol_count/coverage_pct/"
            "quality_status/quality_report/published_at。"
            "可选过滤:dataset_name / quality_status。"
        ),
    )
    async def _dataset_manifest_list(
        dataset_name: str | None = None,
        quality_status: str | None = None,
        limit: int = 50,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await dataset_manifest_list(
            app_context(ctx),
            dataset_name=dataset_name,
            quality_status=quality_status,
            limit=limit,
        )

    @mcp.tool(
        name="finboard_data_cache_status",
        description=(
            "查询行情缓存状态(分页)。扫描 data_cache/*.parquet,"
            "返回 {items, total, limit, offset}。每条含 symbol/listing_board/"
            "period/adjust/bar_count/first_date/last_date/source。"
            "可选过滤:q(代码)/ period / adjust / listing_board(列表)。"
            "用于了解哪些标的有缓存数据。"
        ),
    )
    async def _data_cache_status(
        limit: int = 200,
        offset: int = 0,
        q: str | None = None,
        period: str | None = None,
        adjust: str | None = None,
        listing_board: list[str] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_cache_status(
            app_context(ctx),
            limit=limit,
            offset=offset,
            q=q,
            period=period,
            adjust=adjust,
            listing_board=listing_board,
        )

    @mcp.tool(
        name="finboard_data_quality_check",
        description=(
            "检查已缓存行情数据的质量(缺失/异常/重复)。返回逐标的报告列表。"
            "每条含 symbol/total_bars/anomaly_count/duplicate_count/sources/"
            "anomalies/passed/primary_source/error。"
            "参数:symbols(逗号分隔代码,不传则检查全部)/ adjust(默认 qfq)。"
        ),
    )
    async def _data_quality_check(
        symbols: str | None = None,
        adjust: str = "qfq",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_quality_check(
            app_context(ctx), symbols=symbols, adjust=adjust
        )

    @mcp.tool(
        name="finboard_tushare_quota",
        description=(
            "查询 Tushare API 配额状态(RPM / 每日限额 / 已用 / 剩余)。"
            "无参数。返回 date/requests_per_minute/daily_limit/used/remaining。"
            "用于判断是否还能拉取数据。"
        ),
    )
    async def _tushare_quota(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await tushare_quota(app_context(ctx))


__all__ = [
    "data_cache_status",
    "data_quality_check",
    "dataset_manifest_list",
    "dataset_release_get",
    "dataset_release_list",
    "instrument_get",
    "instrument_list",
    "instrument_search",
    "register",
    "tushare_quota",
]
