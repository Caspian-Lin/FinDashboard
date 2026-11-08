"""``finboard.data_catalog.*`` / ``finboard.dataset.release.*`` 工具 ——
全局数据目录查询(只读,issue #509)。

复用 :class:`~finboard_backtest.data_catalog.DataCatalogService`(与 REST
``/api/data/catalog`` 同源):目录列表 / 数据集详情 / 覆盖报告 / 发布逐标的 /
发布引用反查。**只读** —— 不触发 ``dataset_sync``、不发布数据集、不修改任何表。

MCP 层只做三件事:

* 入参防御性收敛(limit 1..500 / offset >= 0 / q 长度截断),避免误传全量把
  响应撑到 MB 级;
* dataclass → JSON 兼容 dict(``Decimal`` → ``float``,日期/时间交给
  :func:`~finboard_mcp.tools._serde.to_jsonable`);
* ``CatalogError`` → :class:`~finboard_mcp.execution.McpToolError` 分类映射
  (``release_not_found`` → ``not_found``,其余 → ``conflict``)。

目录只投影 DB 列 + 有界文件系统扫描,不加载发布 manifest。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_backtest.data_catalog import (
    CatalogEntry,
    CatalogError,
    CatalogQuery,
    DataCatalogService,
)
from finboard_backtest.data_catalog.contracts import CATALOG_STATUSES
from finboard_data.datasets import CatalogDataset
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ErrorKind, ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_persistence.catalog_repo import (
    BatchHeader,
    PresenceRow,
    ReleaseHeader,
    ReleaseInstrumentRow,
    ReleaseReferenceRow,
)

#: 单页上限(clamp 用;与 REST 的 500 上限同口径)。
MAX_PAGE_LIMIT = 500
#: 目录搜索词上限(REST ``q`` max_length=128 同口径)。
MAX_CATALOG_QUERY_LENGTH = 128
#: 逐标的搜索词上限(REST ``q`` max_length=64 同口径)。
MAX_INSTRUMENT_QUERY_LENGTH = 64


# ---------------------------------------------------------------------------
# 服务构造(不打开 session,调用方负责 async with)
# ---------------------------------------------------------------------------


def _service(session: AsyncSession) -> DataCatalogService:
    """构造目录服务;缓存/发布根目录与 REST 一致,由环境变量覆盖。"""
    return DataCatalogService(
        session,
        cache_dir=Path(os.getenv("FINBOARD_DATA_CACHE_DIR", "data_cache")),
        release_root=Path(
            os.getenv("FINBOARD_DATA_RELEASE_ROOT", "data_releases")
        ),
    )


def _catalog_error(exc: CatalogError) -> McpToolError:
    """``CatalogError`` → 具名工具错误(与 REST 404/409 映射对齐)。"""
    kind: ErrorKind = (
        "not_found" if exc.code == "release_not_found" else "conflict"
    )
    return McpToolError(kind, exc.message)


# ---------------------------------------------------------------------------
# 入参收敛(服务端不假设客户端已按 schema 传参)
# ---------------------------------------------------------------------------


def _clamp_limit(value: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return min(max(number, 1), MAX_PAGE_LIMIT)


def _clamp_offset(value: int) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _clamp_q(value: str | None, *, maximum: int) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return text[:maximum]


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _catalog_key(dataset: str) -> CatalogDataset:
    try:
        return CatalogDataset(str(dataset).strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in CatalogDataset)
        raise McpToolError(
            "invalid_argument", f"未知数据集: {dataset}(可选: {allowed})"
        ) from exc


def _validated_status(status: str | None) -> str | None:
    if status is None:
        return None
    normalized = status.strip().lower()
    if normalized not in CATALOG_STATUSES:
        raise McpToolError(
            "invalid_argument",
            f"未知目录状态: {status}(可选: {', '.join(CATALOG_STATUSES)})",
        )
    return normalized


def _release_id_arg(release_id: str) -> str:
    text = str(release_id).strip()
    if not text:
        raise McpToolError("invalid_argument", "release_id 不能为空")
    return text


# ---------------------------------------------------------------------------
# 序列化(dataclass → JSON 兼容 dict;Decimal 一律 float)
# ---------------------------------------------------------------------------


def _decimal_to_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _release_to_dict(header: ReleaseHeader) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "release_id": header.release_id,
                "dataset_name": header.dataset_name,
                "source": header.source,
                "version": header.version,
                "dataset_kind": header.dataset_kind.value,
                "schema_version": header.schema_version,
                "start_date": header.start_date,
                "end_date": header.end_date,
                "adjustment": header.adjustment,
                "symbol_count": header.symbol_count,
                "row_count": header.row_count,
                "coverage_pct": _decimal_to_float(header.coverage_pct),
                "quality_status": header.quality_status,
                "published_at": header.published_at,
                "release_checksum": header.release_checksum,
                "storage_uri": header.storage_uri,
                "capabilities": [dict(item) for item in header.capabilities],
            }
        ),
    )


def _batch_to_dict(batch: BatchHeader) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "id": batch.id,
                "dataset": batch.dataset,
                "source": batch.source,
                "dataset_version": batch.dataset_version,
                "status": batch.status,
                "quality_status": batch.quality_status,
                "expected_rows": batch.expected_rows,
                "received_rows": batch.received_rows,
                "accepted_rows": batch.accepted_rows,
                "rejected_rows": batch.rejected_rows,
                "parameters": dict(batch.parameters),
                "error_summary": batch.error_summary,
                "started_at": batch.started_at,
                "completed_at": batch.completed_at,
                "published_at": batch.published_at,
            }
        ),
    )


def _entry_to_dict(entry: CatalogEntry) -> dict[str, Any]:
    """``CatalogEntry`` → dict(复用 REST 详情/列表的字段清单)。"""
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "key": entry.key.value,
                "title": entry.title,
                "title_en": entry.title_en,
                "storage": entry.storage.value,
                "publishable": entry.publishable,
                "source": entry.source,
                "status": entry.status,
                "release": (
                    _release_to_dict(entry.release) if entry.release else None
                ),
                "batch": _batch_to_dict(entry.batch) if entry.batch else None,
                "coverage_pct": _decimal_to_float(entry.coverage_pct),
                "symbol_count": entry.symbol_count,
                "row_count": entry.row_count,
                "denominator": entry.denominator,
                "local_start": entry.local_start,
                "local_end": entry.local_end,
                "updated_at": entry.updated_at,
                "cache_files": entry.cache_files,
                "cache_bytes": entry.cache_bytes,
                "warnings": list(entry.warnings),
            }
        ),
    )


def _instrument_to_dict(row: ReleaseInstrumentRow) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "code": row.code,
                "name": row.name,
                "market": row.market,
                "instrument_type": row.instrument_type,
                "asset_class": row.asset_class,
                "exchange": row.exchange,
                "listing_board": row.listing_board,
                "start_date": row.start_date,
                "end_date": row.end_date,
                "row_count": row.row_count,
                "expected_sessions": row.expected_sessions,
                "missing_sessions": row.missing_sessions,
                "suspended_sessions": row.suspended_sessions,
                "anomaly_count": row.anomaly_count,
                "duplicate_count": row.duplicate_count,
                "coverage_pct": _decimal_to_float(row.coverage_pct),
                "ready": row.ready,
                "issues": list(row.issues),
                "list_date": row.list_date,
                "delist_date": row.delist_date,
                "status": row.status,
            }
        ),
    )


def _presence_to_dict(row: PresenceRow) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "release_id": row.release_id,
                "dataset_kind": row.dataset_kind.value,
                "version": row.version,
                "quality_status": row.quality_status,
                "published_at": row.published_at,
                "start_date": row.start_date,
                "end_date": row.end_date,
                "coverage_pct": _decimal_to_float(row.coverage_pct),
                "ready": row.ready,
            }
        ),
    )


def _reference_to_dict(
    row: ReleaseReferenceRow, deep_link: str | None = None
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "kind": row.kind,
                "ref_id": row.ref_id,
                "status": row.status,
                "created_at": row.created_at,
                "deep_link": deep_link,
            }
        ),
    )


# ---------------------------------------------------------------------------
# finboard.data_catalog.*(只读)
# ---------------------------------------------------------------------------


async def data_catalog_list(
    app: McpAppContext,
    *,
    dataset: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ToolEnvelope:
    """全局数据目录列表(数据集维度,只读)。"""

    async def _do() -> dict[str, Any]:
        query = CatalogQuery(
            dataset=_catalog_key(dataset) if dataset is not None else None,
            status=_validated_status(status),
            q=_clamp_q(q, maximum=MAX_CATALOG_QUERY_LENGTH),
            limit=_clamp_limit(limit),
            offset=_clamp_offset(offset),
        )
        async with app.session_maker() as session:
            items, total = await _service(session).list_entries(query)
        return cast(
            dict[str, Any],
            to_jsonable(
                {
                    "items": [_entry_to_dict(item) for item in items],
                    "total": total,
                    "limit": query.limit,
                    "offset": query.offset,
                    "generated_at": datetime.now(UTC),
                }
            ),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data_catalog.list",
        arguments={
            "dataset": dataset,
            "status": status,
            "q": q,
            "limit": limit,
            "offset": offset,
        },
        handler=_do,
    )


async def data_catalog_get(
    app: McpAppContext, dataset: str
) -> ToolEnvelope:
    """单个数据集的目录详情(只读)。"""

    async def _do() -> dict[str, Any]:
        key = _catalog_key(dataset)
        async with app.session_maker() as session:
            detail = await _service(session).get_dataset(key)
        return cast(
            dict[str, Any],
            to_jsonable(
                {
                    "entry": _entry_to_dict(detail.entry),
                    "fields": list(detail.spec.fields),
                    "unit_notes": list(detail.spec.unit_notes),
                    "pit_rule": detail.spec.pit_rule,
                    "calendar_market": detail.spec.calendar_market,
                    "adjustment": detail.adjustment,
                    "publishable": detail.spec.publishable,
                    "releases": [
                        _release_to_dict(item) for item in detail.releases
                    ],
                    "batches": [
                        _batch_to_dict(item) for item in detail.batches
                    ],
                    "failures": [
                        _batch_to_dict(item) for item in detail.failures
                    ],
                }
            ),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data_catalog.get",
        arguments={"dataset": dataset},
        handler=_do,
    )


async def data_coverage_get(
    app: McpAppContext,
    *,
    dataset: str,
    release_id: str | None = None,
    limit: int = 200,
) -> ToolEnvelope:
    """数据集覆盖报告(只读,缺失清单只给有界预览)。"""

    async def _do() -> dict[str, Any]:
        key = _catalog_key(dataset)
        safe_release_id = _clean_optional(release_id)
        safe_limit = _clamp_limit(limit)
        async with app.session_maker() as session:
            try:
                report = await _service(session).get_coverage(
                    key, release_id=safe_release_id, limit=safe_limit
                )
            except CatalogError as exc:
                raise _catalog_error(exc) from exc
        return cast(
            dict[str, Any],
            to_jsonable(
                {
                    "dataset": report.dataset.value,
                    "denominator": report.denominator,
                    "release": (
                        _release_to_dict(report.release)
                        if report.release
                        else None
                    ),
                    "batch": (
                        _batch_to_dict(report.batch) if report.batch else None
                    ),
                    "coverage_pct": _decimal_to_float(report.coverage_pct),
                    "expected_sessions": report.expected_sessions,
                    "missing_sessions": report.missing_sessions,
                    "symbols_with_gaps": report.symbols_with_gaps,
                    "missing_preview": [
                        _instrument_to_dict(item)
                        for item in report.missing_preview
                    ],
                    "warnings": list(report.warnings),
                }
            ),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data_catalog.coverage",
        arguments={
            "dataset": dataset,
            "release_id": release_id,
            "limit": limit,
        },
        handler=_do,
    )


async def dataset_release_instruments(
    app: McpAppContext,
    *,
    release_id: str,
    ready: bool | None = None,
    missing_only: bool = False,
    market: str | None = None,
    instrument_type: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> ToolEnvelope:
    """某份研究数据发布的逐标的覆盖页(只读)。"""

    async def _do() -> dict[str, Any]:
        safe_release_id = _release_id_arg(release_id)
        safe_limit = _clamp_limit(limit)
        safe_offset = _clamp_offset(offset)
        async with app.session_maker() as session:
            try:
                page = await _service(session).list_release_instruments(
                    safe_release_id,
                    ready=ready,
                    missing_only=bool(missing_only),
                    market=_clean_optional(market),
                    instrument_type=_clean_optional(instrument_type),
                    q=_clamp_q(q, maximum=MAX_INSTRUMENT_QUERY_LENGTH),
                    limit=safe_limit,
                    offset=safe_offset,
                )
            except CatalogError as exc:
                raise _catalog_error(exc) from exc
        return cast(
            dict[str, Any],
            to_jsonable(
                {
                    "release_id": page.release_id,
                    "items": [
                        _instrument_to_dict(item) for item in page.items
                    ],
                    "total": page.total,
                    "limit": safe_limit,
                    "offset": safe_offset,
                }
            ),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.dataset.release.instruments",
        arguments={
            "release_id": release_id,
            "ready": ready,
            "missing_only": missing_only,
            "market": market,
            "instrument_type": instrument_type,
            "q": q,
            "limit": limit,
            "offset": offset,
        },
        handler=_do,
    )


async def dataset_release_references(
    app: McpAppContext,
    *,
    release_id: str,
    limit: int = 20,
) -> ToolEnvelope:
    """引用某份研究数据发布的课题/研究运行/策略规格(只读反查)。"""

    async def _do() -> dict[str, Any]:
        safe_release_id = _release_id_arg(release_id)
        safe_limit = _clamp_limit(limit)
        async with app.session_maker() as session:
            try:
                page = await _service(session).list_release_references(
                    safe_release_id, limit=safe_limit
                )
            except CatalogError as exc:
                raise _catalog_error(exc) from exc
        return cast(
            dict[str, Any],
            to_jsonable(
                {
                    "release_id": page.release_id,
                    "items": [
                        _reference_to_dict(
                            item,
                            page.deep_links.get(f"{item.kind}:{item.ref_id}"),
                        )
                        for item in page.items
                    ],
                }
            ),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.dataset.release.references",
        arguments={"release_id": release_id, "limit": limit},
        handler=_do,
    )


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def register(mcp: MCPServer) -> None:
    """把全局数据目录只读工具注册到 MCP server。"""

    @mcp.tool(
        name="finboard_data_catalog_list",
        description=(
            "只读:列出全局数据目录(数据集维度)。返回 {items, total, limit, "
            "offset, generated_at};每条含 key/title/status/publishable/"
            "coverage_pct/symbol_count/release/batch/warnings。"
            "可选 dataset(bars|daily_metrics|financial_indicators|"
            "convertible_metrics|income_statements|balance_sheets|"
            "cashflow_statements|dividends|profiles|suspensions)/ "
            "status(usable|ingested_not_published|blocked|not_ingested|"
            "stale|unknown)/ q(标题模糊)。分页 limit 1..500(默认 50)、"
            "offset>=0。目录只读:不触发 dataset_sync、不发布数据集、"
            "不修改任何表。"
        ),
    )
    async def _data_catalog_list(
        dataset: str | None = None,
        status: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_catalog_list(
            app_context(ctx),
            dataset=dataset,
            status=status,
            q=q,
            limit=limit,
            offset=offset,
        )

    @mcp.tool(
        name="finboard_data_catalog_get",
        description=(
            "只读:查询单个数据集的目录详情(按 dataset key,无分页)。返回 "
            "entry + fields/unit_notes/pit_rule/calendar_market/adjustment/"
            "publishable + 最近 releases/batches/failures。未知 dataset → "
            "invalid_argument。只读:不触发 dataset_sync、不发布数据集。"
        ),
    )
    async def _data_catalog_get(
        dataset: str, ctx: Context = None  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_catalog_get(app_context(ctx), dataset)

    @mcp.tool(
        name="finboard_data_coverage_get",
        description=(
            "只读:查询数据集覆盖报告(默认最新可用发布,可用 release_id 指定)。"
            "返回 dataset/denominator/release/batch/coverage_pct/"
            "expected_sessions/missing_sessions/symbols_with_gaps/"
            "missing_preview/warnings;missing_preview 只含有缺口标的的前 "
            "limit 条预览(limit 1..500,默认 200),不是全量清单。"
            "只读:不触发 dataset_sync、不发布数据集。"
        ),
    )
    async def _data_coverage_get(
        dataset: str,
        release_id: str | None = None,
        limit: int = 200,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_coverage_get(
            app_context(ctx),
            dataset=dataset,
            release_id=release_id,
            limit=limit,
        )

    @mcp.tool(
        name="finboard_dataset_release_instruments",
        description=(
            "只读:分页查询某份研究数据发布的逐标的覆盖读模型。返回 "
            "{release_id, items, total, limit, offset};item 含 code/name/"
            "market/instrument_type/asset_class/coverage_pct/"
            "expected_sessions/missing_sessions/suspended_sessions/"
            "ready/issues 等。可选 ready(布尔)/ missing_only(只回有缺口标的)/ "
            "market / instrument_type / q(代码或名称)。分页 limit 1..500"
            "(默认 100)、offset>=0。发布不存在 → not_found;逐标的索引不可"
            "重建 → conflict。只读:不触发 dataset_sync、不发布数据集。"
        ),
    )
    async def _dataset_release_instruments(
        release_id: str,
        ready: bool | None = None,
        missing_only: bool = False,
        market: str | None = None,
        instrument_type: str | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await dataset_release_instruments(
            app_context(ctx),
            release_id=release_id,
            ready=ready,
            missing_only=missing_only,
            market=market,
            instrument_type=instrument_type,
            q=q,
            limit=limit,
            offset=offset,
        )

    @mcp.tool(
        name="finboard_dataset_release_references",
        description=(
            "只读:反查引用某份研究数据发布的课题/研究运行/策略规格。返回 "
            "{release_id, items:[{kind, ref_id, status, created_at, "
            "deep_link}]};deep_link 为 Web 深链(无对应页面时为 null)。"
            "limit 1..500(默认 20)。发布不存在 → not_found。"
            "只读:不触发 dataset_sync、不发布数据集。"
        ),
    )
    async def _dataset_release_references(
        release_id: str,
        limit: int = 20,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await dataset_release_references(
            app_context(ctx), release_id=release_id, limit=limit
        )


__all__ = [
    "MAX_CATALOG_QUERY_LENGTH",
    "MAX_INSTRUMENT_QUERY_LENGTH",
    "MAX_PAGE_LIMIT",
    "data_catalog_get",
    "data_catalog_list",
    "data_coverage_get",
    "dataset_release_instruments",
    "dataset_release_references",
    "register",
]
