"""统一枚举。

约定:全部继承 ``StrEnum``,便于序列化为字符串与 JSON 字段对齐;
跨网络/持久化场景下永远以 ``.value`` 为准。
"""

from __future__ import annotations

from enum import StrEnum


class Market(StrEnum):
    A_SHARE = "a_share"
    HK = "hk"
    US = "us"
    FUTURE = "future"


class InstrumentType(StrEnum):
    """资产类型。

    决定撮合规则 / 费用 / 手数 / 持仓方向;与 ``Market`` 正交(同一市场内可含多种类型)。
    研究 / 回测域接入新资产类(issue #58)时,**新增类型必须显式在
    ``AssetRuleTable`` 中注册规则**,未注册的 (market, type) 在回测中 fail closed。
    """

    STOCK = "stock"
    ETF = "etf"
    INDEX = "index"
    FUTURES = "futures"
    BOND = "bond"  # 交易所债券(国债 / 政策金融债 / 企业债),#58
    CONVERTIBLE = "convertible"  # 可转债,#58


class AssetClass(StrEnum):
    """跨资产大类 —— 用于组合层风险预算和资产配置(issue #59 消费)。

    一个 ``InstrumentType`` 唯一映射到一个 ``AssetClass``:
    STOCK/ETF(股票 ETF) → EQUITY;BOND/BOND_ETF → FIXED_INCOME;
    CONVERTIBLE → CONVERTIBLE;FUTURES → DERIVATIVE。
    """

    EQUITY = "equity"
    FIXED_INCOME = "fixed_income"
    CONVERTIBLE = "convertible"
    COMMODITY = "commodity"
    CASH = "cash"
    DERIVATIVE = "derivative"


class EtfCategory(StrEnum):
    """ETF 子分类 —— 决定 T+0 / 印花税 / 涨跌停差异(issue #58)。

    ``InstrumentType.ETF`` 之下进一步细分,与 ``AssetRule`` 一一对应。
    """

    EQUITY = "equity"  # 股票 ETF:T+1、卖出印花税万 5
    CROSS_BORDER = "cross_border"  # 跨境 ETF:T+0、免印花税
    BOND = "bond"  # 国债 / 政策金融债 ETF:免印花税、10 份/手
    MONEY_MARKET = "money_market"  # 货币 ETF:T+0、无佣金
    COMMODITY = "commodity"  # 黄金 / 商品 ETF
    INDEX = "index"  # 指数 ETF(宽基 / 行业)


class ListingStatus(StrEnum):
    """标的上市状态。"""

    ACTIVE = "active"  # 正常交易
    SUSPENDED = "suspended"  # 停牌(临时或长期)
    DELISTED = "delisted"  # 已退市
    PENDING = "pending"  # 待上市(已公告未挂牌)
    UNKNOWN = "unknown"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class PositionSide(StrEnum):
    """持仓方向。

    A 股一律为 LONG;期货区分多空,允许平仓时指定对应方向。
    """

    LONG = "long"
    SHORT = "short"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"


class TimeInForce(StrEnum):
    GFD = "GFD"  # Good-for-day,默认
    IOC = "IOC"  # Immediate-or-cancel
    FOK = "FOK"  # Fill-or-kill


class OrderStatus(StrEnum):
    """订单状态机(见 phase1_doc.md §4.2)。

    状态迁移图::

        CREATED → RISK_CHECKED → SUBMITTING → SUBMITTED → ACKNOWLEDGED
                                                        ├─ PARTIALLY_FILLED → ...
                                                        ├─ FILLED            (终态)
                                                        ├─ CANCEL_PENDING → CANCELLED (终态)
                                                        ├─ REJECTED           (终态)
                                                        └─ UNKNOWN            (超时兜底,需人工/查询驱动)
    """

    CREATED = "created"
    RISK_CHECKED = "risk_checked"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }

    @property
    def is_active(self) -> bool:
        return not self.is_terminal


class RejectReason(StrEnum):
    """订单被拒/风控失败的具体原因。

    风控层抛 ``RiskCheckError`` 时必须带 ``reason``;持久化到 ``orders.reject_reason`` 列。
    """

    RISK_CHECK_FAILED = "risk_check_failed"
    INSUFFICIENT_CASH = "insufficient_cash"
    INSUFFICIENT_POSITION = "insufficient_position"
    INVALID_PRICE = "invalid_price"
    INVALID_QUANTITY = "invalid_quantity"
    INVALID_SYMBOL = "invalid_symbol"
    DUPLICATE_ORDER = "duplicate_order"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    BROKER_REJECTED = "broker_rejected"
    SUBMIT_TIMEOUT = "submit_timeout"  # 触发 UNKNOWN 流程
    UNKNOWN = "unknown"


class BrokerKind(StrEnum):
    MOCK = "mock"
    QMT = "qmt"
    CTP = "ctp"
    BACKTEST = "backtest"

    @property
    def market(self) -> Market:
        return Market.A_SHARE if self in (BrokerKind.QMT, BrokerKind.BACKTEST) else Market.FUTURE


class KillSwitchLevel(StrEnum):
    """Kill Switch 分级(见 phase1_doc.md §6.3)。

    严格程度自上而下递增;高级别隐含低级别所有约束。
    必须由交易内核强制执行,不能只靠网页按钮。
    """

    OFF = "off"
    NO_NEW_ORDERS = "no_new_orders"  # 暂停新单(允许撤单、允许查询)
    REDUCE_ONLY = "reduce_only"  # 仅允许减仓
    CANCEL_ALL = "cancel_all"  # 撤销全部活动订单
    HALT = "halt"  # 全局停止


class BarPeriod(StrEnum):
    """K 线周期。

    值与 xtdata ``period`` 参数对齐,便于直接透传。
    """

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    D1 = "1d"


class TradingPhase(StrEnum):
    """交易日内系统的运行阶段。"""

    PRE_MARKET = "pre_market"
    AUCTION = "auction"  # 集合竞价
    CONTINUOUS = "continuous"  # 连续竞价
    POST_MARKET = "post_market"
    CLOSED = "closed"


# ---------------------------------------------------------------------------
# 多资产生命周期(issue #58)
# ---------------------------------------------------------------------------


class CouponFrequency(StrEnum):
    """债券付息频率。"""

    ANNUAL = "annual"
    SEMI_ANNUAL = "semi_annual"
    QUARTERLY = "quarterly"
    MONTHLY = "monthly"
    ZERO_COUPON = "zero_coupon"  # 零息债
    AT_MATURITY = "at_maturity"  # 到期一次还本付息


class ConvertibleEventType(StrEnum):
    """可转债公司行为 / 事件类型 —— 时点化记录(issue #58)。"""

    LISTING = "listing"  # 上市
    DELISTING = "delisting"  # 退市
    FORCED_REDEMPTION = "forced_redemption"  # 强赎触发 / 公告
    SELL_BACK = "sell_back"  # 回售
    DOWNWARD_REVISION = "downward_revision"  # 下修转股价
    CONVERSION_PRICE_ADJUST = "conversion_price_adjust"  # 除权除息导致的转股价调整
    REDEMPTION = "redemption"  # 到期赎回
    INTEREST_PAYMENT = "interest_payment"  # 付息


class FuturesEventType(StrEnum):
    """期货合约事件 —— 时点化记录。"""

    LISTING = "listing"
    LAST_TRADING = "last_trading"  # 最后交易日
    DELIVERY = "delivery"  # 交割日
    ROLL = "roll"  # 主力换月
    EXPIRATION = "expiration"  # 到期


class RollMethod(StrEnum):
    """连续期货主力换月判定方法。"""

    VOLUME = "volume"  # 按成交量最大
    OPEN_INTEREST = "open_interest"  # 按持仓量最大
    SCHEDULED = "scheduled"  # 按预定日历(如交割月前 N 个交易日)
    MANUAL = "manual"  # 人工指定换月表


class AdjustmentMethod(StrEnum):
    """连续期货拼接的调整方法 —— 影响 OOS 可比性。"""

    NONE = "none"  # 原始价不调整(换月处有跳空)
    RATIO = "ratio"  # 比例调整(回溯缩放,保持收益率连续)
    DIFFERENCE = "difference"  # 差分调整(平移历史价格)
    PANAMA = "panama"  # 巴拿马法(差分的变种)


class DatasetQualityStatus(StrEnum):
    """数据集质量门 —— 不合格数据集不能供回测使用(issue #58 验收)。"""

    UNKNOWN = "unknown"
    PENDING = "pending"  # 待校验
    PASSED = "passed"  # 通过校验
    WARNINGS = "warnings"  # 有警告但可用
    FAILED = "failed"  # 不合格,禁止回测使用


class LifecycleEventType(StrEnum):
    """通用公司行为 / 合约事件(覆盖所有资产类型)。"""

    LISTING = "listing"
    DELISTING = "delisting"
    DIVIDEND = "dividend"  # 分红(股 / ETF)
    SPLIT = "split"  # 拆股 / 合股
    COUPON = "coupon"  # 债券付息
    FORCED_REDEMPTION = "forced_redemption"  # 可转债强赎
    SELL_BACK = "sell_back"
    DOWNWARD_REVISION = "downward_revision"
    CONVERSION_PRICE_ADJUST = "conversion_price_adjust"
    ROLL = "roll"  # 期货换月
    EXPIRATION = "expiration"
    DELIVERY = "delivery"
    SUSPENSION = "suspension"  # 停牌
    RESUMPTION = "resumption"  # 复牌
    OTHER = "other"
