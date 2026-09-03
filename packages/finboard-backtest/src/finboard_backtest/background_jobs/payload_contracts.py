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

#: ``research_data_sync`` 数据集白名单(唯一事实来源;执行器从这里 re-export,
#: 对应 ResearchDataset 枚举的摄取入口,#251 加 name_changes,#265 加
#: convertible_profiles —— 转债条款快照,写主数据 convertible_metadata,
#: 不走 research_* 批次,与 name_changes 同风格)。
RESEARCH_DATA_SYNC_DATASETS: frozenset[str] = frozenset(
    {
        "profiles",
        "name_changes",
        "convertible_profiles",
        "daily_metrics",
        "financial_indicators",
        "industry_memberships",
    }
)

_RESEARCH_DATA_SYNC_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"datasets", "start_date", "end_date", "symbols"}
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


def validate_research_data_sync_payload(payload: Mapping[str, Any]) -> None:
    """校验 ``kind=research_data_sync`` 的 payload(入队期契约,issue #260)。

    * 未知键拒绝 —— ``data_types`` 类拼写错误入队即报并列出已知键,
      不再静默忽略后按缺省全数据集执行;
    * ``start_date`` / ``end_date`` 必填且为 ISO 日期,``start <= end``;
    * ``datasets`` 枚举校验(缺省 = 全部五类);
    * ``symbols`` 须为字符串列表(缺省可空);逐标的数据集在「未提供 symbols
      且 datasets 不含 profiles」时拒绝 —— symbol 池将解析为空、任务静默零迭代。
    """

    unknown = sorted(set(payload) - _RESEARCH_DATA_SYNC_ALLOWED_KEYS)
    if unknown:
        raise PayloadContractError(
            "unknown_payload_key",
            f"未知 payload 键: {unknown};已知键: "
            f"{sorted(_RESEARCH_DATA_SYNC_ALLOWED_KEYS)}"
            "(常见拼写错误:data_types 不存在,数据集参数名为 datasets)",
        )

    raw_datasets = payload.get("datasets")
    if raw_datasets is None:
        datasets = RESEARCH_DATA_SYNC_DATASETS
    else:
        if not isinstance(raw_datasets, list) or not all(
            isinstance(item, str) for item in raw_datasets
        ):
            raise PayloadContractError(
                "invalid_field_value", "datasets 必须是字符串列表"
            )
        unknown_datasets = sorted(set(raw_datasets) - RESEARCH_DATA_SYNC_DATASETS)
        if unknown_datasets:
            raise PayloadContractError(
                "invalid_field_value",
                f"不支持的 datasets: {unknown_datasets};"
                f" 可用: {sorted(RESEARCH_DATA_SYNC_DATASETS)}",
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

    per_symbol = _RESEARCH_DATA_SYNC_PER_SYMBOL & datasets
    if per_symbol and not any(s for s in (symbols or [])) and "profiles" not in datasets:
        raise PayloadContractError(
            "empty_symbol_pool",
            f"datasets 含逐标的同步 {sorted(per_symbol)} 但未提供 symbols 且"
            " datasets 不含 profiles —— symbol 池将解析为空,任务静默零迭代;"
            "修复:提供 symbols 列表,或把 profiles 加入 datasets(以其同步"
            "结果作为全市场 symbol 池)",
        )


#: kind → 入队期 payload 校验器。新 kind 在此注册即可被 REST + MCP + 执行器
#: 重放三方共用。
PAYLOAD_CONTRACTS: dict[str, Callable[[Mapping[str, Any]], None]] = {
    "research_data_sync": validate_research_data_sync_payload,
}


def validate_job_payload(kind: str, payload: Mapping[str, Any] | None) -> None:
    """按 kind 校验 payload;未注册 kind 直接放行(payload 自由结构向后兼容)。"""

    if payload is None:
        return
    validator = PAYLOAD_CONTRACTS.get(kind)
    if validator is not None:
        validator(payload)


__all__ = [
    "PAYLOAD_CONTRACTS",
    "RESEARCH_DATA_SYNC_DATASETS",
    "PayloadContractError",
    "validate_job_payload",
    "validate_research_data_sync_payload",
]
