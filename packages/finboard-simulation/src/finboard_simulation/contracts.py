"""持久化模拟交易领域契约(issue #83)。

所有外部输入均为白名单结构化数据。这里不接受 Python、模块路径、Broker
凭据、实盘账户 ID 或外部生成的模拟订单 ID。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from finboard_backtest.config import FillTiming
from finboard_shared.models import Bar
from finboard_shared.types import (
    InstrumentType,
    Market,
    OrderType,
    PositionSide,
    TimeInForce,
)

SIMULATION_SCHEMA_VERSION = "v1"
SIMULATION_MODE = "simulation"


class SimulationError(RuntimeError):
    """模拟域基础错误。"""


class SimulationConflictError(SimulationError):
    """幂等内容、状态或并发写入冲突。"""


class SimulationNotFoundError(SimulationError):
    """模拟实体不存在。"""


class SimulationTransitionError(SimulationError):
    """模拟账户、会话或订单状态迁移非法。"""


class SimulationRiskError(SimulationError):
    """模拟下单前风险检查失败。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class SimulationIsolationError(SimulationError):
    """请求试图突破模拟与实盘边界。"""


class SimulationAccountStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class SimulationSourceMode(StrEnum):
    HISTORICAL_REPLAY = "historical_replay"
    READONLY_MARKET = "readonly_market"


class SimulationSessionStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ARCHIVED = "archived"


class SimulationPromotionStatus(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    ELIGIBLE = "eligible"
    FAILED = "failed"


class SimulationPositionEffect(StrEnum):
    OPEN = "open"
    CLOSE = "close"


class SimulationMarketEventStatus(StrEnum):
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SimulationRiskLimits:
    max_order_value: Decimal = Decimal("100000")
    max_symbol_position_value: Decimal = Decimal("150000")
    max_gross_exposure: Decimal = Decimal("1")
    max_margin_usage: Decimal = Decimal("0.8")
    max_active_orders: int = 20
    allow_short: bool = False

    def __post_init__(self) -> None:
        if self.max_order_value <= 0 or self.max_symbol_position_value <= 0:
            raise ValueError("模拟订单与单标的金额上限必须为正")
        if self.max_gross_exposure <= 0:
            raise ValueError("模拟总敞口上限必须为正")
        if not Decimal("0") < self.max_margin_usage <= Decimal("1"):
            raise ValueError("模拟保证金占用上限必须位于 (0, 1]")
        if self.max_active_orders < 1:
            raise ValueError("max_active_orders 必须 >= 1")

    def as_dict(self) -> dict[str, object]:
        return {
            "max_order_value": str(self.max_order_value),
            "max_symbol_position_value": str(self.max_symbol_position_value),
            "max_gross_exposure": str(self.max_gross_exposure),
            "max_margin_usage": str(self.max_margin_usage),
            "max_active_orders": self.max_active_orders,
            "allow_short": self.allow_short,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> SimulationRiskLimits:
        return cls(
            max_order_value=Decimal(str(raw["max_order_value"])),
            max_symbol_position_value=Decimal(str(raw["max_symbol_position_value"])),
            max_gross_exposure=Decimal(str(raw["max_gross_exposure"])),
            max_margin_usage=Decimal(str(raw["max_margin_usage"])),
            max_active_orders=int(str(raw["max_active_orders"])),
            allow_short=bool(raw["allow_short"]),
        )


@dataclass(frozen=True, slots=True)
class SimulationMatchingConfig:
    fill_timing: FillTiming = FillTiming.NEXT_BAR_OPEN
    next_bar_only: bool = True
    max_participation: Decimal = Decimal("0.1")
    allow_partial_fill: bool = True
    honour_gaps: bool = True
    enforce_suspension: bool = True
    enforce_price_limit: bool = True
    enforce_lot_rounding: bool = True
    slippage_bps: Decimal = Decimal("5")
    market_price_buffer_bps: Decimal = Decimal("200")

    def __post_init__(self) -> None:
        if not Decimal("0") < self.max_participation <= Decimal("1"):
            raise ValueError("max_participation 必须位于 (0, 1]")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps 不能为负")
        if self.market_price_buffer_bps < self.slippage_bps:
            raise ValueError("市价预占缓冲不得小于滑点假设")
        if not self.next_bar_only:
            raise ValueError("产品模拟盘强制 next-bar 撮合")

    def as_dict(self) -> dict[str, object]:
        return {
            "fill_timing": self.fill_timing.value,
            "next_bar_only": self.next_bar_only,
            "max_participation": str(self.max_participation),
            "allow_partial_fill": self.allow_partial_fill,
            "honour_gaps": self.honour_gaps,
            "enforce_suspension": self.enforce_suspension,
            "enforce_price_limit": self.enforce_price_limit,
            "enforce_lot_rounding": self.enforce_lot_rounding,
            "slippage_bps": str(self.slippage_bps),
            "market_price_buffer_bps": str(self.market_price_buffer_bps),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> SimulationMatchingConfig:
        return cls(
            fill_timing=FillTiming(str(raw["fill_timing"])),
            next_bar_only=bool(raw["next_bar_only"]),
            max_participation=Decimal(str(raw["max_participation"])),
            allow_partial_fill=bool(raw["allow_partial_fill"]),
            honour_gaps=bool(raw["honour_gaps"]),
            enforce_suspension=bool(raw["enforce_suspension"]),
            enforce_price_limit=bool(raw["enforce_price_limit"]),
            enforce_lot_rounding=bool(raw["enforce_lot_rounding"]),
            slippage_bps=Decimal(str(raw["slippage_bps"])),
            market_price_buffer_bps=Decimal(str(raw["market_price_buffer_bps"])),
        )


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    matching: SimulationMatchingConfig = field(default_factory=SimulationMatchingConfig)
    risk: SimulationRiskLimits = field(default_factory=SimulationRiskLimits)
    clock_speed: Decimal = Decimal("1")
    schema_version: str = SIMULATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.clock_speed <= 0 or self.clock_speed > Decimal("1000"):
            raise ValueError("clock_speed 必须位于 (0, 1000]")
        if self.schema_version != SIMULATION_SCHEMA_VERSION:
            raise ValueError("不支持的模拟配置版本")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "clock_speed": str(self.clock_speed),
            "matching": self.matching.as_dict(),
            "risk": self.risk.as_dict(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> SimulationConfig:
        matching = raw.get("matching")
        risk = raw.get("risk")
        if not isinstance(matching, dict) or not isinstance(risk, dict):
            raise ValueError("模拟配置缺少 matching/risk")
        return cls(
            matching=SimulationMatchingConfig.from_dict(matching),
            risk=SimulationRiskLimits.from_dict(risk),
            clock_speed=Decimal(str(raw["clock_speed"])),
            schema_version=str(raw["schema_version"]),
        )


@dataclass(frozen=True, slots=True)
class SimulationTarget:
    """策略 runner 消费的目标仓位, 而不是订单。"""

    symbol: str
    market: Market
    instrument_type: InstrumentType
    asset_rule_key: str
    position_side: PositionSide
    target_quantity: Decimal
    signal_trace_id: str
    reason: str
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GFD

    def __post_init__(self) -> None:
        if not self.symbol or not self.signal_trace_id or not self.reason:
            raise ValueError("目标仓位必须包含 symbol、信号 trace 和原因")
        if self.target_quantity < 0:
            raise ValueError("target_quantity 不能为负; 空头用 position_side 表达")
        if self.order_type is OrderType.LIMIT and (
            self.limit_price is None or self.limit_price <= 0
        ):
            raise ValueError("限价目标必须提供正 limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("市价目标不能提供 limit_price")
        if (
            self.position_side is PositionSide.SHORT
            and self.instrument_type is not InstrumentType.FUTURES
        ):
            raise ValueError("只有期货模拟目标可以声明空头")

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "market": self.market.value,
            "instrument_type": self.instrument_type.value,
            "asset_rule_key": self.asset_rule_key,
            "position_side": self.position_side.value,
            "target_quantity": str(self.target_quantity),
            "signal_trace_id": self.signal_trace_id,
            "reason": self.reason,
            "order_type": self.order_type.value,
            "limit_price": (str(self.limit_price) if self.limit_price is not None else None),
            "time_in_force": self.time_in_force.value,
        }


@dataclass(frozen=True, slots=True)
class SimulationDecision:
    """由已完成机器验证 run 产生的可幂等目标仓位决策。"""

    decision_id: str
    source_run_id: str
    source_decision_id: str
    targets: tuple[SimulationTarget, ...]
    actor: str

    def __post_init__(self) -> None:
        if not self.decision_id or not self.source_decision_id or not self.actor:
            raise ValueError("模拟决策标识、来源决策和 actor 不能为空")
        if not self.source_run_id.startswith("RR-"):
            raise ValueError("模拟决策必须引用 RR- 机器验证运行")
        if not self.targets:
            raise ValueError("模拟决策必须包含至少一个目标仓位")
        keys = [(item.symbol, item.position_side) for item in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("同一决策不能重复声明 symbol/position_side")

    @property
    def checksum(self) -> str:
        return stable_checksum(
            {
                "decision_id": self.decision_id,
                "source_run_id": self.source_run_id,
                "source_decision_id": self.source_decision_id,
                "targets": [item.as_dict() for item in self.targets],
                "actor": self.actor,
            }
        )


@dataclass(frozen=True, slots=True)
class SimulationBarEvent:
    source_event_id: str
    bar: Bar
    actor: str = "market-data"
    contract_id: str | None = None

    def __post_init__(self) -> None:
        if not self.source_event_id or not self.actor:
            raise ValueError("行情事件 ID 与 actor 不能为空")
        if self.bar.timestamp.tzinfo is None:
            raise ValueError("模拟行情时间必须带时区")
        prices = (self.bar.open, self.bar.high, self.bar.low, self.bar.close)
        if any(item <= 0 for item in prices):
            raise ValueError("模拟 OHLC 必须为正")
        if self.bar.low > min(self.bar.open, self.bar.close, self.bar.high):
            raise ValueError("bar.low 超出 OHLC 范围")
        if self.bar.high < max(self.bar.open, self.bar.close, self.bar.low):
            raise ValueError("bar.high 超出 OHLC 范围")
        if self.bar.volume < 0:
            raise ValueError("bar.volume 不能为负")
        if self.bar.symbol.market is Market.FUTURE:
            expected_contract = self.bar.symbol.code.split(".", 1)[0]
            if self.contract_id != expected_contract:
                raise ValueError(
                    "期货行情必须声明与 symbol 一致的具体 contract_id,模拟盘禁止静默连续合约换月"
                )

    def as_dict(self) -> dict[str, object]:
        return {
            "source_event_id": self.source_event_id,
            "symbol": self.bar.symbol.code,
            "market": self.bar.symbol.market.value,
            "period": self.bar.period.value,
            "timestamp": self.bar.timestamp.isoformat(),
            "open": str(self.bar.open),
            "high": str(self.bar.high),
            "low": str(self.bar.low),
            "close": str(self.bar.close),
            "volume": str(self.bar.volume),
            "amount": str(self.bar.amount),
            "actor": self.actor,
            "contract_id": self.contract_id,
        }

    @property
    def checksum(self) -> str:
        return stable_checksum(self.as_dict())


@dataclass(frozen=True, slots=True)
class SimulationProcessResult:
    source_event_id: str
    duplicate: bool
    fill_ids: tuple[str, ...]
    rejected_order_ids: tuple[str, ...]
    equity: Decimal
    clock_at: datetime


def new_simulation_id(kind: str) -> str:
    """生成只属于模拟域的本地 ID。"""

    allowed = {"A", "S", "O", "F", "L", "X"}
    if kind not in allowed:
        raise ValueError("未知模拟 ID 类型")
    today = datetime.now(UTC).strftime("%Y%m%d")
    return f"SIM-{kind}-{today}-{secrets.token_hex(8)}"


def stable_checksum(value: object) -> str:
    normalized = _json_value(value)
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _json_value(value: object) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise TypeError(f"不支持 checksum 类型: {type(value)!r}")


__all__ = [
    "SIMULATION_MODE",
    "SIMULATION_SCHEMA_VERSION",
    "SimulationAccountStatus",
    "SimulationBarEvent",
    "SimulationConfig",
    "SimulationConflictError",
    "SimulationDecision",
    "SimulationError",
    "SimulationIsolationError",
    "SimulationMarketEventStatus",
    "SimulationMatchingConfig",
    "SimulationNotFoundError",
    "SimulationPositionEffect",
    "SimulationProcessResult",
    "SimulationPromotionStatus",
    "SimulationRiskError",
    "SimulationRiskLimits",
    "SimulationSessionStatus",
    "SimulationSourceMode",
    "SimulationTarget",
    "SimulationTransitionError",
    "new_simulation_id",
    "stable_checksum",
]
