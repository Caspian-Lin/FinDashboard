"""per-kind job payload 入队期契约(issue #260)。

此前 ``finboard_job_enqueue``(MCP)与 ``POST /api/jobs``(REST)对 payload
均为自由 dict,契约错误要到 worker 执行期才 fail-fast;未知键(如把
``datasets`` 误写成 ``data_types``)更是被静默忽略后按缺省全数据集执行了一个
不是调用方意图的同步。本模块提供按 kind 注册的 payload 校验器,REST 与 MCP
在**入队期**共用同一校验(#186/#203 秒级失败风格);执行器 ``execute()`` 入口
重放同一校验,覆盖旁路入队(直接写库 / 旧版本入队的存量行,#255 先例)。

注册表可扩展:新 kind 按需注册自己的校验器,未注册 kind 不做额外校验
(payload 自由结构保持向后兼容,不必一次性全做)。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any


def _dataset_sync_names() -> frozenset[str]:
    """``dataset_sync`` 数据集白名单(唯一事实来源 = SyncSpec 注册表,#392)。

    经函数延迟解析(模块 import 即触发内置六集注册),避免 payload_contracts
    在 import 期依赖 dataset_sync 包的初始化顺序。
    """

    from finboard_backtest.background_jobs.dataset_sync.spec import SYNC_SPECS

    return SYNC_SPECS.names


def _dataset_sync_per_symbol() -> frozenset[str]:
    from finboard_backtest.background_jobs.dataset_sync.spec import SYNC_SPECS

    return frozenset(
        spec.name for spec in SYNC_SPECS.specs if spec.is_per_symbol
    )


#: ``dataset_sync`` 合法 payload 键(issue #392):旧 research_data_sync 键集
#: + scope 四元组(#385 过滤语义升为框架级公共参数,与 bulk_download 共享解析)。
_DATASET_SYNC_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "datasets",
        "start_date",
        "end_date",
        "symbols",
        "exchange",
        "listing_boards",
        "instrument_type",
    }
)

#: ``bulk_download`` 合法 payload 键(issue #347)。
_BULK_DOWNLOAD_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"market", "source", "start", "instrument_type", "exchange", "listing_boards", "symbols"}
)

#: ``bulk_download`` 行情源白名单(#347)。空串 / 缺省 = 回落配置默认源
#: (``resolve_provider_name`` 的回落链:settings 默认 → env → akshare,
#: #341 跟进先例)。
BULK_DOWNLOAD_SOURCES: frozenset[str] = frozenset(
    {"akshare", "tushare", "yfinance"}
)

#: 逐标的迭代的数据集:symbol 池为空时整段循环零迭代(静默 no-op)。
_RESEARCH_DATA_SYNC_PER_SYMBOL: frozenset[str] = frozenset(
    {"financial_indicators", "industry_memberships"}
)


class PayloadContractError(Exception):
    """payload 契约校验失败(入队期拒绝;执行器重放时映射为 invalid_payload)。

    ``code`` 为具名根因,便于测试与日志定位:
    ``unknown_payload_key`` / ``missing_required_field`` / ``invalid_field_value``
    / ``empty_symbol_pool``。
    """

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary


def _contract_date(value: object, key: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise PayloadContractError(
                "invalid_field_value",
                f"{key} 必须是 ISO 日期字符串(YYYY-MM-DD),收到: {value!r}",
            ) from exc
    raise PayloadContractError(
        "invalid_field_value",
        f"{key} 必须是 ISO 日期字符串(YYYY-MM-DD),收到: {value!r}",
    )


def _require_date(payload: Mapping[str, Any], key: str) -> date:
    if payload.get(key) is None:
        raise PayloadContractError(
            "missing_required_field", f"{key} 必填(ISO 日期 YYYY-MM-DD)"
        )
    return _contract_date(payload[key], key)


def validate_dataset_sync_payload(payload: Mapping[str, Any]) -> None:
    """校验 ``kind=dataset_sync`` 的 payload(入队期契约,issue #392)。

    * 未知键拒绝(常见拼写错误 ``data_types`` 不存在,参数名为 ``datasets``);
    * ``start_date`` / ``end_date`` 必填且为 ISO 日期,``start <= end``;
    * ``datasets`` 按 SyncSpec 注册表枚举校验(缺省 = 全部注册数据集);
    * scope 四元组(``exchange`` / ``listing_boards`` / ``instrument_type`` /
      ``symbols``)经共享解析 ``normalize_sync_scope`` 校验归一(#385 语义,
      与 bulk_download 同一函数);
    * 逐标的数据集在「未提供 symbols、未声明宇宙过滤、且 datasets 不含
      profiles」时拒绝 —— symbol 池将解析为空、任务静默零迭代。
    """

    from finboard_backtest.background_jobs.dataset_sync.scope import (
        ScopeValueError,
        normalize_sync_scope,
    )

    unknown = sorted(set(payload) - _DATASET_SYNC_ALLOWED_KEYS)
    if unknown:
        raise PayloadContractError(
            "unknown_payload_key",
            f"未知 payload 键: {unknown};已知键: "
            f"{sorted(_DATASET_SYNC_ALLOWED_KEYS)}"
            "(常见拼写错误:data_types 不存在,数据集参数名为 datasets)",
        )

    names = _dataset_sync_names()
    raw_datasets = payload.get("datasets")
    if raw_datasets is None:
        datasets = names
    else:
        if not isinstance(raw_datasets, list) or not all(
            isinstance(item, str) for item in raw_datasets
        ):
            raise PayloadContractError(
                "invalid_field_value", "datasets 必须是字符串列表"
            )
        unknown_datasets = sorted(set(raw_datasets) - names)
        if unknown_datasets:
            raise PayloadContractError(
                "invalid_field_value",
                f"不支持的 datasets: {unknown_datasets};"
                f" 可用: {sorted(names)}",
            )
        datasets = frozenset(raw_datasets)

    start = _require_date(payload, "start_date")
    end = _require_date(payload, "end_date")
    if start > end:
        raise PayloadContractError(
            "invalid_field_value",
            f"start_date({start}) 不能晚于 end_date({end})",
        )

    symbols = payload.get("symbols")
    if symbols is not None and (
        not isinstance(symbols, list) or not all(isinstance(i, str) for i in symbols)
    ):
        raise PayloadContractError("invalid_field_value", "symbols 必须是字符串列表")

    # scope 四元组与 bulk_download 共享同一解析(#392:不复制两份语义)。
    try:
        scope = normalize_sync_scope(
            exchange=payload.get("exchange"),
            listing_boards=payload.get("listing_boards"),
            instrument_type=payload.get("instrument_type"),
            symbols=payload.get("symbols"),
        )
    except ScopeValueError as exc:
        raise PayloadContractError("invalid_field_value", str(exc)) from exc

    per_symbol = _dataset_sync_per_symbol() & datasets
    if (
        per_symbol
        and not scope.symbols
        and not scope.has_universe_filters
        and "profiles" not in datasets
    ):
        raise PayloadContractError(
            "empty_symbol_pool",
            f"datasets 含逐标的同步 {sorted(per_symbol)} 但未提供 symbols、"
            "未声明 exchange/listing_boards/instrument_type 宇宙过滤,且"
            " datasets 不含 profiles —— symbol 池将解析为空,任务静默零迭代;"
            "修复:提供 symbols 列表,声明宇宙过滤,或把 profiles 加入 "
            "datasets(以其同步结果作为全市场 symbol 池)",
        )


def validate_bulk_download_payload(payload: Mapping[str, Any]) -> None:
    """校验 ``kind=bulk_download`` 的 payload(入队期契约,issue #347)。

    * 未知键拒绝;``market`` 必填非空;``start`` 必填且为 ISO 日期;
    * ``source`` 白名单(akshare/tushare/yfinance,大小写不敏感,与
      ``resolve_provider_name`` 的 ``strip().lower()`` 口径一致);
      空串 / 缺省 = 回落配置默认源(#341 跟进语义);
    * scope 四元组(``instrument_type`` / ``exchange`` / ``listing_boards`` /
      ``symbols``)经共享解析 ``normalize_sync_scope`` 校验归一(#392:与
      dataset_sync 同一函数,#385 口径;``symbols`` 空列表拒绝 —— 缺省不传 =
      全池,显式空列表几乎必然是调用方笔误,fail-visible);
    * ``tushare`` x ``etf`` 字面量预检(执行器基于 DB 行的
      ``tushare_scope_mismatch`` 校验保留,#341 边界不变;期货 #395 起放行
      —— fut_daily 2000 积分档实测可调,#267「另档积分」旧记录作废)。
    """

    from finboard_backtest.background_jobs.dataset_sync.scope import (
        ScopeValueError,
        normalize_sync_scope,
    )

    unknown = sorted(set(payload) - _BULK_DOWNLOAD_ALLOWED_KEYS)
    if unknown:
        raise PayloadContractError(
            "unknown_payload_key",
            f"未知 payload 键: {unknown};已知键: "
            f"{sorted(_BULK_DOWNLOAD_ALLOWED_KEYS)}",
        )

    market = payload.get("market")
    if not isinstance(market, str) or not market:
        raise PayloadContractError(
            "missing_required_field", "market 必填(非空字符串,如 a_share / future)"
        )

    raw_source = payload.get("source")
    source: str | None = None
    if raw_source is not None and raw_source != "":
        if not isinstance(raw_source, str) or raw_source.strip().lower() not in (
            BULK_DOWNLOAD_SOURCES
        ):
            raise PayloadContractError(
                "invalid_field_value",
                f"不支持的 source: {raw_source!r};可用: "
                f"{sorted(BULK_DOWNLOAD_SOURCES)};空 / 缺省 = 回落配置默认源",
            )
        source = raw_source.strip().lower()

    _require_date(payload, "start")

    try:
        scope = normalize_sync_scope(
            exchange=payload.get("exchange"),
            listing_boards=payload.get("listing_boards"),
            instrument_type=payload.get("instrument_type"),
            symbols=payload.get("symbols"),
        )
    except ScopeValueError as exc:
        raise PayloadContractError("invalid_field_value", str(exc)) from exc

    symbols = payload.get("symbols")
    if symbols is not None and not scope.symbols:
        # 空列表(或全空串)拒绝;缺省不传 = 全池不过滤子集。
        raise PayloadContractError(
            "empty_symbol_pool",
            "symbols 不能为空列表(缺省不传 = 全池不过滤子集;"
            "子集重跑请列出失败标的代码,如 000001.SZ)",
        )

    if source == "tushare" and scope.instrument_type == "etf":
        raise PayloadContractError(
            "tushare_scope_mismatch",
            "Tushare 批量任务不支持 ETF(复权口径对齐未定稿,#341);"
            "请选 akshare 源(期货已放行:fut_daily,#395)",
        )


#: kind → 入队期 payload 校验器。新 kind 在此注册即可被 REST + MCP + 执行器
#: 重放三方共用。
PAYLOAD_CONTRACTS: dict[str, Callable[[Mapping[str, Any]], None]] = {
    "dataset_sync": validate_dataset_sync_payload,
    "bulk_download": validate_bulk_download_payload,
}


def validate_job_payload(kind: str, payload: Mapping[str, Any] | None) -> None:
    """按 kind 校验 payload;未注册 kind 直接放行(payload 自由结构向后兼容)。"""

    if payload is None:
        return
    validator = PAYLOAD_CONTRACTS.get(kind)
    if validator is not None:
        validator(payload)


__all__ = [
    "BULK_DOWNLOAD_SOURCES",
    "PAYLOAD_CONTRACTS",
    "PayloadContractError",
    "validate_bulk_download_payload",
    "validate_dataset_sync_payload",
    "validate_job_payload",
]
