"""ETF 多维分类纯规则组件(issue #97)。

分类器不接触任何网络 / SDK —— 只接受标准化的 :class:`EtfRawFacts`,
输出 :class:`EtfClassification`。规则版本化(``ETF_CLASSIFIER_VERSION``),
便于回溯和升级。弱证据(名称关键词)不直接决定会影响成交规则的
``execution_profile``,只降低置信度或进入 ``needs_review``。

多维分类维度(issue #97):

* ``underlying_asset_class`` —— equity / fixed_income / commodity / cash;
* ``underlying_market`` —— domestic / hk / overseas / global;
* ``strategy_type`` —— index / active;
* ``execution_profile`` —— 决定 T+N / 印花税 / 手数 的权威维度。

159010 验收样例:恒生港股通科技 ETF,基金类型"股票指数"但跟踪港股,
分类结果为 ``asset_class=equity``、``market=hk``、``strategy=index``、
``execution=cross_border_etf``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from finboard_shared.types import (
    AssetClass,
    EtfCategory,
    EtfExecutionProfile,
    EtfStrategyType,
    ReviewStatus,
    UnderlyingMarket,
    etf_category_from_execution_profile,
)

ETF_CLASSIFIER_VERSION = "v1"
"""分类器规则版本;推导逻辑变更时递增。"""

_HIGH_CONFIDENCE = Decimal("0.8")
"""自动采用阈值;达到即 ``auto_adopted``,否则 ``needs_review``。

国内 A 股 ETF 的标的市场默认为 domestic(置信度 0.8),上游基金类型明确时
即可达到阈值自动采用;只有名称关键词等弱证据的场景置信度低于此阈值。"""

_HK_KEYWORDS = ("恒生", "港股", "港股通")
_OVERSEAS_KEYWORDS = (
    "纳斯达克", "纳指", "标普", "美国", "美股", "日经", "日本", "东证",
    "德国", "法国", "英国", "亚太", "海外", "中概",
)
_COMMODITY_KEYWORDS = ("黄金", "白银", "商品", "原油", "有色", "豆粕")
_BOND_KEYWORDS = ("国债", "债券", "政金", "信用", "利率")
_MONEY_KEYWORDS = ("货币", "保证金", "华宝添益", "理财")

_QDII_HINTS = ("QDII", "qdii")


@dataclass(frozen=True, slots=True)
class EtfRawFacts:
    """从上游数据源(akshare 等)提取的标准化原始事实。

    分类器只依赖此结构,不直接消费供应商 DataFrame,便于 fixture 测试。
    所有字段均可空 —— 空字段意味着上游缺失,会降低置信度。
    """

    code: str
    name: str
    fund_type: str | None = None
    invest_type: str | None = None
    tracked_index: str | None = None
    management_fee_rate: Decimal | None = None
    custody_fee_rate: Decimal | None = None
    inception_date: object = None

    def _has_fund_type(self, *keywords: str) -> bool:
        if self.fund_type is None:
            return False
        lowered = self.fund_type.lower()
        return any(kw.lower() in lowered for kw in keywords)

    def _name_or_index_has(self, *keywords: str) -> bool:
        blob = f"{self.name} {self.tracked_index or ''}"
        return any(kw in blob for kw in keywords)


@dataclass(frozen=True, slots=True)
class EtfClassification:
    """分类器输出 —— 多维分类 + 证据 + 置信度 + 审核状态。

    ``confidence`` 取值 0~1;``evidence`` 是可读的人类友好规则摘要,
    持久化到 ``etf_metadata.evidence`` 供复核队列展示。
    """

    code: str
    execution_profile: EtfExecutionProfile
    underlying_asset_class: AssetClass
    underlying_market: UnderlyingMarket
    strategy_type: EtfStrategyType
    category: EtfCategory
    confidence: Decimal
    review_status: ReviewStatus
    rule_version: str = ETF_CLASSIFIER_VERSION
    evidence: tuple[str, ...] = field(default_factory=tuple)
    tracked_index: str | None = None


def classify_etf(facts: EtfRawFacts) -> EtfClassification:
    """对单只 ETF 执行多维分类。

    fail-closed 原则:如果所有维度都无法推导出高置信度结果,
    返回 ``review_status=needs_review`` 并给出 ``execution_profile`` 的最佳猜测,
    但该记录不会自动采用,必须人工确认。
    """
    evidence: list[str] = []
    confidence_parts: list[Decimal] = []

    # ---- 1. underlying_asset_class ----
    asset_class, asset_conf, asset_ev = _derive_asset_class(facts)
    evidence.extend(asset_ev)
    confidence_parts.append(asset_conf)

    # ---- 2. underlying_market ----
    market, market_conf, market_ev = _derive_market(facts)
    evidence.extend(market_ev)
    confidence_parts.append(market_conf)

    # ---- 3. strategy_type ----
    strategy, strat_conf, strat_ev = _derive_strategy(facts)
    evidence.extend(strat_ev)
    confidence_parts.append(strat_conf)

    # ---- 4. execution_profile(综合以上维度)----
    if asset_class is AssetClass.FIXED_INCOME:
        execution_profile = EtfExecutionProfile.BOND_ETF
    elif asset_class is AssetClass.CASH:
        execution_profile = EtfExecutionProfile.MONEY_MARKET_ETF
    elif asset_class is AssetClass.COMMODITY:
        execution_profile = EtfExecutionProfile.COMMODITY_ETF
    elif market is not UnderlyingMarket.DOMESTIC:
        execution_profile = EtfExecutionProfile.CROSS_BORDER_ETF
    else:
        execution_profile = EtfExecutionProfile.DOMESTIC_EQUITY_ETF

    confidence = min(confidence_parts) if confidence_parts else Decimal("0")

    # T+0 推导:跨境 / 商品 / 债券 / 货币均允许当日回转
    review = (
        ReviewStatus.AUTO_ADOPTED
        if confidence >= _HIGH_CONFIDENCE
        else ReviewStatus.NEEDS_REVIEW
    )

    return EtfClassification(
        code=facts.code,
        execution_profile=execution_profile,
        underlying_asset_class=asset_class,
        underlying_market=market,
        strategy_type=strategy,
        category=etf_category_from_execution_profile(execution_profile),
        confidence=confidence,
        review_status=review,
        rule_version=ETF_CLASSIFIER_VERSION,
        evidence=tuple(evidence),
        tracked_index=facts.tracked_index,
    )


def _derive_asset_class(
    facts: EtfRawFacts,
) -> tuple[AssetClass, Decimal, list[str]]:
    if facts._has_fund_type("债券", "政金", "国债"):
        return AssetClass.FIXED_INCOME, Decimal("0.9"), [
            f"基金类型「{facts.fund_type}」→ 固定收益"
        ]
    if facts._has_fund_type("货币"):
        return AssetClass.CASH, Decimal("0.9"), [
            f"基金类型「{facts.fund_type}」→ 货币"
        ]
    if facts._has_fund_type("商品", "黄金"):
        return AssetClass.COMMODITY, Decimal("0.85"), [
            f"基金类型「{facts.fund_type}」→ 商品"
        ]
    if facts._has_fund_type("股票", "指数"):
        return AssetClass.EQUITY, Decimal("0.9"), [
            f"基金类型「{facts.fund_type}」→ 权益"
        ]
    if facts._name_or_index_has(*_BOND_KEYWORDS):
        return AssetClass.FIXED_INCOME, Decimal("0.6"), [
            "名称/跟踪标的含债券关键词 → 固定收益(弱证据)"
        ]
    if facts._name_or_index_has(*_MONEY_KEYWORDS):
        return AssetClass.CASH, Decimal("0.6"), [
            "名称含货币关键词 → 货币(弱证据)"
        ]
    if facts._name_or_index_has(*_COMMODITY_KEYWORDS):
        return AssetClass.COMMODITY, Decimal("0.6"), [
            "名称含商品关键词 → 商品(弱证据)"
        ]
    if facts._name_or_index_has("ETF", "指数"):
        return AssetClass.EQUITY, Decimal("0.5"), [
            "名称含 ETF/指数 → 权益(弱证据)"
        ]
    return AssetClass.EQUITY, Decimal("0.2"), [
        "无明确资产类别证据 → 默认权益(低置信度)"
    ]


def _derive_market(
    facts: EtfRawFacts,
) -> tuple[UnderlyingMarket, Decimal, list[str]]:
    is_qdii = facts._has_fund_type(*_QDII_HINTS) or any(
        h in (facts.invest_type or "").upper() for h in _QDII_HINTS
    )
    if facts._name_or_index_has(*_HK_KEYWORDS):
        return UnderlyingMarket.HK, Decimal("0.85"), [
            "名称/跟踪标的含港股关键词 → 港股"
        ]
    if is_qdii and facts._name_or_index_has(*_OVERSEAS_KEYWORDS):
        return UnderlyingMarket.OVERSEAS, Decimal("0.9"), [
            f"QDII + 海外关键词「{facts.tracked_index or facts.name}」→ 海外"
        ]
    if facts._name_or_index_has(*_OVERSEAS_KEYWORDS):
        return UnderlyingMarket.OVERSEAS, Decimal("0.7"), [
            "名称/跟踪标的含海外关键词 → 海外"
        ]
    if is_qdii:
        return UnderlyingMarket.OVERSEAS, Decimal("0.5"), [
            "QDII 但无明确地区 → 推断海外(弱证据)"
        ]
    return UnderlyingMarket.DOMESTIC, Decimal("0.8"), ["无跨境证据 → 国内"]


def _derive_strategy(
    facts: EtfRawFacts,
) -> tuple[EtfStrategyType, Decimal, list[str]]:
    if facts._has_fund_type("指数") or facts.tracked_index:
        ev = "基金类型含「指数」" if facts._has_fund_type("指数") else "存在跟踪标的"
        return EtfStrategyType.INDEX, Decimal("0.85"), [f"{ev} → 被动指数"]
    if facts._has_fund_type("股票型", "混合型", "主动"):
        return EtfStrategyType.ACTIVE, Decimal("0.7"), [
            f"基金类型「{facts.fund_type}」→ 主动管理"
        ]
    return EtfStrategyType.INDEX, Decimal("0.4"), ["无明确策略证据 → 默认指数(弱证据)"]


class EtfClassifier:
    """ETF 多维分类器(可注入 / 可 mock 的服务外观)。

    纯函数 ``classify_etf`` 的薄封装,便于在同步编排中作为依赖注入。
    """

    version: str = ETF_CLASSIFIER_VERSION

    def classify(self, facts: EtfRawFacts) -> EtfClassification:
        return classify_etf(facts)

    def classify_batch(self, facts_list: list[EtfRawFacts]) -> list[EtfClassification]:
        return [self.classify(f) for f in facts_list]


__all__ = [
    "ETF_CLASSIFIER_VERSION",
    "EtfClassification",
    "EtfClassifier",
    "EtfRawFacts",
    "classify_etf",
]
