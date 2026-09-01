"""不可变研究数据发布与只读 Provider(issue #77)。

本模块把可变 ``data_cache`` 中的 Parquet 文件冻结为一个版本化发布目录:

```
data_releases/<release_id>/
  manifest.json
  bars/<symbol>_<period>_<adjust>.parquet
```

发布只在全部必需资产通过质量门后原子改名为最终目录。已存在的 ``release_id``
永不覆盖;相同规格可幂等读取,不同规格会 fail closed。模块不访问 Broker、订单、
持仓或实盘配置。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast, runtime_checkable
from zoneinfo import ZoneInfo

from finboard_data.cache import CacheMetadata, ParquetCache
from finboard_data.quality import BarQualityChecker
from finboard_data.research import DailySecurityMetrics, FinancialIndicator
from finboard_data.trading_calendar import trading_days as _trading_days
from finboard_shared.instruments import ASSET_METADATA_VERSION, DatasetManifest
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import (
    AssetClass,
    BarPeriod,
    DatasetQualityStatus,
    EtfCategory,
    InstrumentType,
    ListingStatus,
    Market,
)

RELEASE_SCHEMA_VERSION = "v1"
RELEASE_MANIFEST_FILENAME = "manifest.json"
RELEASE_FIELDS = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)

# issue #187:研究数据发布的数据集类型。除价格 bars 外,daily_metrics
# (每日估值/流动性截面)与 financial_indicators(财务公告修订)也支持冻结发布,
# 供因子快照从联合 release 取数(解锁 pb/市值/换手/ROE 等 signal_eligible 因子)。
class ReleaseDatasetKind(StrEnum):
    """一个冻结发布的数据集类型。"""

    BARS = "bars"
    DAILY_METRICS = "daily_metrics"
    FINANCIAL_INDICATORS = "financial_indicators"


RELEASE_KINDS = frozenset(kind.value for kind in ReleaseDatasetKind)

# 各数据集类型的字段白名单(fields 只能是白名单子集)。列名与
# ``research_daily_metrics`` / ``research_financial_indicators`` 表一致,
# 数据来源是 research_data_sync(#171)摄取的研究数据。
DAILY_METRICS_FIELDS = (
    "trade_date",
    "close",
    "turnover_rate",
    "turnover_rate_free",
    "volume_ratio",
    "pe",
    "pe_ttm",
    "pb",
    "ps",
    "ps_ttm",
    "dividend_yield",
    "dividend_yield_ttm",
    "total_shares",
    "float_shares",
    "free_shares",
    "total_market_cap",
    "circulating_market_cap",
    "limit_status",
)
FINANCIAL_INDICATORS_FIELDS = (
    "announcement_date",
    "report_period",
    "update_flag",
    "eps",
    "diluted_eps",
    "book_value_per_share",
    "operating_cash_flow_per_share",
    "return_on_equity",
    "weighted_return_on_equity",
    "gross_profit_margin",
    "net_profit_margin",
    "debt_to_assets",
    "revenue_yoy",
    "net_profit_yoy",
    "operating_cash_flow_yoy",
)

# 研究数据冻结字段白名单:symbol 单独成列,available_at/observed_at/source
# 属于时点化必需元数据,不参与用户声明的 fields。
RESEARCH_RELEASE_METADATA_FIELDS = (
    "available_at",
    "observed_at",
    "source",
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.]{0,31}$")
_VALID_ADJUSTMENTS = frozenset({"qfq", "hqfq", "none"})


class DatasetReleaseError(RuntimeError):
    """研究数据发布错误基类。"""


class DatasetReleaseQualityError(DatasetReleaseError):
    """发布内容没有通过质量门。"""


class ImmutableReleaseError(DatasetReleaseError):
    """尝试以不同规格覆盖已经冻结的发布。"""


class ReleaseIntegrityError(DatasetReleaseError):
    """发布清单或文件校验和不一致。"""


class ReleaseCapabilityError(DatasetReleaseError):
    """请求的资产能力在发布中不完整。"""


class CapabilityStatus(StrEnum):
    """一个资产研究能力是否具备可用数据、元数据与事件。"""

    READY = "ready"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class ReleaseLifecycleEvent:
    """冻结到发布中的时点化公司行为/合约事件。"""

    event_type: str
    effective_date: date
    available_at: datetime
    source: str
    dataset_version: str
    details: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_type or not self.source or not self.dataset_version:
            raise ValueError("生命周期事件类型、来源和版本不能为空")
        if self.available_at.tzinfo is None:
            raise ValueError("生命周期事件 available_at 必须带时区")
        effective_at = datetime.combine(
            self.effective_date,
            datetime.min.time(),
            tzinfo=self.available_at.tzinfo,
        )
        if self.available_at < effective_at:
            raise ValueError("生命周期事件 available_at 不能早于 effective_date")

    def as_dict(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "effective_date": self.effective_date.isoformat(),
            "available_at": self.available_at.isoformat(),
            "source": self.source,
            "dataset_version": self.dataset_version,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> ReleaseLifecycleEvent:
        return cls(
            event_type=str(raw["event_type"]),
            effective_date=date.fromisoformat(str(raw["effective_date"])),
            available_at=datetime.fromisoformat(str(raw["available_at"])),
            source=str(raw["source"]),
            dataset_version=str(raw["dataset_version"]),
            details=cast(dict[str, object], raw.get("details", {})),
        )


@dataclass(frozen=True, slots=True)
class ExecutionMetadata:
    """回测消费的资产规则快照。

    这些字段只描述研究执行假设,不会写入或修改实盘 Risk Manager。
    """

    lot_size: Decimal
    price_tick: Decimal
    settlement_days: int
    multiplier: Decimal = Decimal("1")
    margin_rate: Decimal | None = None
    stamp_tax_rate: Decimal = Decimal("0")
    commission_rate: Decimal = Decimal("0.0003")
    commission_min: Decimal = Decimal("5")
    trading_calendar: str = "SSE"
    allows_short: bool = False

    def __post_init__(self) -> None:
        if self.lot_size <= 0:
            raise ValueError("lot_size 必须为正")
        if self.price_tick <= 0:
            raise ValueError("price_tick 必须为正")
        if self.settlement_days < 0:
            raise ValueError("settlement_days 不能为负")
        if self.multiplier <= 0:
            raise ValueError("multiplier 必须为正")
        if self.margin_rate is not None and not (Decimal("0") < self.margin_rate <= Decimal("1")):
            raise ValueError("margin_rate 必须落在 (0, 1]")
        if self.stamp_tax_rate < 0:
            raise ValueError("stamp_tax_rate 不能为负")
        if self.commission_rate < 0 or self.commission_min < 0:
            raise ValueError("佣金参数不能为负")
        if not self.trading_calendar:
            raise ValueError("trading_calendar 不能为空")

    def as_dict(self) -> dict[str, object]:
        return {
            "lot_size": str(self.lot_size),
            "price_tick": str(self.price_tick),
            "settlement_days": self.settlement_days,
            "multiplier": str(self.multiplier),
            "margin_rate": str(self.margin_rate) if self.margin_rate is not None else None,
            "stamp_tax_rate": str(self.stamp_tax_rate),
            "commission_rate": str(self.commission_rate),
            "commission_min": str(self.commission_min),
            "trading_calendar": self.trading_calendar,
            "allows_short": self.allows_short,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> ExecutionMetadata:
        margin = raw.get("margin_rate")
        return cls(
            lot_size=Decimal(str(raw["lot_size"])),
            price_tick=Decimal(str(raw["price_tick"])),
            settlement_days=int(str(raw["settlement_days"])),
            multiplier=Decimal(str(raw.get("multiplier", "1"))),
            margin_rate=Decimal(str(margin)) if margin is not None else None,
            stamp_tax_rate=Decimal(str(raw.get("stamp_tax_rate", "0"))),
            commission_rate=Decimal(str(raw.get("commission_rate", "0.0003"))),
            commission_min=Decimal(str(raw.get("commission_min", "5"))),
            trading_calendar=str(raw.get("trading_calendar", "SSE")),
            allows_short=bool(raw.get("allows_short", False)),
        )


@dataclass(frozen=True, slots=True)
class ResearchEtfCatalogEntry:
    """#66 首期多资产 ETF sleeve 的保守元数据种子。"""

    code: str
    name: str
    category: EtfCategory
    asset_class: AssetClass
    list_date: date
    underlying_index: str | None = None


RESEARCH_ETF_CATALOG: tuple[ResearchEtfCatalogEntry, ...] = (
    ResearchEtfCatalogEntry(
        "510300.SH",
        "沪深300ETF",
        EtfCategory.INDEX,
        AssetClass.EQUITY,
        date(2012, 5, 28),
        "000300.SH",
    ),
    ResearchEtfCatalogEntry(
        "159915.SZ",
        "创业板ETF",
        EtfCategory.INDEX,
        AssetClass.EQUITY,
        date(2011, 9, 20),
        "399006.SZ",
    ),
    ResearchEtfCatalogEntry(
        "513100.SH",
        "纳指ETF",
        EtfCategory.CROSS_BORDER,
        AssetClass.EQUITY,
        date(2013, 5, 15),
        "NDX.US",
    ),
    ResearchEtfCatalogEntry(
        "513500.SH",
        "标普500ETF",
        EtfCategory.CROSS_BORDER,
        AssetClass.EQUITY,
        date(2013, 12, 5),
        "SPX.US",
    ),
    ResearchEtfCatalogEntry(
        "518880.SH",
        "黄金ETF",
        EtfCategory.COMMODITY,
        AssetClass.COMMODITY,
        date(2013, 7, 29),
    ),
    ResearchEtfCatalogEntry(
        "511010.SH",
        "国债ETF",
        EtfCategory.BOND,
        AssetClass.FIXED_INCOME,
        date(2013, 3, 5),
    ),
    ResearchEtfCatalogEntry(
        "511260.SH",
        "十年国债ETF",
        EtfCategory.BOND,
        AssetClass.FIXED_INCOME,
        date(2017, 8, 16),
    ),
)
_ETF_CATALOG_BY_CODE = {item.code: item for item in RESEARCH_ETF_CATALOG}


def research_etf_catalog_entry(code: str) -> ResearchEtfCatalogEntry | None:
    """返回首期研究 ETF 种子;未知代码不猜测分类。"""

    return _ETF_CATALOG_BY_CODE.get(code.upper())


def default_execution_metadata(
    *,
    market: Market,
    instrument_type: InstrumentType,
    etf_category: EtfCategory | None = None,
    multiplier: Decimal = Decimal("1"),
    margin_rate: Decimal | None = None,
) -> ExecutionMetadata:
    """根据已知资产类型生成显式研究执行元数据。

    未知市场/类型直接 raise,禁止静默套用 A 股股票规则。
    """

    if market is Market.A_SHARE and instrument_type is InstrumentType.STOCK:
        return ExecutionMetadata(
            lot_size=Decimal("100"),
            price_tick=Decimal("0.01"),
            settlement_days=1,
            stamp_tax_rate=Decimal("0.0005"),
            trading_calendar="SSE/SZSE/BSE",
        )
    if market is Market.A_SHARE and instrument_type is InstrumentType.ETF:
        if etf_category is None:
            raise ReleaseCapabilityError("ETF 缺少 etf_category,无法确定 T+N/税费/手数")
        if etf_category is EtfCategory.BOND:
            return ExecutionMetadata(
                lot_size=Decimal("10"),
                price_tick=Decimal("0.001"),
                settlement_days=1,
                stamp_tax_rate=Decimal("0"),
                commission_rate=Decimal("0.00003"),
                trading_calendar="SSE/SZSE",
            )
        if etf_category is EtfCategory.MONEY_MARKET:
            return ExecutionMetadata(
                lot_size=Decimal("100"),
                price_tick=Decimal("0.001"),
                settlement_days=0,
                stamp_tax_rate=Decimal("0"),
                commission_rate=Decimal("0"),
                commission_min=Decimal("0"),
                trading_calendar="SSE/SZSE",
            )
        settlement = 0 if etf_category in (EtfCategory.CROSS_BORDER, EtfCategory.COMMODITY) else 1
        return ExecutionMetadata(
            lot_size=Decimal("100"),
            price_tick=Decimal("0.001"),
            settlement_days=settlement,
            stamp_tax_rate=Decimal("0"),
            trading_calendar="SSE/SZSE",
        )
    if market is Market.A_SHARE and instrument_type is InstrumentType.CONVERTIBLE:
        return ExecutionMetadata(
            lot_size=Decimal("10"),
            price_tick=Decimal("0.001"),
            settlement_days=0,
            stamp_tax_rate=Decimal("0"),
            commission_rate=Decimal("0.0002"),
            commission_min=Decimal("1"),
            trading_calendar="SSE/SZSE",
        )
    if market is Market.A_SHARE and instrument_type is InstrumentType.BOND:
        return ExecutionMetadata(
            lot_size=Decimal("10"),
            price_tick=Decimal("0.001"),
            settlement_days=0,
            stamp_tax_rate=Decimal("0"),
            commission_rate=Decimal("0.00003"),
            trading_calendar="SSE/SZSE",
        )
    if market is Market.A_SHARE and instrument_type is InstrumentType.INDEX:
        # 指数基准资产(issue #184):只进数据/发布通道供基准曲线与超额收益
        # 计算,不可撮合 —— 执行元数据只是数据域占位(零费用、整手=1)。
        # 回测撮合域没有 INDEX 撮合规则,若把指数放进可交易 universe 会
        # 按默认 STOCK 规则处理,属调用方误用,不在本通道内放行。
        return ExecutionMetadata(
            lot_size=Decimal("1"),
            price_tick=Decimal("0.01"),
            settlement_days=0,
            stamp_tax_rate=Decimal("0"),
            commission_rate=Decimal("0"),
            commission_min=Decimal("0"),
            trading_calendar="SSE/SZSE",
        )
    if market is Market.FUTURE and instrument_type is InstrumentType.FUTURES:
        if margin_rate is None:
            raise ReleaseCapabilityError("期货缺少 margin_rate")
        return ExecutionMetadata(
            lot_size=Decimal("1"),
            price_tick=Decimal("0.01"),
            settlement_days=0,
            multiplier=multiplier,
            margin_rate=margin_rate,
            stamp_tax_rate=Decimal("0"),
            commission_min=Decimal("0"),
            trading_calendar="FUTURES_EXCHANGE",
            allows_short=True,
        )
    raise ReleaseCapabilityError(
        f"无研究执行元数据: market={market.value} type={instrument_type.value}"
    )


@dataclass(frozen=True, slots=True)
class ReleaseInstrumentSpec:
    """一项待冻结标的的时点化元数据。"""

    code: str
    name: str
    market: Market
    instrument_type: InstrumentType
    asset_class: AssetClass
    available_at: datetime
    execution: ExecutionMetadata
    exchange: str | None = None
    listing_board: str = "unknown"
    currency: str = "CNY"
    etf_category: EtfCategory | None = None
    list_date: date | None = None
    delist_date: date | None = None
    industry: str | None = None
    status: ListingStatus = ListingStatus.UNKNOWN
    metadata_complete: bool = True
    lifecycle_events: tuple[ReleaseLifecycleEvent, ...] = ()
    present_event_types: tuple[str, ...] = ()
    required_event_types: tuple[str, ...] = ()
    name_history: tuple[tuple[str, date, date | None], ...] = ()

    def __post_init__(self) -> None:
        if not _SAFE_SYMBOL.fullmatch(self.code):
            raise ValueError(f"不安全或非法标的代码: {self.code}")
        if not self.name:
            raise ValueError("name 不能为空")
        if self.available_at.tzinfo is None:
            raise ValueError("available_at 必须带时区")
        if self.instrument_type is InstrumentType.ETF and self.etf_category is None:
            raise ValueError("ETF 必须声明 etf_category")
        event_types = {event.event_type for event in self.lifecycle_events}
        if not event_types.issubset(self.present_event_types):
            raise ValueError("lifecycle_events 的类型必须包含在 present_event_types")

    @property
    def capability_key(self) -> str:
        if self.instrument_type is InstrumentType.ETF:
            assert self.etf_category is not None
            return f"etf:{self.etf_category.value}"
        return self.instrument_type.value


@dataclass(frozen=True, slots=True)
class DatasetReleaseSpec:
    """一次冻结发布的不可变输入规格。"""

    release_id: str
    dataset_name: str
    source: str
    version: str
    start_date: date
    end_date: date
    code_version: str
    schema_version: str = RELEASE_SCHEMA_VERSION
    period: BarPeriod = BarPeriod.D1
    adjustment: str = "qfq"
    fields: tuple[str, ...] = RELEASE_FIELDS
    dataset_kind: ReleaseDatasetKind = ReleaseDatasetKind.BARS
    required_capabilities: tuple[str, ...] = (
        InstrumentType.STOCK.value,
        f"etf:{EtfCategory.INDEX.value}",
        f"etf:{EtfCategory.CROSS_BORDER.value}",
        f"etf:{EtfCategory.COMMODITY.value}",
        f"etf:{EtfCategory.BOND.value}",
    )
    availability_rules: tuple[tuple[str, str], ...] = (
        ("daily_bars", "T 日收盘后可知;回测按事件时间逐 Bar 暴露"),
        ("instrument_metadata", "available_at <= decision_at"),
        ("lifecycle_events", "available_at <= decision_at"),
    )
    known_limitations: tuple[str, ...] = ()
    minimum_symbol_coverage: Decimal = Decimal("0.98")
    minimum_release_coverage: Decimal = Decimal("0.98")
    max_anomaly_ratio: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for value, name in (
            (self.release_id, "release_id"),
            (self.dataset_name, "dataset_name"),
            (self.source, "source"),
            (self.version, "version"),
            (self.schema_version, "schema_version"),
        ):
            if not _SAFE_ID.fullmatch(value):
                raise ValueError(f"{name} 只能包含字母、数字、点、下划线和连字符")
        if self.start_date > self.end_date:
            raise ValueError("start_date 不能晚于 end_date")
        if self.adjustment not in _VALID_ADJUSTMENTS:
            raise ValueError(f"不支持的 adjustment: {self.adjustment}")
        if not self.fields:
            raise ValueError("fields 不能为空")
        if len(self.fields) != len(set(self.fields)):
            raise ValueError("fields 不能重复")
        unknown_fields = set(self.fields) - _fields_whitelist(self.dataset_kind)
        if unknown_fields:
            raise ValueError(
                f"{self.dataset_kind.value} 发布 fields 包含未冻结字段: "
                f"{sorted(unknown_fields)}"
            )
        if not (Decimal("0") < self.minimum_symbol_coverage <= Decimal("1")):
            raise ValueError("minimum_symbol_coverage 必须落在 (0, 1]")
        if not (Decimal("0") < self.minimum_release_coverage <= Decimal("1")):
            raise ValueError("minimum_release_coverage 必须落在 (0, 1]")
        if not (Decimal("0") <= self.max_anomaly_ratio <= Decimal("1")):
            raise ValueError("max_anomaly_ratio 必须落在 [0, 1]")


def _fields_whitelist(kind: ReleaseDatasetKind) -> frozenset[str]:
    """按数据集类型返回冻结字段白名单。"""
    if kind is ReleaseDatasetKind.DAILY_METRICS:
        return frozenset(DAILY_METRICS_FIELDS)
    if kind is ReleaseDatasetKind.FINANCIAL_INDICATORS:
        return frozenset(FINANCIAL_INDICATORS_FIELDS)
    return frozenset(RELEASE_FIELDS)


@dataclass(frozen=True, slots=True)
class ReleasedInstrument:
    """冻结发布中的逐标的资产、文件和覆盖快照。"""

    code: str
    name: str
    market: Market
    instrument_type: InstrumentType
    asset_class: AssetClass
    available_at: datetime
    execution: ExecutionMetadata
    artifact_path: str
    artifact_checksum: str
    artifact_size: int
    row_count: int
    start_date: date
    end_date: date
    expected_sessions: int
    missing_sessions: int
    suspended_sessions: int
    anomaly_count: int
    coverage_pct: Decimal
    category: str
    ready: bool
    issues: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    exchange: str | None = None
    listing_board: str = "unknown"
    currency: str = "CNY"
    etf_category: EtfCategory | None = None
    list_date: date | None = None
    delist_date: date | None = None
    industry: str | None = None
    status: ListingStatus = ListingStatus.UNKNOWN
    metadata_complete: bool = True
    lifecycle_events: tuple[ReleaseLifecycleEvent, ...] = ()
    present_event_types: tuple[str, ...] = ()
    required_event_types: tuple[str, ...] = ()
    name_history: tuple[tuple[str, date, date | None], ...] = ()

    @property
    def capability_key(self) -> str:
        if self.instrument_type is InstrumentType.ETF:
            assert self.etf_category is not None
            return f"etf:{self.etf_category.value}"
        return self.instrument_type.value

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "name": self.name,
            "market": self.market.value,
            "instrument_type": self.instrument_type.value,
            "asset_class": self.asset_class.value,
            "available_at": self.available_at.isoformat(),
            "execution": self.execution.as_dict(),
            "artifact_path": self.artifact_path,
            "artifact_checksum": self.artifact_checksum,
            "artifact_size": self.artifact_size,
            "row_count": self.row_count,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "expected_sessions": self.expected_sessions,
            "missing_sessions": self.missing_sessions,
            "suspended_sessions": self.suspended_sessions,
            "anomaly_count": self.anomaly_count,
            "coverage_pct": str(self.coverage_pct),
            "category": self.category,
            "ready": self.ready,
            "issues": list(self.issues),
            **({"sources": list(self.sources)} if self.sources else {}),
            "exchange": self.exchange,
            "listing_board": self.listing_board,
            "currency": self.currency,
            "etf_category": self.etf_category.value if self.etf_category else None,
            "list_date": self.list_date.isoformat() if self.list_date else None,
            "delist_date": self.delist_date.isoformat() if self.delist_date else None,
            "industry": self.industry,
            "status": self.status.value,
            "metadata_complete": self.metadata_complete,
            "lifecycle_events": [event.as_dict() for event in self.lifecycle_events],
            "present_event_types": list(self.present_event_types),
            "required_event_types": list(self.required_event_types),
            "name_history": [
                {
                    "name": name,
                    "valid_from": valid_from.isoformat(),
                    "valid_to": valid_to.isoformat() if valid_to else None,
                }
                for name, valid_from, valid_to in self.name_history
            ],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> ReleasedInstrument:
        execution = cast(dict[str, object], raw["execution"])
        history_raw = cast(list[dict[str, object]], raw.get("name_history", []))
        etf_raw = raw.get("etf_category")
        list_raw = raw.get("list_date")
        delist_raw = raw.get("delist_date")
        events_raw = cast(
            list[dict[str, object]],
            raw.get("lifecycle_events", []),
        )
        return cls(
            code=str(raw["code"]),
            name=str(raw["name"]),
            market=Market(str(raw["market"])),
            instrument_type=InstrumentType(str(raw["instrument_type"])),
            asset_class=AssetClass(str(raw["asset_class"])),
            available_at=datetime.fromisoformat(str(raw["available_at"])),
            execution=ExecutionMetadata.from_dict(execution),
            artifact_path=str(raw["artifact_path"]),
            artifact_checksum=str(raw["artifact_checksum"]),
            artifact_size=int(str(raw["artifact_size"])),
            row_count=int(str(raw["row_count"])),
            start_date=date.fromisoformat(str(raw["start_date"])),
            end_date=date.fromisoformat(str(raw["end_date"])),
            expected_sessions=int(str(raw["expected_sessions"])),
            missing_sessions=int(str(raw["missing_sessions"])),
            suspended_sessions=int(str(raw["suspended_sessions"])),
            anomaly_count=int(str(raw["anomaly_count"])),
            coverage_pct=Decimal(str(raw["coverage_pct"])),
            category=str(raw["category"]),
            ready=bool(raw["ready"]),
            issues=tuple(str(item) for item in cast(list[object], raw.get("issues", []))),
            sources=tuple(str(item) for item in cast(list[object], raw.get("sources", []))),
            exchange=str(raw["exchange"]) if raw.get("exchange") is not None else None,
            listing_board=str(raw.get("listing_board", "unknown")),
            currency=str(raw.get("currency", "CNY")),
            etf_category=EtfCategory(str(etf_raw)) if etf_raw is not None else None,
            list_date=date.fromisoformat(str(list_raw)) if list_raw is not None else None,
            delist_date=(date.fromisoformat(str(delist_raw)) if delist_raw is not None else None),
            industry=str(raw["industry"]) if raw.get("industry") is not None else None,
            status=ListingStatus(str(raw.get("status", ListingStatus.UNKNOWN.value))),
            metadata_complete=bool(raw.get("metadata_complete", True)),
            lifecycle_events=tuple(ReleaseLifecycleEvent.from_dict(item) for item in events_raw),
            present_event_types=tuple(
                str(item) for item in cast(list[object], raw.get("present_event_types", []))
            ),
            required_event_types=tuple(
                str(item) for item in cast(list[object], raw.get("required_event_types", []))
            ),
            name_history=tuple(
                (
                    str(item["name"]),
                    date.fromisoformat(str(item["valid_from"])),
                    (
                        date.fromisoformat(str(item["valid_to"]))
                        if item.get("valid_to") is not None
                        else None
                    ),
                )
                for item in history_raw
            ),
        )


@dataclass(frozen=True, slots=True)
class AssetCapability:
    """发布对某类资产研究的就绪报告。"""

    key: str
    status: CapabilityStatus
    symbol_count: int
    ready_count: int
    missing_requirements: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.status is CapabilityStatus.READY

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "status": self.status.value,
            "symbol_count": self.symbol_count,
            "ready_count": self.ready_count,
            "missing_requirements": list(self.missing_requirements),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> AssetCapability:
        return cls(
            key=str(raw["key"]),
            status=CapabilityStatus(str(raw["status"])),
            symbol_count=int(str(raw["symbol_count"])),
            ready_count=int(str(raw["ready_count"])),
            missing_requirements=tuple(
                str(item) for item in cast(list[object], raw.get("missing_requirements", []))
            ),
        )


@dataclass(frozen=True, slots=True)
class ResearchDatasetRelease:
    """可持久化、可复现的冻结数据发布。"""

    release_id: str
    dataset_name: str
    source: str
    version: str
    schema_version: str
    start_date: date
    end_date: date
    period: BarPeriod
    adjustment: str
    fields: tuple[str, ...]
    availability_rules: tuple[tuple[str, str], ...]
    code_version: str
    published_at: datetime
    instruments: tuple[ReleasedInstrument, ...]
    capabilities: tuple[AssetCapability, ...]
    quality_status: DatasetQualityStatus
    quality_report: dict[str, object]
    dataset_kind: ReleaseDatasetKind = ReleaseDatasetKind.BARS
    known_limitations: tuple[str, ...] = ()
    storage_uri: str = ""
    metadata_version: str = ASSET_METADATA_VERSION
    release_checksum: str = ""

    @property
    def symbol_count(self) -> int:
        return len(self.instruments)

    @property
    def row_count(self) -> int:
        return sum(item.row_count for item in self.instruments)

    @property
    def coverage_pct(self) -> Decimal:
        if not self.instruments:
            return Decimal("0")
        return sum(
            (item.coverage_pct for item in self.instruments),
            start=Decimal("0"),
        ) / Decimal(len(self.instruments))

    @property
    def is_usable(self) -> bool:
        return self.quality_status in (
            DatasetQualityStatus.PASSED,
            DatasetQualityStatus.WARNINGS,
        )

    @property
    def manifest(self) -> DatasetManifest:
        """兼容 #58 的基础 manifest 契约。"""

        return DatasetManifest(
            dataset_name=self.dataset_name,
            source=self.source,
            version=self.version,
            start_date=self.start_date,
            end_date=self.end_date,
            row_count=self.row_count,
            symbol_count=self.symbol_count,
            coverage_pct=self.coverage_pct,
            checksum=self.release_checksum,
            quality_status=self.quality_status,
            quality_report=self.quality_report,
            published_at=self.published_at,
            code_version=self.code_version,
        )

    def instrument(self, code: str) -> ReleasedInstrument:
        for item in self.instruments:
            if item.code == code:
                if not item.ready:
                    raise ReleaseCapabilityError(
                        f"标的 {code} 在发布 {self.release_id} 中未就绪: {item.issues}"
                    )
                return item
        raise ReleaseCapabilityError(
            f"标的 {code} 不在发布 {self.release_id} 中,禁止回退到外部数据源"
        )

    def require_capability(self, key: str) -> AssetCapability:
        for capability in self.capabilities:
            if capability.key == key:
                if not capability.ready:
                    raise ReleaseCapabilityError(
                        f"发布 {self.release_id} 的 {key} 能力不完整: "
                        f"{capability.missing_requirements}"
                    )
                return capability
        raise ReleaseCapabilityError(f"发布 {self.release_id} 未声明 {key} 能力")

    def as_dict(self) -> dict[str, object]:
        return {
            "release_id": self.release_id,
            "dataset_name": self.dataset_name,
            "source": self.source,
            "version": self.version,
            "schema_version": self.schema_version,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "period": self.period.value,
            "adjustment": self.adjustment,
            "fields": list(self.fields),
            "dataset_kind": self.dataset_kind.value,
            "availability_rules": [
                {"dataset": dataset, "rule": rule} for dataset, rule in self.availability_rules
            ],
            "code_version": self.code_version,
            "published_at": self.published_at.isoformat(),
            "instruments": [item.as_dict() for item in self.instruments],
            "capabilities": [item.as_dict() for item in self.capabilities],
            "quality_status": self.quality_status.value,
            "quality_report": self.quality_report,
            "known_limitations": list(self.known_limitations),
            "storage_uri": self.storage_uri,
            "metadata_version": self.metadata_version,
            "release_checksum": self.release_checksum,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> ResearchDatasetRelease:
        availability_raw = cast(list[dict[str, object]], raw.get("availability_rules", []))
        instruments_raw = cast(list[dict[str, object]], raw.get("instruments", []))
        capabilities_raw = cast(list[dict[str, object]], raw.get("capabilities", []))
        quality_report = cast(dict[str, object], raw.get("quality_report", {}))
        return cls(
            release_id=str(raw["release_id"]),
            dataset_name=str(raw["dataset_name"]),
            source=str(raw["source"]),
            version=str(raw["version"]),
            schema_version=str(raw["schema_version"]),
            start_date=date.fromisoformat(str(raw["start_date"])),
            end_date=date.fromisoformat(str(raw["end_date"])),
            period=BarPeriod(str(raw["period"])),
            adjustment=str(raw["adjustment"]),
            fields=tuple(str(item) for item in cast(list[object], raw["fields"])),
            dataset_kind=ReleaseDatasetKind(
                str(raw.get("dataset_kind", ReleaseDatasetKind.BARS.value))
            ),
            availability_rules=tuple(
                (str(item["dataset"]), str(item["rule"])) for item in availability_raw
            ),
            code_version=str(raw.get("code_version", "")),
            published_at=datetime.fromisoformat(str(raw["published_at"])),
            instruments=tuple(ReleasedInstrument.from_dict(item) for item in instruments_raw),
            capabilities=tuple(AssetCapability.from_dict(item) for item in capabilities_raw),
            quality_status=DatasetQualityStatus(str(raw["quality_status"])),
            quality_report=quality_report,
            known_limitations=tuple(
                str(item) for item in cast(list[object], raw.get("known_limitations", []))
            ),
            storage_uri=str(raw.get("storage_uri", "")),
            metadata_version=str(raw.get("metadata_version", ASSET_METADATA_VERSION)),
            release_checksum=str(raw.get("release_checksum", "")),
        )

    def symbol_codes(self) -> frozenset[str]:
        """发布标的代码集合(一致性校验 / diff 用)。"""
        return frozenset(item.code for item in self.instruments)


def symbol_set_diff(
    release_a: ResearchDatasetRelease,
    release_b: ResearchDatasetRelease,
    *,
    preview_limit: int = 200,
) -> dict[str, object]:
    """两份发布的标的集 diff(issue #252):计数 + 具名清单(有界预览)。

    供发布后自检(如 financial_indicators vs bars 主发布的并集一致性):
    ``only_in_release`` / ``only_in_other`` 是差集的字典序预览(截断由
    ``truncated`` 标注),计数始终是全量精确值,与 #206 返回瘦身精神一致。
    """
    codes_a = release_a.symbol_codes()
    codes_b = release_b.symbol_codes()
    only_a = sorted(codes_a - codes_b)
    only_b = sorted(codes_b - codes_a)
    return {
        "release_id": release_a.release_id,
        "other_release_id": release_b.release_id,
        "symbol_count_a": len(codes_a),
        "symbol_count_b": len(codes_b),
        "common_count": len(codes_a & codes_b),
        "only_in_release_count": len(only_a),
        "only_in_other_count": len(only_b),
        "only_in_release": only_a[:preview_limit],
        "only_in_other": only_b[:preview_limit],
        "preview_limit": preview_limit,
        "truncated": len(only_a) > preview_limit or len(only_b) > preview_limit,
        "consistent": not only_a and not only_b,
    }


@dataclass(frozen=True, slots=True)
class _BarAudit:
    start_date: date
    end_date: date
    expected_sessions: int
    missing_sessions: int
    suspended_sessions: int
    anomaly_count: int
    coverage_pct: Decimal
    category: str
    issues: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class _ResearchAudit:
    """研究数据(非 bars)逐标的冻结审计。"""

    start_date: date
    end_date: date
    expected_sessions: int
    missing_sessions: int
    record_count: int
    field_null_counts: dict[str, int]
    coverage_pct: Decimal
    category: str
    issues: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PointInTimeBar:
    """带机器可判定 ``available_at`` 的冻结 Bar 视图。"""

    bar: Bar
    available_at: datetime


@dataclass(frozen=True, slots=True)
class PointInTimePrice:
    """价格特征计算所需的轻量 PIT 收盘价视图。"""

    timestamp: datetime
    close: Decimal
    available_at: datetime


@runtime_checkable
class ResearchDataReleaseSource(Protocol):
    """按数据集类型批量读取研究数据记录的注入点(发布冻结输入)。"""

    async def daily_metrics(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[DailySecurityMetrics]]:
        """返回 {symbol: [PIT 时点化的每日指标记录]},按 available_at 升序。"""
        ...

    async def financial_indicators(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[FinancialIndicator]]:
        """返回 {symbol: [PIT 时点化的财务公告修订]},按 available_at 升序。"""
        ...


class FrozenDatasetReleaseBuilder:
    """把可变 Parquet 缓存原子冻结为不可变研究发布。"""

    def __init__(
        self,
        *,
        cache_dir: str | Path,
        release_root: str | Path,
        max_concurrency: int = 8,
        research_source: ResearchDataReleaseSource | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir).resolve()
        self._release_root = Path(release_root).resolve()
        self._release_root.mkdir(parents=True, exist_ok=True)
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须 >= 1")
        self._max_concurrency = max_concurrency
        self._research_source = research_source

    @property
    def research_source(self) -> ResearchDataReleaseSource | None:
        """研究数据发布注入点(bars 发布为 None)。"""

        return self._research_source

    async def publish(
        self,
        spec: DatasetReleaseSpec,
        instruments: list[ReleaseInstrumentSpec],
        *,
        previous_release: ResearchDatasetRelease | None = None,
    ) -> ResearchDatasetRelease:
        """冻结并发布;失败不会覆盖已有发布或留下可读的半成品。"""

        _validate_schema_compatibility(previous_release, spec)
        if not instruments:
            raise DatasetReleaseQualityError("发布标的不能为空")
        codes = [item.code for item in instruments]
        if len(codes) != len(set(codes)):
            raise DatasetReleaseQualityError("发布标的代码重复")
        if spec.dataset_kind is not ReleaseDatasetKind.BARS:
            if self._research_source is None:
                raise DatasetReleaseQualityError(
                    f"{spec.dataset_kind.value} 发布缺少研究数据源注入"
                )
            if spec.adjustment != "none" and spec.period is not BarPeriod.D1:
                raise DatasetReleaseQualityError(
                    "研究数据发布不支持 period/adjustment(非行情数据集)"
                )

        final_dir = self._release_root / spec.release_id
        if final_dir.exists():
            existing = await asyncio.to_thread(verify_dataset_release, final_dir)
            _assert_same_release_identity(existing, spec)
            return existing

        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{spec.release_id}-",
                dir=self._release_root,
            )
        )
        try:
            target_cache = ParquetCache(
                staging / "bars",
                max_io_concurrency=self._max_concurrency,
            )
            source_cache = ParquetCache(
                self._cache_dir,
                max_io_concurrency=self._max_concurrency,
            )
            semaphore = asyncio.Semaphore(self._max_concurrency)

            async def _freeze_one(instrument: ReleaseInstrumentSpec) -> ReleasedInstrument:
                async with semaphore:
                    if spec.dataset_kind is ReleaseDatasetKind.BARS:
                        return await self._freeze_bars_instrument(
                            spec=spec,
                            instrument=instrument,
                            staging=staging,
                            target_cache=target_cache,
                            source_cache=source_cache,
                        )
                    return await self._freeze_research_instrument(
                        spec=spec,
                        instrument=instrument,
                        staging=staging,
                    )

            # 按代码排序保证输入顺序确定;并行执行后再次排序保证 manifest 稳定。
            sorted_instruments = sorted(instruments, key=lambda item: item.code)
            released_unordered = await asyncio.gather(
                *(_freeze_one(item) for item in sorted_instruments)
            )
            released = sorted(released_unordered, key=lambda item: item.code)

            capabilities = _build_capabilities(released, spec.required_capabilities)
            missing_required = [
                capability
                for capability in capabilities
                if capability.key in spec.required_capabilities and not capability.ready
            ]
            release_coverage = sum(
                (item.coverage_pct for item in released),
                start=Decimal("0"),
            ) / Decimal(len(released))
            not_ready = [item for item in released if not item.ready]
            release_sources = sorted({source for item in released for source in item.sources})
            if spec.source == "mixed" and len(release_sources) < 2:
                raise DatasetReleaseQualityError(
                    "mixed_source_requires_multiple_sources:"
                    f"actual={','.join(release_sources) or 'unknown'}"
                )
            if missing_required or not_ready or release_coverage < spec.minimum_release_coverage:
                reasons = [
                    *(
                        f"capability:{item.key}:{','.join(item.missing_requirements)}"
                        for item in missing_required
                    ),
                    *(f"instrument:{item.code}:{','.join(item.issues)}" for item in not_ready),
                ]
                if release_coverage < spec.minimum_release_coverage:
                    reasons.append(
                        f"release_coverage:{release_coverage}<{spec.minimum_release_coverage}"
                    )
                raise DatasetReleaseQualityError("; ".join(reasons))

            warnings = [f"{item.code}:{','.join(item.issues)}" for item in released if item.issues]
            quality_status = (
                DatasetQualityStatus.WARNINGS if warnings else DatasetQualityStatus.PASSED
            )
            published_at = datetime.now(UTC)
            release = ResearchDatasetRelease(
                release_id=spec.release_id,
                dataset_name=spec.dataset_name,
                source=spec.source,
                version=spec.version,
                schema_version=spec.schema_version,
                start_date=spec.start_date,
                end_date=spec.end_date,
                period=spec.period,
                adjustment=spec.adjustment,
                fields=spec.fields,
                availability_rules=spec.availability_rules,
                dataset_kind=spec.dataset_kind,
                code_version=spec.code_version,
                published_at=published_at,
                instruments=tuple(released),
                capabilities=tuple(capabilities),
                quality_status=quality_status,
                quality_report={
                    "source": spec.source,
                    "dataset_kind": spec.dataset_kind.value,
                    "period": spec.period.value,
                    "adjustment": spec.adjustment,
                    "fields": list(spec.fields),
                    "required_capabilities": list(spec.required_capabilities),
                    "minimum_symbol_coverage": str(spec.minimum_symbol_coverage),
                    "minimum_release_coverage": str(spec.minimum_release_coverage),
                    "release_coverage": str(release_coverage),
                    "coverage": _coverage_summary(released),
                    "source_policy": "mixed" if spec.source == "mixed" else "single",
                    "sources": release_sources,
                    "source_symbol_counts": dict(
                        sorted(
                            Counter(source for item in released for source in item.sources).items()
                        )
                    ),
"source_by_instrument": {item.code: list(item.sources) for item in released},
                    "instrument_metadata": {
                        "total": len(released),
                        # 缺失字段统计(issue #185):让 list_date/industry 缺失可见。
                        "missing_list_date": sum(
                            1 for item in released if item.list_date is None
                        ),
                        "missing_industry": sum(
                            1 for item in released if item.industry is None
                        ),
                        # #251:delist_date 缺失与名称历史覆盖同样可见。
                        "missing_delist_date": sum(
                            1 for item in released if item.delist_date is None
                        ),
                        "with_name_history": sum(
                            1 for item in released if item.name_history
                        ),
                        "name_history_coverage": (
                            f"{sum(1 for item in released if item.name_history) / len(released):.4f}"
                            if released
                            else "0.0000"
                        ),
                    },
                    "warnings": warnings,
                },
                known_limitations=spec.known_limitations,
                storage_uri=spec.release_id,
            )
            checksum = _release_checksum(release)
            release = replace(release, release_checksum=checksum)
            await asyncio.to_thread(_write_manifest, staging, release)

            await self._atomically_publish(spec, staging, final_dir)
            return replace(release, storage_uri=spec.release_id)
        except Exception:
            await asyncio.to_thread(shutil.rmtree, staging, True)
            raise

    async def _atomically_publish(
        self,
        spec: DatasetReleaseSpec,
        staging: Path,
        final_dir: Path,
    ) -> None:
        """把 staging 原子改名为最终发布目录(有界重试)。

        Windows 上杀软 / 目录同步可能瞬时锁定刚写入的目录树,导致
        ``os.replace`` 抛 PermissionError(WinError 5,与 POSIX rename 语义
        不同);做有界退避重试,仍失败则原样抛出。最终目录若在等待期间出现
        (并发发布完成),按「已存在」语义校验后返回,不重复覆盖。
        """
        for attempt in range(4):
            if await asyncio.to_thread(final_dir.exists):
                existing = await asyncio.to_thread(verify_dataset_release, final_dir)
                _assert_same_release_identity(existing, spec)
                return
            try:
                # 最终目录不存在时 rename 在同一文件系统内为原子操作。
                await asyncio.to_thread(staging.replace, final_dir)
                return
            except PermissionError:
                if attempt == 3:
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))
        raise RuntimeError("发布目录原子改名未完成")

    async def _freeze_research_instrument(
        self,
        *,
        spec: DatasetReleaseSpec,
        instrument: ReleaseInstrumentSpec,
        staging: Path,
    ) -> ReleasedInstrument:
        """从注入的研究数据源冻结非 bars 数据集(daily_metrics / financial_indicators)。"""

        kinds_dir = staging / spec.dataset_kind.value
        kinds_dir.mkdir(parents=True, exist_ok=True)
        records = await self._load_research_records(spec, instrument)
        if not records:
            raise DatasetReleaseQualityError(
                f"{instrument.code}:no_{spec.dataset_kind.value}_records_in_release_range"
            )
        audit = _audit_research_records(
            records,
            instrument=instrument,
            spec=spec,
        )
        artifact_path = f"{spec.dataset_kind.value}/{instrument.code}.parquet"
        artifact = kinds_dir / f"{instrument.code}.parquet"
        await asyncio.to_thread(
            _write_research_records,
            artifact,
            records,
            fields=spec.fields,
            dataset_kind=spec.dataset_kind,
        )
        checksum = await asyncio.to_thread(_sha256_file, artifact)
        # 研究数据 quality:`all_null_fields` 与逐标的 coverage 缺口都只是可见
        # warning——财务指标字段稀疏是常态;issue #212 全市场实测 5534 只中
        # 1421 只跨度口径 coverage<0.98(停牌日 daily_basic 无截面是 A 股常态,
        # 研究数据无停复牌事件表可查,不像 bars 路径有停牌感知),逐标的硬门
        # 会让任何真实全市场研究发布不可发布。ready 只由元数据完整决定;
        # 发布级平均覆盖率 minimum_release_coverage 仍是硬门。与 bars 的
        # quality_status=WARNINGS 语义一致——缺失可见而非静默。
        ready = instrument.metadata_complete
        issues = list(audit.issues)
        if audit.coverage_pct < spec.minimum_symbol_coverage:
            issues.append(
                f"coverage:{audit.coverage_pct}<{spec.minimum_symbol_coverage}"
            )
        if not instrument.metadata_complete:
            issues.append("metadata_incomplete")
        return ReleasedInstrument(
            code=instrument.code,
            name=instrument.name,
            market=instrument.market,
            instrument_type=instrument.instrument_type,
            asset_class=instrument.asset_class,
            available_at=instrument.available_at,
            execution=instrument.execution,
            artifact_path=artifact_path,
            artifact_checksum=checksum,
            artifact_size=artifact.stat().st_size,
            row_count=audit.record_count,
            start_date=audit.start_date,
            end_date=audit.end_date,
            expected_sessions=audit.expected_sessions,
            missing_sessions=audit.missing_sessions,
            suspended_sessions=0,
            anomaly_count=0,
            coverage_pct=audit.coverage_pct,
            category=audit.category,
            ready=ready,
            issues=tuple(issues),
            sources=tuple(
                sorted({str(_research_record_value(record, "source")) for record in records})
            ),
            exchange=instrument.exchange,
            listing_board=instrument.listing_board,
            currency=instrument.currency,
            etf_category=instrument.etf_category,
            list_date=instrument.list_date,
            delist_date=instrument.delist_date,
            industry=instrument.industry,
            status=instrument.status,
            metadata_complete=instrument.metadata_complete,
            lifecycle_events=instrument.lifecycle_events,
            present_event_types=instrument.present_event_types,
            required_event_types=instrument.required_event_types,
            name_history=instrument.name_history,
        )

    async def _load_research_records(
        self,
        spec: DatasetReleaseSpec,
        instrument: ReleaseInstrumentSpec,
    ) -> list[object]:
        """按 dataset_kind 从注入源加载一条标的的全部研究记录。"""
        assert self._research_source is not None
        if spec.dataset_kind is ReleaseDatasetKind.DAILY_METRICS:
            daily_records = await self._research_source.daily_metrics(
                symbols=[instrument.code],
                start_date=spec.start_date,
                end_date=spec.end_date,
            )
            return sorted(daily_records[instrument.code], key=_research_record_available_at)
        if spec.dataset_kind is ReleaseDatasetKind.FINANCIAL_INDICATORS:
            indicator_records = await self._research_source.financial_indicators(
                symbols=[instrument.code],
                start_date=spec.start_date,
                end_date=spec.end_date,
            )
            return sorted(
                indicator_records[instrument.code],
                key=_research_record_available_at,
            )
        raise DatasetReleaseQualityError(f"不支持的发布数据集类型: {spec.dataset_kind}")

    async def _freeze_bars_instrument(
            self,
            *,
            spec: DatasetReleaseSpec,
            instrument: ReleaseInstrumentSpec,
            staging: Path,
            target_cache: ParquetCache,
            source_cache: ParquetCache,
        ) -> ReleasedInstrument:
            source_path = _cache_path(
                self._cache_dir,
                instrument.code,
                spec.period,
                spec.adjustment,
            )
            if not source_path.exists() or source_path.is_symlink():
                raise DatasetReleaseQualityError(f"{instrument.code}:source_artifact_missing")
            before = source_path.stat()
            try:
                bars = await source_cache.read(
                    Symbol(code=instrument.code, market=instrument.market),
                    spec.period,
                    spec.adjustment,
                )
            except Exception as exc:
                raise DatasetReleaseQualityError(
                    f"{instrument.code}:source_read_failed:{type(exc).__name__}"
                ) from exc
            # 读取缓存元数据(包含已查询过的合法无 Bar 区间,如停牌/上市前)。
            # 与批量拉取的日期命中逻辑一致(issue #99):只要某交易日落在
            # covered_ranges 或 Bar 首尾区间内,就算"已成功查询过",不重复
            # 请求也不在发布审计中计为 missing。
            try:
                metadata = await source_cache.metadata_for(
                    Symbol(code=instrument.code, market=instrument.market),
                    spec.period,
                    spec.adjustment,
                )
            except Exception as exc:
                raise DatasetReleaseQualityError(
                    f"{instrument.code}:source_metadata_failed:{type(exc).__name__}"
                ) from exc
            after = source_path.stat()
            if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                raise DatasetReleaseQualityError(f"{instrument.code}:source_changed_during_release")
            frozen_bars = [
                bar for bar in bars if spec.start_date <= bar.timestamp.date() <= spec.end_date
            ]
            if not frozen_bars:
                raise DatasetReleaseQualityError(f"{instrument.code}:no_bars_in_release_range")
            known_sources = {bar.source for bar in frozen_bars if bar.source}
            if not known_sources:
                raise DatasetReleaseQualityError(f"{instrument.code}:source_metadata_missing")
            if spec.source != "mixed" and known_sources != {spec.source}:
                raise DatasetReleaseQualityError(
                    f"{instrument.code}:source_mismatch:"
                    f"cache={','.join(sorted(known_sources))},release={spec.source}"
                )
            audit = _audit_bars(
                frozen_bars,
                instrument=instrument,
                spec=spec,
                metadata=metadata,
            )
            total_bars = len(frozen_bars)
            anomaly_ratio = (
                Decimal(audit.anomaly_count) / Decimal(total_bars) if total_bars > 0 else Decimal("1")
            )
            ready = (
                anomaly_ratio <= spec.max_anomaly_ratio
                and audit.coverage_pct >= spec.minimum_symbol_coverage
                and instrument.metadata_complete
                and set(instrument.required_event_types).issubset(instrument.present_event_types)
            )
            issues = list(audit.issues)
            if anomaly_ratio > spec.max_anomaly_ratio:
                issues.append(f"anomaly_ratio:{anomaly_ratio:.4f}>{spec.max_anomaly_ratio}")
            if not instrument.metadata_complete:
                issues.append("metadata_incomplete")
            missing_events = sorted(
                set(instrument.required_event_types) - set(instrument.present_event_types)
            )
            if missing_events:
                issues.append(f"missing_events:{','.join(missing_events)}")
            if audit.coverage_pct < spec.minimum_symbol_coverage:
                issues.append(f"coverage:{audit.coverage_pct}<{spec.minimum_symbol_coverage}")

            await target_cache.write(
                Symbol(code=instrument.code, market=instrument.market),
                spec.period,
                spec.adjustment,
                frozen_bars,
            )
            artifact = _cache_path(
                staging / "bars",
                instrument.code,
                spec.period,
                spec.adjustment,
            )
            checksum = await asyncio.to_thread(_sha256_file, artifact)
            relative = artifact.relative_to(staging).as_posix()
            return ReleasedInstrument(
                code=instrument.code,
                name=instrument.name,
                market=instrument.market,
                instrument_type=instrument.instrument_type,
                asset_class=instrument.asset_class,
                available_at=instrument.available_at,
                execution=instrument.execution,
                artifact_path=relative,
                artifact_checksum=checksum,
                artifact_size=artifact.stat().st_size,
                row_count=len(frozen_bars),
                start_date=audit.start_date,
                end_date=audit.end_date,
                expected_sessions=audit.expected_sessions,
                missing_sessions=audit.missing_sessions,
                suspended_sessions=audit.suspended_sessions,
                anomaly_count=audit.anomaly_count,
                coverage_pct=audit.coverage_pct,
                category=audit.category,
                ready=ready,
                issues=tuple(issues),
                sources=tuple(sorted(known_sources)),
                exchange=instrument.exchange,
                listing_board=instrument.listing_board,
                currency=instrument.currency,
                etf_category=instrument.etf_category,
                list_date=instrument.list_date,
                delist_date=instrument.delist_date,
                industry=instrument.industry,
                status=instrument.status,
                metadata_complete=instrument.metadata_complete,
                lifecycle_events=instrument.lifecycle_events,
                present_event_types=instrument.present_event_types,
                required_event_types=instrument.required_event_types,
                name_history=instrument.name_history,
            )





def _audit_research_records(
    records: list[object],
    *,
    instrument: ReleaseInstrumentSpec,
    spec: DatasetReleaseSpec,
) -> _ResearchAudit:
    """对研究数据记录做覆盖审计与缺失字段统计。"""
    available_at_values = [
        _research_record_value(item, "available_at") for item in records
    ]
    dates = [
        (
            value.date()
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value)).date()
        )
        for value in available_at_values
    ]
    start_date = min(dates)
    end_date = max(dates)
    effective_list = instrument.list_date or start_date
    effective_delist = instrument.delist_date or end_date

    if spec.dataset_kind is ReleaseDatasetKind.DAILY_METRICS:
        expected_dates = _trading_days(
            max(spec.start_date, effective_list),
            min(spec.end_date, effective_delist),
        )
        covered_dates = {
            _research_record_date(item, "trade_date") for item in records
        }
        expected_sessions = len(expected_dates)
        missing_sessions = len(expected_dates - covered_dates)
        coverage = (
            Decimal(len(expected_dates & covered_dates)) / Decimal(expected_sessions)
            if expected_sessions > 0
            else Decimal("1")
        )
        if instrument.delist_date is not None and instrument.delist_date <= spec.end_date:
            category = "delisted"
        elif instrument.list_date is not None and instrument.list_date > spec.start_date:
            category = "short_history"
        elif missing_sessions:
            category = "gaps"
        else:
            category = "full"
    else:
        # financial_indicators:按报告期(quarter)覆盖,不按交易日。
        expected_dates = _quarter_end_dates(
            max(spec.start_date, effective_list),
            min(spec.end_date, effective_delist),
        )
        covered_dates = {
            _research_record_date(item, "report_period") for item in records
        }
        expected_sessions = len(expected_dates)
        missing_sessions = len(expected_dates - covered_dates)
        coverage = (
            Decimal(len(expected_dates & covered_dates)) / Decimal(expected_sessions)
            if expected_sessions > 0
            else Decimal("1")
        )
        category = "full" if not missing_sessions else "gaps"

    field_names = set(spec.fields) - set(RESEARCH_RELEASE_METADATA_FIELDS)
    field_null_counts: dict[str, int] = {}
    for field_name in sorted(field_names):
        field_null_counts[field_name] = sum(
            1
            for item in records
            if _research_record_value(item, field_name) is None
        )
    issues: list[str] = []
    if missing_sessions:
        issues.append(f"missing_sessions:{missing_sessions}")
    stale_fields = [
        field_name
        for field_name, null_count in field_null_counts.items()
        if null_count == len(records)
    ]
    if stale_fields:
        issues.append(f"all_null_fields:{','.join(stale_fields)}")
    return _ResearchAudit(
        start_date=start_date,
        end_date=end_date,
        expected_sessions=expected_sessions,
        missing_sessions=missing_sessions,
        record_count=len(records),
        field_null_counts=field_null_counts,
        coverage_pct=coverage,
        category=category,
        issues=tuple(issues),
    )


def _quarter_end_dates(start: date, end: date) -> set[date]:
    """返回 ``[start, end]`` 内的 A 股季度报告期末日期(3/31 6/30 9/30 12/31)。"""
    result: set[date] = set()
    year, month = start.year, 3
    while True:
        cursor = date(year, month, _QUARTER_END_DAY[month])
        if cursor > end:
            break
        if cursor >= start:
            result.add(cursor)
        month += 3
        if month > 12:
            month = 3
            year += 1
    return result


_QUARTER_END_DAY: dict[int, int] = {3: 31, 6: 30, 9: 30, 12: 31}


def _research_record_date(item: object, field_name: str) -> date:
    """把研究记录中的日期字段规范化为 ``date``。"""
    value = _research_record_value(item, field_name)
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value))


def _research_record_value(item: object, field_name: str) -> object:
    """从研究数据记录读取字段(DailySecurityMetrics/FinancialIndicator 或字典)。"""
    if isinstance(item, dict):
        return item.get(field_name)
    return getattr(item, field_name)


def _research_record_available_at(item: object) -> datetime:
    value = _research_record_value(item, "available_at")
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _research_value_to_scalar(value: object) -> object:
    """把研究字段值转换为 parquet 可写标量(Decimal→float,datetime→iso)。"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _write_research_records(
    path: Path,
    records: list[object],
    *,
    fields: tuple[str, ...],
    dataset_kind: ReleaseDatasetKind,
) -> None:
    """把研究数据记录冻结为 parquet(每标的一文件,列=fields 白名单字段)。"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    key_field = (
        "trade_date"
        if dataset_kind is ReleaseDatasetKind.DAILY_METRICS
        else "report_period"
    )
    columns: dict[str, object] = {
        key_field: [_research_record_date(item, key_field) for item in records],
        "available_at": [
            _research_value_to_scalar(_research_record_value(item, "available_at"))
            for item in records
        ],
        "observed_at": [
            _research_value_to_scalar(_research_record_value(item, "observed_at"))
            for item in records
        ],
        "source": [str(_research_record_value(item, "source")) for item in records],
    }
    for field_name in fields:
        if field_name in columns or field_name in RESEARCH_RELEASE_METADATA_FIELDS:
            continue
        columns[field_name] = [
            _research_value_to_scalar(_research_record_value(item, field_name))
            for item in records
        ]
    table = pa.table(columns)
    pq.write_table(table, path)


def _read_research_records(
    path: Path,
    *,
    kind: ReleaseDatasetKind,
) -> list[dict[str, object]]:
    """读回研究数据发布 parquet,还原为记录字典(字段值已规范化)。"""
    import pyarrow.parquet as pq

    table = pq.read_table(path, use_threads=False, pre_buffer=False)
    rows: list[dict[str, object]] = []
    for row in table.to_pylist():
        normalized: dict[str, object] = {}
        for key, value in row.items():
            if value is None:
                normalized[key] = None
            elif isinstance(value, (datetime, date)):
                normalized[key] = value
            else:
                normalized[key] = value
        rows.append(normalized)
    return rows


def _daily_metrics_from_release_row(
    row: dict[str, object],
    *,
    symbol: str,
) -> DailySecurityMetrics:
    """把 daily_metrics 发布行还原为领域记录(未冻结字段为 None)。"""
    value = row.get("available_at") or row.get("observed_at")
    available_at = _coerce_datetime(value)
    trade_date = _coerce_date(row.get("trade_date"))
    if trade_date is None:
        raise DatasetReleaseQualityError("研究数据记录缺少 trade_date")
    return DailySecurityMetrics(
        symbol=symbol,
        trade_date=trade_date,
        close=_coerce_decimal(row.get("close")),
        turnover_rate=_coerce_decimal(row.get("turnover_rate")),
        turnover_rate_free=_coerce_decimal(row.get("turnover_rate_free")),
        volume_ratio=_coerce_decimal(row.get("volume_ratio")),
        pe=_coerce_decimal(row.get("pe")),
        pe_ttm=_coerce_decimal(row.get("pe_ttm")),
        pb=_coerce_decimal(row.get("pb")),
        ps=_coerce_decimal(row.get("ps")),
        ps_ttm=_coerce_decimal(row.get("ps_ttm")),
        dividend_yield=_coerce_decimal(row.get("dividend_yield")),
        dividend_yield_ttm=_coerce_decimal(row.get("dividend_yield_ttm")),
        total_shares=_coerce_decimal(row.get("total_shares")),
        float_shares=_coerce_decimal(row.get("float_shares")),
        free_shares=_coerce_decimal(row.get("free_shares")),
        total_market_cap=_coerce_decimal(row.get("total_market_cap")),
        circulating_market_cap=_coerce_decimal(row.get("circulating_market_cap")),
        limit_status=_coerce_int(row.get("limit_status")),
        source=str(row.get("source") or ""),
        observed_at=available_at,
        available_at=available_at,
    )


def _financial_indicator_from_release_row(
    row: dict[str, object],
    *,
    symbol: str,
) -> FinancialIndicator:
    """把 financial_indicators 发布行还原为领域记录(未冻结字段为 None)。"""
    value = row.get("available_at") or row.get("observed_at")
    available_at = _coerce_datetime(value)
    announcement_date = _coerce_date(row.get("announcement_date"))
    report_period = _coerce_date(row.get("report_period"))
    if announcement_date is None or report_period is None:
        raise DatasetReleaseQualityError("研究数据记录缺少公告日或报告期")
    return FinancialIndicator(
        symbol=symbol,
        announcement_date=announcement_date,
        report_period=report_period,
        update_flag=str(row.get("update_flag") or "") or None,
        eps=_coerce_decimal(row.get("eps")),
        diluted_eps=_coerce_decimal(row.get("diluted_eps")),
        book_value_per_share=_coerce_decimal(row.get("book_value_per_share")),
        operating_cash_flow_per_share=_coerce_decimal(
            row.get("operating_cash_flow_per_share")
        ),
        return_on_equity=_coerce_decimal(row.get("return_on_equity")),
        weighted_return_on_equity=_coerce_decimal(
            row.get("weighted_return_on_equity")
        ),
        gross_profit_margin=_coerce_decimal(row.get("gross_profit_margin")),
        net_profit_margin=_coerce_decimal(row.get("net_profit_margin")),
        debt_to_assets=_coerce_decimal(row.get("debt_to_assets")),
        revenue_yoy=_coerce_decimal(row.get("revenue_yoy")),
        net_profit_yoy=_coerce_decimal(row.get("net_profit_yoy")),
        operating_cash_flow_yoy=_coerce_decimal(row.get("operating_cash_flow_yoy")),
        source=str(row.get("source") or ""),
        observed_at=available_at,
        available_at=available_at,
    )


def _coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if value is None:
        raise DatasetReleaseQualityError("研究数据记录缺少 available_at")
    return datetime.fromisoformat(str(value))


def _coerce_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _coerce_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
    return int(str(value))


class FrozenReleaseProvider:
    """只读取一个指定冻结发布的历史数据 Provider。"""

    def __init__(
        self,
        *,
        release_root: str | Path,
        release_id: str,
        verify_files: bool = True,
        max_concurrency: int = 8,
        expected_checksum: str | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须 >= 1")
        self._release_dir = Path(release_root).resolve() / release_id
        self._release = load_dataset_release(
            self._release_dir,
            expected_checksum=expected_checksum,
        )
        if self._release.release_id != release_id:
            raise ReleaseIntegrityError("目录 release_id 与 manifest 不一致")
        if not self._release.is_usable:
            raise DatasetReleaseQualityError(
                f"发布 {release_id} quality={self._release.quality_status.value}"
            )
        self._verified: set[str] = set()
        self._verify_files = verify_files
        self._cache = ParquetCache(
            self._release_dir / "bars",
            max_io_concurrency=max_concurrency,
        )

    @property
    def release(self) -> ResearchDatasetRelease:
        return self._release

    @property
    def release_dir(self) -> Path:
        """返回已通过 manifest 校验的冻结发布目录。"""

        return self._release_dir

    @property
    def verify_files(self) -> bool:
        """是否要求读取前校验逐文件 checksum。"""

        return self._verify_files

    async def fetch_bars(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[Bar]:
        item = self._validate_fetch_request(symbol, period, start, end, adjust)
        await self._verified_artifact(item)
        bars = await self._cache.read(symbol, period, adjust)
        return ParquetCache.filter_by_date(bars, start, end)

    def _validate_fetch_request(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        adjust: str,
    ) -> ReleasedInstrument:
        if period is not self._release.period:
            raise ReleaseCapabilityError(
                f"发布周期为 {self._release.period.value},请求 {period.value}"
            )
        if adjust != self._release.adjustment:
            raise ReleaseCapabilityError(f"发布复权为 {self._release.adjustment},请求 {adjust}")
        if start < self._release.start_date or end > self._release.end_date:
            raise ReleaseCapabilityError(
                f"请求 {start}~{end} 超出发布范围 "
                f"{self._release.start_date}~{self._release.end_date}"
            )
        item = self._release.instrument(symbol.code)
        if item.market is not symbol.market:
            raise ReleaseCapabilityError(
                f"{symbol.code} 市场应为 {item.market.value},请求 {symbol.market.value}"
            )
        return item

    async def _verified_artifact(self, item: ReleasedInstrument) -> Path:
        artifact = _safe_release_artifact(self._release_dir, item.artifact_path)
        if self._verify_files and item.code not in self._verified:
            actual = await asyncio.to_thread(_sha256_file, artifact)
            if actual != item.artifact_checksum:
                raise ReleaseIntegrityError(
                    f"{item.code} 文件校验和不一致: expected="
                    f"{item.artifact_checksum} actual={actual}"
                )
            self._verified.add(item.code)
        return artifact

    async def fetch_point_in_time_bars(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        decision_at: datetime,
        adjust: str = "qfq",
    ) -> list[PointInTimeBar]:
        """只暴露 ``available_at <= decision_at`` 的 Bar。

        日线按对应市场收盘后时点暴露;分钟线沿用其带时区 timestamp。调用方若要
        做历史逐日决策,应使用本接口或等价的事件回放门,不能提前读取未来 Bar。
        """

        if decision_at.tzinfo is None:
            raise ValueError("decision_at 必须带时区")
        item = self._release.instrument(symbol.code)
        bars = await self.fetch_bars(
            symbol,
            period,
            start,
            end,
            adjust=adjust,
        )
        result = [
            PointInTimeBar(
                bar=bar,
                available_at=_bar_available_at(bar, item),
            )
            for bar in bars
        ]
        return [item for item in result if item.available_at <= decision_at]

    async def fetch_point_in_time_prices(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        decision_at: datetime,
        adjust: str = "qfq",
    ) -> list[PointInTimePrice]:
        """读取价格特征所需的轻量 PIT 视图。

        日线只解码 Parquet 的 ``timestamp``/``close`` 两列;其它周期回退到
        通用 Bar 读取,保持原有 PIT 语义。
        """

        if decision_at.tzinfo is None:
            raise ValueError("decision_at 必须带时区")
        item = self._validate_fetch_request(symbol, period, start, end, adjust)
        if period is BarPeriod.D1:
            await self._verified_artifact(item)
            points = await self._cache.read_close_points(
                symbol,
                period,
                adjust,
                start=start,
                end=end,
            )
            result = [
                PointInTimePrice(
                    timestamp=timestamp,
                    close=close,
                    available_at=_timestamp_available_at(
                        timestamp,
                        period,
                        item,
                    ),
                )
                for timestamp, close in points
            ]
            return [point for point in result if point.available_at <= decision_at]

        bars = await self.fetch_point_in_time_bars(
            symbol,
            period,
            start,
            end,
            decision_at=decision_at,
            adjust=adjust,
        )
        return [
            PointInTimePrice(
                timestamp=point.bar.timestamp,
                close=point.bar.close,
                available_at=point.available_at,
            )
            for point in bars
        ]

    async def fetch_daily_metrics(
        self,
        symbol: Symbol,
        *,
        start: date,
        end: date,
        decision_at: datetime,
    ) -> list[DailySecurityMetrics]:
        """读取 ``daily_metrics`` 发布的时点化每日指标记录(PIT 门控)。"""
        rows = await self._fetch_research_records(
            ReleaseDatasetKind.DAILY_METRICS,
            symbol,
            start=start,
            end=end,
            decision_at=decision_at,
        )
        return [
            _daily_metrics_from_release_row(row, symbol=symbol.code)
            for row in rows
        ]

    async def fetch_financial_indicators(
        self,
        symbol: Symbol,
        *,
        decision_at: datetime,
    ) -> list[FinancialIndicator]:
        """读取 ``financial_indicators`` 发布在决策时点可见的全部公告修订。"""
        rows = await self._fetch_research_records(
            ReleaseDatasetKind.FINANCIAL_INDICATORS,
            symbol,
            start=None,
            end=None,
            decision_at=decision_at,
        )
        return [
            _financial_indicator_from_release_row(row, symbol=symbol.code)
            for row in rows
        ]

    async def _fetch_research_records(
        self,
        kind: ReleaseDatasetKind,
        symbol: Symbol,
        *,
        start: date | None,
        end: date | None,
        decision_at: datetime,
    ) -> list[dict[str, object]]:
        """按 kind 读取研究数据发布记录;非对应 kind 的发布 fail-closed。"""
        if decision_at.tzinfo is None:
            raise ValueError("decision_at 必须带时区")
        if self._release.dataset_kind is not kind:
            raise ReleaseCapabilityError(
                f"发布 {self._release.release_id} 是 {self._release.dataset_kind.value} "
                f"数据集,不能按 {kind.value} 读取"
            )
        item = self._release.instrument(symbol.code)
        artifact = await self._verified_artifact(item)
        rows = await asyncio.to_thread(
            _read_research_records,
            artifact,
            kind=kind,
        )
        result: list[dict[str, object]] = []
        for row in rows:
            available_at = _research_record_available_at(row)
            if available_at > decision_at:
                continue
            if start is not None and end is not None:
                if kind is ReleaseDatasetKind.DAILY_METRICS:
                    record_date = _coerce_date(row.get("trade_date"))
                else:
                    record_date = _coerce_date(row.get("report_period"))
                if record_date is None:
                    raise ReleaseIntegrityError(f"{self._release.release_id} 研究记录缺少日期字段")
                if start <= record_date <= end:
                    result.append(row)
            else:
                result.append(row)
        return result


def load_dataset_release(
    release_dir: str | Path,
    *,
    expected_checksum: str | None = None,
) -> ResearchDatasetRelease:
    """从磁盘加载并验证 manifest 自身校验和。

    ``expected_checksum``(DB 锚定,取 ``research_dataset_releases.release_checksum``)
    提供时与 manifest 内嵌 ``release_checksum`` 直接比对,**不做**依赖当前代码
    ``as_dict()`` 输出的整清单重算——发布端与读取端代码版本漂移(字段增删、
    git SHA 漂移、工作区 dirty)不会让刚发布的冻结数据被拒读。未提供时保留
    重算路径(纯磁盘独立场景与向后兼容);文件级完整性两种路径下均由逐文件
    ``artifact_checksum`` sha256 保证。
    """

    root = Path(release_dir).resolve()
    manifest_path = root / RELEASE_MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ReleaseIntegrityError(f"发布清单不存在: {manifest_path}")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseIntegrityError(f"发布清单无法读取: {manifest_path}") from exc
    if not isinstance(raw, dict):
        raise ReleaseIntegrityError("发布清单必须是 JSON object")
    release = ResearchDatasetRelease.from_dict(cast(dict[str, object], raw))
    if expected_checksum is not None:
        if release.release_checksum != expected_checksum:
            raise ReleaseIntegrityError(
                "manifest checksum 与 DB 锚定不一致: "
                f"expected={expected_checksum} actual={release.release_checksum}"
            )
        return release
    expected = _release_checksum(release)
    if release.release_checksum != expected:
        raise ReleaseIntegrityError(
            f"manifest checksum 不一致: expected={release.release_checksum} actual={expected}"
        )
    return release


def verify_dataset_release(
    release_dir: str | Path,
) -> ResearchDatasetRelease:
    """验证 manifest 和全部文件校验和,成功时返回发布。"""

    root = Path(release_dir).resolve()
    release = load_dataset_release(root)
    for item in release.instruments:
        artifact = _safe_release_artifact(root, item.artifact_path)
        actual = _sha256_file(artifact)
        if actual != item.artifact_checksum:
            raise ReleaseIntegrityError(
                f"{item.code} 文件校验和不一致: expected={item.artifact_checksum} actual={actual}"
            )
    return release


def _audit_bars(
    bars: list[Bar],
    *,
    instrument: ReleaseInstrumentSpec,
    spec: DatasetReleaseSpec,
    metadata: CacheMetadata | None = None,
) -> _BarAudit:
    checker = BarQualityChecker()
    qr = checker.check(bars, symbol=instrument.code)

    unique_dates = {bar.timestamp.date() for bar in bars}
    suspended_bar_dates: set[date] = set()
    for bar in bars:
        if bar.volume == 0 and bar.open == bar.high == bar.low == bar.close:
            suspended_bar_dates.add(bar.timestamp.date())

    # bars before list_date / after delist_date
    lifecycle_anomalies = 0
    for bar in bars:
        bar_date = bar.timestamp.date()
        if instrument.list_date is not None and bar_date < instrument.list_date:
            lifecycle_anomalies += 1
        if instrument.delist_date is not None and bar_date > instrument.delist_date:
            lifecycle_anomalies += 1

    anomaly_count = qr.anomaly_count + lifecycle_anomalies + qr.duplicate_count

    first_bar_date = min(unique_dates)
    last_bar_date = max(unique_dates)
    effective_list = instrument.list_date or first_bar_date
    effective_delist = instrument.delist_date or last_bar_date
    expected_start = max(spec.start_date, effective_list)
    expected_end = min(spec.end_date, effective_delist)
    lifecycle_dates = _known_suspension_dates(
        instrument,
        start=expected_start,
        end=expected_end,
    )
    expected_dates = _trading_days(expected_start, expected_end)
    required_dates = expected_dates - lifecycle_dates
    expected_sessions = len(required_dates)

    # 与批量拉取的日期命中逻辑(issue #99)对齐:CacheMetadata.covered_ranges
    # 记录的是"已成功向远端查询过"的合法无 Bar 区间(停牌、上市前、节假日
    # 等),与 Bar 首尾区间共同决定是否需要重新请求。覆盖率审计沿用同一口径:
    # 这些区间内落在 expected_window 的交易日视为已覆盖,不计为 missing,
    # 否则会把所有数据源省略的停牌 Bar 误判为数据缺口。
    queried_dates = _covered_trading_dates(
        metadata,
        expected_start,
        expected_end,
    )
    covered_dates = unique_dates | queried_dates
    missing_sessions = len(required_dates - covered_dates)
    covered_sessions = len(required_dates & covered_dates)
    coverage = (
        Decimal(covered_sessions) / Decimal(expected_sessions)
        if expected_sessions > 0
        else Decimal("1")
    )
    suspended_dates = lifecycle_dates | (suspended_bar_dates & expected_dates)
    issues: list[str] = []
    if anomaly_count:
        issues.append(f"anomalies:{anomaly_count}")
    # 使用真实 A 股交易日历后,missing_sessions 只反映真正的数据缺口。
    if missing_sessions:
        issues.append(f"missing_sessions:{missing_sessions}")
    if suspended_dates:
        issues.append(f"suspended_sessions:{len(suspended_dates)}")

    actual_start = first_bar_date
    actual_end = last_bar_date
    if instrument.delist_date is not None and instrument.delist_date <= spec.end_date:
        category = "delisted"
    elif instrument.list_date is not None and instrument.list_date > spec.start_date:
        category = "short_history"
    elif missing_sessions:
        category = "gaps"
    else:
        category = "full"
    return _BarAudit(
        start_date=actual_start,
        end_date=actual_end,
        expected_sessions=expected_sessions,
        missing_sessions=missing_sessions,
        suspended_sessions=len(suspended_dates),
        anomaly_count=anomaly_count,
        coverage_pct=coverage,
        category=category,
        issues=tuple(issues),
    )


def _known_suspension_dates(
    instrument: ReleaseInstrumentSpec,
    *,
    start: date,
    end: date,
) -> set[date]:
    """从权威生命周期事件提取停牌窗口;复牌日恢复为必需交易日。"""

    events = sorted(
        instrument.lifecycle_events,
        key=lambda item: (item.effective_date, item.available_at),
    )
    suspended_from: date | None = None
    result: set[date] = set()
    for event in events:
        if event.event_type == "suspension_day":
            if start <= event.effective_date <= end:
                result.add(event.effective_date)
        elif event.event_type == "suspension":
            suspended_from = event.effective_date
        elif event.event_type == "resumption" and suspended_from is not None:
            result.update(
                _trading_days(
                    max(start, suspended_from),
                    min(end, event.effective_date - timedelta(days=1)),
                )
            )
            suspended_from = None
    if suspended_from is not None and instrument.status in (
        ListingStatus.SUSPENDED,
        ListingStatus.DELISTED,
    ):
        result.update(_trading_days(max(start, suspended_from), end))
    return result


def _covered_trading_dates(
    metadata: CacheMetadata | None,
    start: date,
    end: date,
) -> set[date]:
    """从缓存元数据提取落在 ``[start, end]`` 内的"已成功查询过"交易日。

    与 :meth:`finboard_data.cache.CacheMetadata.covers` / 批量拉取的
    ``_cache_fetch_ranges`` 共用同一口径:``covered_ranges`` 加上 Bar 首尾
    天然区间都属于"已成功查询过"的日期,数据源在这些区间内合法地不返回
    Bar(停牌、上市前、退市后等)不会在覆盖率审计中被误判为缺口。
    """

    if metadata is None or start > end:
        return set()
    ranges = list(metadata.covered_ranges)
    if metadata.first_date is not None and metadata.last_date is not None:
        ranges.append((metadata.first_date, metadata.last_date))
    if not ranges:
        return set()
    covered: set[date] = set()
    for range_start, range_end in ranges:
        lo = max(start, range_start)
        hi = min(end, range_end)
        if lo > hi:
            continue
        try:
            covered.update(_trading_days(lo, hi))
        except Exception:
            # 日历不可用时退化为朴素日期枚举(包含周末),覆盖率会偏宽松;
            # 与 trading_days 抛 TradingCalendarError 不同,这里是审计侧
            # best-effort,不阻断发布流程。
            cursor = lo
            while cursor <= hi:
                covered.add(cursor)
                cursor += timedelta(days=1)
    return covered


def _bar_available_at(bar: Bar, instrument: ReleasedInstrument) -> datetime:
    return _timestamp_available_at(bar.timestamp, bar.period, instrument)


def _timestamp_available_at(
    timestamp: datetime,
    period: BarPeriod,
    instrument: ReleasedInstrument | Market,
) -> datetime:
    market = instrument.market if isinstance(instrument, ReleasedInstrument) else instrument
    if period is not BarPeriod.D1:
        if timestamp.tzinfo is None:
            raise ReleaseIntegrityError("冻结 Bar timestamp 必须带时区")
        return timestamp
    if market in (Market.A_SHARE, Market.FUTURE):
        local_time = time(15, 30)
        timezone = ZoneInfo("Asia/Shanghai")
    elif market is Market.HK:
        local_time = time(16, 30)
        timezone = ZoneInfo("Asia/Hong_Kong")
    elif market is Market.US:
        local_time = time(16, 30)
        timezone = ZoneInfo("America/New_York")
    else:  # pragma: no cover - Market 是封闭枚举
        raise ReleaseCapabilityError(f"没有 {market.value} 的日线 available_at 规则")
    return datetime.combine(timestamp.date(), local_time, tzinfo=timezone).astimezone(UTC)


def _weekdays(start: date, end: date) -> set[date]:
    if start > end:
        return set()
    result: set[date] = set()
    current = start
    while current <= end:
        if current.weekday() < 5:
            result.add(current)
        current += timedelta(days=1)
    return result


def _build_capabilities(
    instruments: list[ReleasedInstrument],
    required: tuple[str, ...],
) -> list[AssetCapability]:
    keys = {
        InstrumentType.STOCK.value,
        InstrumentType.CONVERTIBLE.value,
        InstrumentType.FUTURES.value,
        *(f"etf:{category.value}" for category in EtfCategory),
        *(item.capability_key for item in instruments),
        *required,
    }
    result: list[AssetCapability] = []
    for key in sorted(keys):
        members = [item for item in instruments if item.capability_key == key]
        ready_count = sum(item.ready for item in members)
        missing: list[str] = []
        if not members:
            missing.append("no_instruments")
        for item in members:
            if not item.ready:
                missing.append(f"{item.code}:{','.join(item.issues)}")
        status = (
            CapabilityStatus.READY
            if members and ready_count == len(members)
            else CapabilityStatus.INCOMPLETE
        )
        result.append(
            AssetCapability(
                key=key,
                status=status,
                symbol_count=len(members),
                ready_count=ready_count,
                missing_requirements=tuple(missing),
            )
        )
    return result


def _coverage_summary(instruments: list[ReleasedInstrument]) -> dict[str, object]:
    categories: dict[str, int] = {}
    asset_classes: dict[str, int] = {}
    markets: dict[str, int] = {}
    listing_boards: dict[str, int] = {}
    for item in instruments:
        categories[item.category] = categories.get(item.category, 0) + 1
        asset_classes[item.asset_class.value] = asset_classes.get(item.asset_class.value, 0) + 1
        markets[item.market.value] = markets.get(item.market.value, 0) + 1
        listing_boards[item.listing_board] = listing_boards.get(item.listing_board, 0) + 1
    return {
        "categories": categories,
        "asset_classes": asset_classes,
        "markets": markets,
        "listing_boards": listing_boards,
        "missing_sessions": sum(item.missing_sessions for item in instruments),
        "suspended_sessions": sum(item.suspended_sessions for item in instruments),
        "anomaly_count": sum(item.anomaly_count for item in instruments),
        "name_history_records": sum(len(item.name_history) for item in instruments),
        "lifecycle_event_records": sum(len(item.lifecycle_events) for item in instruments),
    }


def _validate_schema_compatibility(
    previous: ResearchDatasetRelease | None,
    spec: DatasetReleaseSpec,
) -> None:
    if previous is None:
        return
    if (
        previous.dataset_name == spec.dataset_name
        and previous.source == spec.source
        and previous.schema_version == spec.schema_version
        and previous.fields != spec.fields
    ):
        raise DatasetReleaseQualityError("字段集合发生变化但 schema_version 未递增")


def _assert_same_release_identity(
    release: ResearchDatasetRelease,
    spec: DatasetReleaseSpec,
) -> None:
    actual = (
        release.release_id,
        release.dataset_name,
        release.source,
        release.version,
        release.schema_version,
        release.start_date,
        release.end_date,
        release.period,
        release.adjustment,
        release.fields,
        release.dataset_kind.value,
        release.availability_rules,
        release.code_version,
        release.known_limitations,
        tuple(cast(list[str], release.quality_report.get("required_capabilities", []))),
        str(release.quality_report.get("minimum_symbol_coverage", "")),
        str(release.quality_report.get("minimum_release_coverage", "")),
    )
    expected = (
        spec.release_id,
        spec.dataset_name,
        spec.source,
        spec.version,
        spec.schema_version,
        spec.start_date,
        spec.end_date,
        spec.period,
        spec.adjustment,
        spec.fields,
        spec.dataset_kind.value,
        spec.availability_rules,
        spec.code_version,
        spec.known_limitations,
        spec.required_capabilities,
        str(spec.minimum_symbol_coverage),
        str(spec.minimum_release_coverage),
    )
    if actual != expected:
        raise ImmutableReleaseError(f"release_id={spec.release_id} 已存在且规格不同,禁止覆盖")


def _release_checksum(release: ResearchDatasetRelease) -> str:
    payload = release.as_dict()
    payload["release_checksum"] = ""
    # 向后兼容(issue #187):旧 manifest 没有 dataset_kind 字段;bars 发布
    # 保持不含该字段的 checksum,使既有冻结发布仍可通过完整性校验。
    if release.dataset_kind is ReleaseDatasetKind.BARS:
        payload.pop("dataset_kind", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_manifest(staging: Path, release: ResearchDatasetRelease) -> None:
    path = staging / RELEASE_MANIFEST_FILENAME
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            release.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _cache_path(
    root: Path,
    code: str,
    period: BarPeriod,
    adjustment: str,
) -> Path:
    if not _SAFE_SYMBOL.fullmatch(code):
        raise DatasetReleaseError(f"非法标的代码: {code}")
    if adjustment not in _VALID_ADJUSTMENTS:
        raise DatasetReleaseError(f"非法复权方式: {adjustment}")
    path = (root / f"{code}_{period.value}_{adjustment}.parquet").resolve()
    if not path.is_relative_to(root.resolve()):
        raise DatasetReleaseError("缓存路径越界")
    return path


def _safe_release_artifact(root: Path, relative_path: str) -> Path:
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ReleaseIntegrityError(f"发布文件不存在或路径越界: {relative_path}")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "DAILY_METRICS_FIELDS",
    "FINANCIAL_INDICATORS_FIELDS",
    "RELEASE_FIELDS",
    "RELEASE_KINDS",
    "RELEASE_MANIFEST_FILENAME",
    "RELEASE_SCHEMA_VERSION",
    "RESEARCH_ETF_CATALOG",
    "AssetCapability",
    "CapabilityStatus",
    "DatasetQualityStatus",
    "DatasetReleaseError",
    "DatasetReleaseQualityError",
    "DatasetReleaseSpec",
    "ExecutionMetadata",
    "FrozenDatasetReleaseBuilder",
    "FrozenReleaseProvider",
    "ImmutableReleaseError",
    "PointInTimeBar",
    "PointInTimePrice",
    "ReleaseCapabilityError",
    "ReleaseDatasetKind",
    "ReleaseInstrumentSpec",
    "ReleaseIntegrityError",
    "ReleaseLifecycleEvent",
    "ReleasedInstrument",
    "ResearchDataReleaseSource",
    "ResearchDatasetRelease",
    "ResearchEtfCatalogEntry",
    "default_execution_metadata",
    "load_dataset_release",
    "research_etf_catalog_entry",
    "verify_dataset_release",
]
