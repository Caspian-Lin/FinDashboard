"""多资产标的元数据与生命周期模型(issue #58)。

设计原则:
* 所有 dataclass ``frozen=True``,创建后不可变 —— 防止运行时被意外修改;
* 货币 / 价格 / 数量字段一律 ``Decimal``;
* 时点化字段(``available_at``)严格区分"业务时间"与"可知时间",禁止未来信息泄漏;
* 不引用任何供应商 SDK 类型 —— Provider 在边界转换为本模块的 dataclass。

本模块**只读**,不触及交易红线(下单 / 持仓 / 风控)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from finboard_shared.types import (
    AdjustmentMethod,
    AssetClass,
    CouponFrequency,
    DatasetQualityStatus,
    EtfCategory,
    EtfExecutionProfile,
    EtfStrategyType,
    InstrumentType,
    LifecycleEventType,
    ListingStatus,
    Market,
    ReviewStatus,
    RollMethod,
    UnderlyingMarket,
)

ASSET_METADATA_VERSION = "v1"
"""本资产元数据契约版本;字段语义变更必须递增。

研究实验与回测运行在 ``DatasetManifest.version_stamp`` 中归档此版本,
使历史 run 不可与改动后的 run 横向比较。
"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _assert_not_empty(value: str, field_name: str) -> None:
    if not value:
        raise ValueError(f"{field_name} 不能为空")


@dataclass(frozen=True, slots=True)
class Instrument:
    """通用标的描述 —— 跨资产的公共字段。

    每类资产有独立的 ``*Metadata`` 子描述(ETF / Bond / Convertible / Futures);
    ``Instrument`` 是注册到 ``InstrumentRegistry`` 的入口,通过 ``code`` 唯一识别。
    """

    code: str  # 券商合约代码,如 510300.SH / IF2406.CFFEX / 113001.SH
    name: str
    market: Market
    instrument_type: InstrumentType
    exchange: str | None = None  # SSE / SZSE / CFFEX / SSE_BOND
    currency: str = "CNY"
    lot_size: Decimal | None = None  # 最小交易单位(股票 100 / ETF 100 / 国债 ETF 10 / 可转债 10 / 期货 1)
    price_tick: Decimal | None = None  # 价格步长
    multiplier: Decimal = Decimal("1")  # 合约乘数(期货 / 期权);股票 / ETF 为 1
    list_date: date | None = None
    delist_date: date | None = None
    status: ListingStatus = ListingStatus.UNKNOWN
    asset_class: AssetClass = AssetClass.EQUITY
    sector: str | None = None
    industry: str | None = None

    def __post_init__(self) -> None:
        _assert_not_empty(self.code, "code")
        if self.multiplier <= 0:
            raise ValueError("multiplier 必须为正")
        if self.lot_size is not None and self.lot_size <= 0:
            raise ValueError("lot_size 必须为正")
        if self.price_tick is not None and self.price_tick <= 0:
            raise ValueError("price_tick 必须为正")
        if (
            self.list_date is not None
            and self.delist_date is not None
            and self.list_date >= self.delist_date
        ):
            raise ValueError("list_date 必须早于 delist_date")

    @property
    def is_active(self) -> bool:
        """当前是否处于可交易状态(不校验停牌日历)。"""
        return self.status in (ListingStatus.ACTIVE, ListingStatus.UNKNOWN)

    @property
    def is_delisted(self) -> bool:
        return self.status is ListingStatus.DELISTED

    def was_listed_at(self, as_of: date) -> bool:
        """``as_of`` 当日是否在上市区间内(只看日期,不看停牌)。"""
        if self.list_date is not None and as_of < self.list_date:
            return False
        return not (self.delist_date is not None and as_of > self.delist_date)


@dataclass(frozen=True, slots=True)
class EtfMetadata:
    """ETF 子描述(issue #58 契约层,#97 多维分类扩展)。

    issue #97 引入正交多维分类:

    * ``execution_profile`` —— 交易规则权威维度(T+N / 印花税 / 手数);
    * ``underlying_market`` —— 标的地理范围(domestic / hk / overseas / global);
    * ``strategy_type`` —— 被动指数 vs 主动管理。

    旧 ``category``(:class:`EtfCategory`)降级为兼容派生值,由
    :func:`~finboard_shared.types.etf_category_from_execution_profile` 生成,
    仅为不破坏既有发布链路而保留。

    溯源字段(``source`` / ``source_updated_at`` / ``rule_version`` /
    ``confidence`` / ``review_status`` / ``evidence``)用于区分自动推导与
    人工覆盖,并支持 fail-closed 审核流程。
    """

    fund_code: str  # 基金代码(可能与交易代码不同)
    category: EtfCategory  # 兼容旧分类;由 execution_profile 派生
    execution_profile: EtfExecutionProfile | None = None  # #97 执行档位(权威)
    underlying_market: UnderlyingMarket = UnderlyingMarket.DOMESTIC  # #97 标的市场
    strategy_type: EtfStrategyType = EtfStrategyType.INDEX  # #97 策略类型
    underlying_index: str | None = None  # 跟踪指数代码,如 "000300.SH"
    management_fee_rate: Decimal | None = None  # 年管理费率
    custody_fee_rate: Decimal | None = None  # 年托管费率
    inception_date: date | None = None  # 基金成立日
    listing_date: date | None = None  # 上市日
    delisting_date: date | None = None
    tracking_error: Decimal | None = None  # 年化跟踪误差
    iopv_available: bool = False  # 是否提供 IOPV 参考价
    allows_t_plus_0: bool = False  # 跨境 / 货币 / 商品 / 债券 ETF 允许 T+0
    dividend_policy: str = "cash"  # cash / reinvest
    underlying_asset_class: AssetClass = AssetClass.EQUITY
    # -- 溯源与审核(issue #97)--
    source: str = "manual"  # akshare / manual / catalog
    source_updated_at: datetime | None = None  # 上游事实观测时间
    rule_version: str = ""  # 分类器规则版本(自动推导时填)
    confidence: Decimal = Decimal("0")  # 置信度 0~1
    review_status: ReviewStatus = ReviewStatus.NEEDS_REVIEW
    evidence: tuple[str, ...] = ()  # 规则证据摘要(可读)
    manual_override: bool = False  # 是否被人工覆盖


@dataclass(frozen=True, slots=True)
class BondMetadata:
    """交易所债券子描述(国债 / 政策金融债 / 企业债)。

    10万-50万元组合优先通过 **债券 ETF** 获取固收暴露,直接现券单列能力边界。
    """

    face_value: Decimal = Decimal("100")  # 面值
    coupon_rate: Decimal | None = None  # 票面利率(年化)
    coupon_frequency: CouponFrequency = CouponFrequency.ANNUAL
    issue_date: date | None = None
    maturity_date: date | None = None
    issuer: str | None = None
    credit_rating: str | None = None  # AAA / AA+ / ...
    credit_entity_type: str | None = None  # treasury / policy_bank / soe / corporate
    duration_years: Decimal | None = None  # 修正久期(年)
    yield_to_maturity: Decimal | None = None  # 到期收益率(年化)

    @property
    def is_treasury(self) -> bool:
        return self.credit_entity_type == "treasury"

    @property
    def years_to_maturity(self) -> Decimal | None:
        if self.maturity_date is None:
            return None
        days = (self.maturity_date - date.today()).days
        return Decimal(max(days, 0)) / Decimal("365")


@dataclass(frozen=True, slots=True)
class ConvertibleMetadata:
    """可转债子描述(issue #58)。"""

    underlying_stock_code: str  # 正股代码,如 "600519.SH"
    conversion_price: Decimal  # 初始转股价
    conversion_ratio: Decimal | None = None  # 每张可转股数(=100/转股价)
    conversion_premium: Decimal | None = None  # 当前转股溢价率(快照)
    issue_date: date | None = None
    maturity_date: date | None = None
    coupon_schedule: tuple[Decimal, ...] = ()  # 各年票面利率(递增)
    redemption_yield: Decimal | None = None  # 到期税后收益率
    forced_redeem_trigger: Decimal | None = None  # 强赎触发条件(正股价格/转股价 >= 1.30 持续 N 日)
    put_back_trigger: Decimal | None = None  # 回售触发条件(正股价格/转股价 <= 0.70 持续 N 日)
    downward_revision_trigger: Decimal | None = None  # 下修触发条件

    def __post_init__(self) -> None:
        if self.conversion_price <= 0:
            raise ValueError("conversion_price 必须为正")


@dataclass(frozen=True, slots=True)
class FuturesContract:
    """期货合约子描述(issue #58 契约层,#64 完整实现)。

    每个 ``FuturesContract`` 描述一份**具体月份合约**(如 IF2406.CFFEX);
    连续主力合约由 :class:`ContinuousFuturesRule` 拼接。
    """

    contract_code: str  # IF2406.CFFEX
    underlying_symbol: str  # 标的(指数代码 / 国债品种)
    exchange: str  # CFFEX / SHFE / DCE / CZCE / GFEX
    multiplier: Decimal  # 合约乘数(IF=300、T=10000)
    margin_rate: Decimal  # 保证金率
    price_limit_pct: Decimal  # 涨跌停百分比
    price_tick: Decimal  # 最小变动价位
    listing_date: date | None = None
    last_trade_date: date | None = None  # 最后交易日
    delivery_date: date | None = None  # 交割日(通常 = 最后交易日 后第 3 个交易日)
    delivery_method: str = "cash"  # cash / physical
    settle_price: Decimal | None = None  # 结算价(快照)
    open_interest: Decimal | None = None  # 持仓量(快照)

    def __post_init__(self) -> None:
        _assert_not_empty(self.contract_code, "contract_code")
        if self.multiplier <= 0:
            raise ValueError("multiplier 必须为正")
        if not (Decimal("0") < self.margin_rate <= Decimal("1")):
            raise ValueError("margin_rate 必须落在 (0, 1]")
        if self.price_limit_pct <= 0:
            raise ValueError("price_limit_pct 必须为正")
        if self.price_tick <= 0:
            raise ValueError("price_tick 必须为正")


@dataclass(frozen=True, slots=True)
class ContinuousFuturesRule:
    """连续期货拼接规则 —— 可审计地还原未拼接价格(issue #58 验收)。"""

    series_id: str  # 连续序列标识,如 "IF" / "T" / "CU"
    roll_method: RollMethod
    adjustment_method: AdjustmentMethod
    roll_day_offset: int = 0  # 交割月前 N 个交易日换月(roll_method=scheduled)
    active: bool = True
    rule_version: str = "v1"
    description: str = ""

    def __post_init__(self) -> None:
        _assert_not_empty(self.series_id, "series_id")


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """时点化的公司行为 / 合约事件(issue #58)。

    红线:``available_at`` 必须 >= ``effective_date`` 当日开盘;禁止用未来公告
    回填历史决策(见 phase1_doc.md §3.2 时点化原则)。
    """

    symbol: str
    event_type: LifecycleEventType
    effective_date: date  # 生效日期(业务时间)
    available_at: datetime  # 公告 / 可知时间
    source: str
    dataset_version: str
    details: dict[str, object] = field(default_factory=dict)  # 事件特定字段
    observed_at: datetime | None = None  # 数据观测时间(默认=available_at)

    def __post_init__(self) -> None:
        _assert_not_empty(self.symbol, "symbol")
        _assert_not_empty(self.source, "source")
        _assert_not_empty(self.dataset_version, "dataset_version")
        effective_dt = datetime.combine(
            self.effective_date, datetime.min.time(), tzinfo=self.available_at.tzinfo or UTC
        )
        if self.available_at < effective_dt:
            raise ValueError(
                f"available_at {self.available_at} 不能早于 effective_date "
                f"{self.effective_date} 的开盘(防止未来信息泄漏)"
            )


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    """数据集发布清单(issue #58 验收:不能因文件存在就认定数据可用)。

    一份 ``DatasetManifest`` 对应一次 ``(dataset_name, source, version)`` 同步;
    质量校验不通过的数据集 status=FAILED,回测引擎拒绝加载。
    """

    dataset_name: str  # instrument_profiles / daily_bars / convertibles / futures_chain ...
    source: str
    version: str  # 版本号(如 "2024Q1" / "20240101" / git short sha)
    start_date: date | None = None  # 数据起始日
    end_date: date | None = None  # 数据结束日
    row_count: int = 0
    symbol_count: int = 0
    coverage_pct: Decimal = Decimal("0")  # 覆盖率(0~1),与目标 universe 对比
    gaps: tuple[tuple[date, date], ...] = ()  # 缺口日期区间
    checksum: str = ""  # 内容校验和(sha256)
    quality_status: DatasetQualityStatus = DatasetQualityStatus.UNKNOWN
    quality_report: dict[str, object] = field(default_factory=dict)
    published_at: datetime = field(default_factory=_utcnow)
    code_version: str = ""  # 抓取代码版本(git sha)

    def __post_init__(self) -> None:
        _assert_not_empty(self.dataset_name, "dataset_name")
        _assert_not_empty(self.source, "source")
        _assert_not_empty(self.version, "version")

    @property
    def is_usable(self) -> bool:
        """质量门:``passed`` / ``warnings`` 可用,其他拒绝。"""
        return self.quality_status in (
            DatasetQualityStatus.PASSED,
            DatasetQualityStatus.WARNINGS,
        )


@dataclass(frozen=True, slots=True)
class ContinuousFuturesPoint:
    """连续期货序列中的单点(可还原原合约)。"""

    timestamp: datetime
    price: Decimal  # 调整后价格(按 adjustment_method)
    raw_price: Decimal  # 原始未调整价格
    contract_code: str  # 当时主力合约代码
    volume: Decimal = Decimal("0")
    open_interest: Decimal | None = None
    roll_flag: bool = False  # 是否为换月日


@dataclass(frozen=True, slots=True)
class ContinuousFuturesSeries:
    """拼接后的连续期货序列 —— 保留可审计的换月表。"""

    series_id: str
    rule: ContinuousFuturesRule
    points: tuple[ContinuousFuturesPoint, ...]
    roll_table: tuple[tuple[date, str, str], ...]  # (roll_date, from_contract, to_contract)
    data_source: str = ""
    dataset_version: str = ""

    def __post_init__(self) -> None:
        if not self.points:
            raise ValueError("ContinuousFuturesSeries.points 不能为空")


__all__ = [
    "ASSET_METADATA_VERSION",
    "BondMetadata",
    "ContinuousFuturesPoint",
    "ContinuousFuturesRule",
    "ContinuousFuturesSeries",
    "ConvertibleMetadata",
    "DatasetManifest",
    "EtfMetadata",
    "FuturesContract",
    "Instrument",
    "LifecycleEvent",
]
