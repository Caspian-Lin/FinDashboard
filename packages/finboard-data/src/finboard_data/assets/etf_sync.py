"""ETF 元数据同步源与编排(issue #97)。

分层:

* :class:`EtfMetadataSource` —— Protocol,定义从上游拉取标准化
  :class:`~finboard_data.assets.classifier.EtfRawFacts` 的契约;
* :class:`AkShareEtfMetadataSource` —— akshare 具体实现(批量 + 按需详情);
* :class:`EtfMetadataSync` —— 编排器,调用 source → classifier → 返回
  :class:`~finboard_data.assets.classifier.EtfClassification` 列表。

akshare 字段会随上游变化(字段漂移),实现层用列名适配 + try/except 容错;
核心分类逻辑在 :mod:`~finboard_data.assets.classifier` 中,不依赖网络,
可用 fixture 完整测试。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

import structlog

from finboard_data.assets.classifier import (
    EtfClassification,
    EtfClassifier,
    EtfRawFacts,
)

logger = structlog.get_logger(__name__)


@runtime_checkable
class EtfMetadataSource(Protocol):
    """ETF 原始事实数据源契约。"""

    async def fetch_etf_facts(self) -> list[EtfRawFacts]:
        """批量获取全市场 ETF 原始事实(代码 / 名称 / 基金类型 / 跟踪标的 ...)。"""
        ...

    async def fetch_fund_detail(self, code: str) -> EtfRawFacts | None:
        """按需获取单基金档案详情(用于补充批量接口缺失的字段)。"""
        ...


class AkShareEtfMetadataSource:
    """akshare ETF 元数据源。

    批量接口 ``fund_name_em()`` 提供基金类型 / 投资类型;
    按需接口 ``fund_overview_em(code)`` 提供跟踪标的 / 费率 / 成立日。
    所有调用走 ``asyncio.to_thread`` 以避免阻塞事件循环。
    """

    def __init__(self, *, detail_concurrency: int = 4) -> None:
        self._sem = asyncio.Semaphore(detail_concurrency)

    async def fetch_etf_facts(self) -> list[EtfRawFacts]:
        result = await asyncio.to_thread(self._fetch_name_list_sync)
        logger.info("etf_sync.fetch_facts", count=len(result))
        return result

    async def fetch_fund_detail(self, code: str) -> EtfRawFacts | None:
        async with self._sem:
            return await asyncio.to_thread(self._fetch_overview_sync, code)

    def _fetch_name_list_sync(self) -> list[EtfRawFacts]:
        import akshare as ak

        df = ak.fund_name_em()
        col_map = _resolve_columns(df.columns, _NAME_COLUMN_CANDIDATES)
        result: list[EtfRawFacts] = []
        for _, row in df.iterrows():
            fund_type = _cell_str(row, col_map.get("fund_type"))
            invest_type = _cell_str(row, col_map.get("invest_type"))
            code = _cell_str(row, col_map.get("code"))
            name = _cell_str(row, col_map.get("name"))
            if code is None:
                continue
            if _is_etf_fund_type(fund_type, name):
                result.append(
                    EtfRawFacts(
                        code=code,
                        name=name or code,
                        fund_type=fund_type,
                        invest_type=invest_type,
                    )
                )
        return result

    def _fetch_overview_sync(self, code: str) -> EtfRawFacts | None:
        import akshare as ak

        fund_code = code.split(".", 1)[0]
        fetcher = getattr(ak, "fund_overview_em", None)
        if fetcher is None:
            logger.warning("etf_sync.fund_overview_unavailable", code=code)
            return None
        try:
            df = fetcher(symbol=fund_code)
        except Exception as exc:
            logger.warning("etf_sync.overview_error", code=code, error=str(exc))
            return None
        if df is None or df.empty:
            return None
        col_map = _resolve_columns(df.columns, _OVERVIEW_COLUMN_CANDIDATES)
        row = df.iloc[0]
        return EtfRawFacts(
            code=code,
            name=_cell_str(row, col_map.get("name")) or code,
            fund_type=_cell_str(row, col_map.get("fund_type")),
            invest_type=_cell_str(row, col_map.get("invest_type")),
            tracked_index=_cell_str(row, col_map.get("tracked_index")),
            management_fee_rate=_parse_decimal(
                _cell_str(row, col_map.get("management_fee_rate"))
            ),
            custody_fee_rate=_parse_decimal(
                _cell_str(row, col_map.get("custody_fee_rate"))
            ),
            inception_date=_parse_date(_cell_str(row, col_map.get("inception_date"))),
        )


_NAME_COLUMN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "code": ("基金代码", "code"),
    "name": ("基金简称", "name", "名称"),
    "fund_type": ("基金类型", "type"),
    "invest_type": ("投资类型", "投资类型(InvestType)"),
}

_OVERVIEW_COLUMN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "name": ("基金简称", "名称", "name"),
    "fund_type": ("基金类型", "类型", "type"),
    "invest_type": ("投资类型", "invest_type"),
    "tracked_index": ("跟踪标的", "跟踪指数", "业绩比较基准"),
    "management_fee_rate": ("管理费率", "管理费", "management_fee"),
    "custody_fee_rate": ("托管费率", "托管费", "custody_fee"),
    "inception_date": ("成立日期", "成立日", "inception_date"),
}


def _resolve_columns(
    columns: Any,
    candidates: dict[str, tuple[str, ...]],
) -> dict[str, str | None]:
    col_list: list[str] = [str(c) for c in columns]
    result: dict[str, str | None] = {}
    for key, options in candidates.items():
        found: str | None = None
        for opt in options:
            for col in col_list:
                if col == opt:
                    found = col
                    break
            if found:
                break
        result[key] = found
    return result


def _cell_str(row: Any, column: str | None) -> str | None:
    if column is None:
        return None
    try:
        value = getattr(row, column)
    except (AttributeError, KeyError):
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_etf_fund_type(fund_type: str | None, name: str | None) -> bool:
    if fund_type and "ETF" in fund_type.upper():
        return True
    return bool(name and "ETF" in name.upper())


def _parse_decimal(text: str | None) -> Decimal | None:
    from decimal import InvalidOperation

    if text is None:
        return None
    cleaned = text.rstrip("%").strip()
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def _parse_date(text: str | None) -> Any:
    from datetime import datetime

    if text is None:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


class EtfMetadataSync:
    """ETF 元数据同步编排器(source → classifier → classification)。

    用法::

        sync = EtfMetadataSync(AkShareEtfMetadataSource(), EtfClassifier())
        classifications = await sync.discover_and_classify()
    """

    def __init__(self, source: EtfMetadataSource, classifier: EtfClassifier) -> None:
        self._source = source
        self._classifier = classifier

    async def discover_and_classify(
        self,
        *,
        enrich_codes: list[str] | None = None,
    ) -> list[EtfClassification]:
        """批量发现 ETF 并分类。

        ``enrich_codes`` 指定的标的会额外拉取单基金档案补充跟踪标的 / 费率,
        其余仅依赖批量接口的基金类型和名称(弱证据,可能进入 needs_review)。
        """
        facts_list = await self._source.fetch_etf_facts()
        if enrich_codes:
            wanted = {c.split(".", 1)[0] for c in enrich_codes}
            enriched: dict[str, EtfRawFacts] = {}
            for facts in facts_list:
                if facts.code.split(".", 1)[0] in wanted:
                    detail = await self._source.fetch_fund_detail(facts.code)
                    if detail is not None:
                        enriched[facts.code] = detail
            facts_list = [
                enriched.get(f.code, f) if f.code in enriched else _merge(f, enriched)
                for f in facts_list
            ]
        return self._classifier.classify_batch(facts_list)

    async def classify_one(self, code: str) -> EtfClassification | None:
        """同步单只 ETF(拉取详情 + 分类)。"""
        detail = await self._source.fetch_fund_detail(code)
        if detail is None:
            return None
        return self._classifier.classify(detail)


def _merge(base: EtfRawFacts, enriched: dict[str, EtfRawFacts]) -> EtfRawFacts:
    detail = enriched.get(base.code)
    if detail is None:
        return base
    return EtfRawFacts(
        code=base.code,
        name=detail.name or base.name,
        fund_type=detail.fund_type or base.fund_type,
        invest_type=detail.invest_type or base.invest_type,
        tracked_index=detail.tracked_index or base.tracked_index,
        management_fee_rate=detail.management_fee_rate or base.management_fee_rate,
        custody_fee_rate=detail.custody_fee_rate or base.custody_fee_rate,
        inception_date=detail.inception_date or base.inception_date,
    )


__all__ = [
    "AkShareEtfMetadataSource",
    "EtfMetadataSource",
    "EtfMetadataSync",
]
