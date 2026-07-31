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
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from finboard_data.cache import ParquetCache
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
    currency: str = "CNY"
    etf_category: EtfCategory | None = None
    list_date: date | None = None
    delist_date: date | None = None
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
    minimum_symbol_coverage: Decimal = Decimal("0.80")
    minimum_release_coverage: Decimal = Decimal("0.85")

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
        unknown_fields = set(self.fields) - set(RELEASE_FIELDS)
        if unknown_fields:
            raise ValueError(f"fields 包含未冻结字段: {sorted(unknown_fields)}")
        if not (Decimal("0") < self.minimum_symbol_coverage <= Decimal("1")):
            raise ValueError("minimum_symbol_coverage 必须落在 (0, 1]")
        if not (Decimal("0") < self.minimum_release_coverage <= Decimal("1")):
            raise ValueError("minimum_release_coverage 必须落在 (0, 1]")


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
    exchange: str | None = None
    currency: str = "CNY"
    etf_category: EtfCategory | None = None
    list_date: date | None = None
    delist_date: date | None = None
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
            "exchange": self.exchange,
            "currency": self.currency,
            "etf_category": self.etf_category.value if self.etf_category else None,
            "list_date": self.list_date.isoformat() if self.list_date else None,
            "delist_date": self.delist_date.isoformat() if self.delist_date else None,
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
            exchange=str(raw["exchange"]) if raw.get("exchange") is not None else None,
            currency=str(raw.get("currency", "CNY")),
            etf_category=EtfCategory(str(etf_raw)) if etf_raw is not None else None,
            list_date=date.fromisoformat(str(list_raw)) if list_raw is not None else None,
            delist_date=(date.fromisoformat(str(delist_raw)) if delist_raw is not None else None),
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
class PointInTimeBar:
    """带机器可判定 ``available_at`` 的冻结 Bar 视图。"""

    bar: Bar
    available_at: datetime


class FrozenDatasetReleaseBuilder:
    """把可变 Parquet 缓存原子冻结为不可变研究发布。"""

    def __init__(
        self,
        *,
        cache_dir: str | Path,
        release_root: str | Path,
    ) -> None:
        self._cache_dir = Path(cache_dir).resolve()
        self._release_root = Path(release_root).resolve()
        self._release_root.mkdir(parents=True, exist_ok=True)

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
            target_cache = ParquetCache(staging / "bars")
            released: list[ReleasedInstrument] = []
            for instrument in sorted(instruments, key=lambda item: item.code):
                released.append(
                    await self._freeze_instrument(
                        spec=spec,
                        instrument=instrument,
                        staging=staging,
                        target_cache=target_cache,
                    )
                )

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
                code_version=spec.code_version,
                published_at=published_at,
                instruments=tuple(released),
                capabilities=tuple(capabilities),
                quality_status=quality_status,
                quality_report={
                    "source": spec.source,
                    "period": spec.period.value,
                    "adjustment": spec.adjustment,
                    "fields": list(spec.fields),
                    "required_capabilities": list(spec.required_capabilities),
                    "minimum_symbol_coverage": str(spec.minimum_symbol_coverage),
                    "minimum_release_coverage": str(spec.minimum_release_coverage),
                    "release_coverage": str(release_coverage),
                    "coverage": _coverage_summary(released),
                    "warnings": warnings,
                },
                known_limitations=spec.known_limitations,
                storage_uri=spec.release_id,
            )
            checksum = _release_checksum(release)
            release = replace(release, release_checksum=checksum)
            await asyncio.to_thread(_write_manifest, staging, release)

            # 最终目录不存在时 rename 在同一文件系统内为原子操作。
            await asyncio.to_thread(staging.replace, final_dir)
            return replace(release, storage_uri=spec.release_id)
        except Exception:
            await asyncio.to_thread(shutil.rmtree, staging, True)
            raise

    async def _freeze_instrument(
        self,
        *,
        spec: DatasetReleaseSpec,
        instrument: ReleaseInstrumentSpec,
        staging: Path,
        target_cache: ParquetCache,
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
        source_cache = ParquetCache(self._cache_dir)
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
        after = source_path.stat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise DatasetReleaseQualityError(f"{instrument.code}:source_changed_during_release")
        frozen_bars = [
            bar for bar in bars if spec.start_date <= bar.timestamp.date() <= spec.end_date
        ]
        if not frozen_bars:
            raise DatasetReleaseQualityError(f"{instrument.code}:no_bars_in_release_range")
        audit = _audit_bars(
            frozen_bars,
            instrument=instrument,
            spec=spec,
        )
        ready = (
            audit.anomaly_count == 0
            and audit.coverage_pct >= spec.minimum_symbol_coverage
            and instrument.metadata_complete
            and set(instrument.required_event_types).issubset(instrument.present_event_types)
        )
        issues = list(audit.issues)
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
            exchange=instrument.exchange,
            currency=instrument.currency,
            etf_category=instrument.etf_category,
            list_date=instrument.list_date,
            delist_date=instrument.delist_date,
            status=instrument.status,
            metadata_complete=instrument.metadata_complete,
            lifecycle_events=instrument.lifecycle_events,
            present_event_types=instrument.present_event_types,
            required_event_types=instrument.required_event_types,
            name_history=instrument.name_history,
        )


class FrozenReleaseProvider:
    """只读取一个指定冻结发布的历史数据 Provider。"""

    def __init__(
        self,
        *,
        release_root: str | Path,
        release_id: str,
        verify_files: bool = True,
    ) -> None:
        self._release_dir = Path(release_root).resolve() / release_id
        self._release = load_dataset_release(self._release_dir)
        if self._release.release_id != release_id:
            raise ReleaseIntegrityError("目录 release_id 与 manifest 不一致")
        if not self._release.is_usable:
            raise DatasetReleaseQualityError(
                f"发布 {release_id} quality={self._release.quality_status.value}"
            )
        self._verified: set[str] = set()
        self._verify_files = verify_files

    @property
    def release(self) -> ResearchDatasetRelease:
        return self._release

    async def fetch_bars(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[Bar]:
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
        artifact = _safe_release_artifact(self._release_dir, item.artifact_path)
        if self._verify_files and item.code not in self._verified:
            actual = await asyncio.to_thread(_sha256_file, artifact)
            if actual != item.artifact_checksum:
                raise ReleaseIntegrityError(
                    f"{item.code} 文件校验和不一致: expected="
                    f"{item.artifact_checksum} actual={actual}"
                )
            self._verified.add(item.code)
        cache = ParquetCache(artifact.parent)
        bars = await cache.read(symbol, period, adjust)
        return ParquetCache.filter_by_date(bars, start, end)

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


def load_dataset_release(release_dir: str | Path) -> ResearchDatasetRelease:
    """从磁盘加载并验证 manifest 自身校验和。"""

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
) -> _BarAudit:
    dates = [bar.timestamp.date() for bar in bars]
    unique_dates = set(dates)
    anomaly_count = 0
    suspended_bar_dates: set[date] = set()
    previous_timestamp: datetime | None = None
    for bar in bars:
        bar_date = bar.timestamp.date()
        if previous_timestamp is not None and bar.timestamp <= previous_timestamp:
            anomaly_count += 1
        previous_timestamp = bar.timestamp
        if instrument.list_date is not None and bar_date < instrument.list_date:
            anomaly_count += 1
        if instrument.delist_date is not None and bar_date > instrument.delist_date:
            anomaly_count += 1
        if (
            bar.open.is_nan() or bar.open <= 0
            or bar.high.is_nan() or bar.high <= 0
            or bar.low.is_nan() or bar.low <= 0
            or bar.close.is_nan() or bar.close <= 0
            or bar.high < max(bar.open, bar.low, bar.close)
            or bar.low > min(bar.open, bar.high, bar.close)
            or bar.volume < 0
            or bar.amount < 0
        ):
            anomaly_count += 1
        if bar.volume == 0 and bar.open == bar.high == bar.low == bar.close:
            suspended_bar_dates.add(bar_date)
    anomaly_count += len(dates) - len(unique_dates)

    expected_start = max(
        spec.start_date,
        instrument.list_date or spec.start_date,
    )
    expected_end = min(
        spec.end_date,
        instrument.delist_date or spec.end_date,
    )
    lifecycle_dates = _known_suspension_dates(
        instrument,
        start=expected_start,
        end=expected_end,
    )
    expected_dates = _weekdays(expected_start, expected_end)
    required_dates = expected_dates - lifecycle_dates
    expected_sessions = len(required_dates)
    unexpected_sessions = unique_dates - expected_dates
    anomaly_count += len(unexpected_sessions)
    missing_sessions = len(required_dates - unique_dates)
    covered_sessions = len(required_dates & unique_dates)
    coverage = (
        Decimal(covered_sessions) / Decimal(expected_sessions)
        if expected_sessions > 0
        else Decimal("1")
    )
    suspended_dates = lifecycle_dates | (suspended_bar_dates & expected_dates)
    issues: list[str] = []
    if anomaly_count:
        issues.append(f"anomalies:{anomaly_count}")
    # 中国节假日不在工作日粗略日历中,小量缺口仅记警告;连续大缺口由覆盖率拦截。
    if missing_sessions:
        issues.append(f"missing_weekdays:{missing_sessions}")
    if suspended_dates:
        issues.append(f"suspended_sessions:{len(suspended_dates)}")

    actual_start = min(unique_dates)
    actual_end = max(unique_dates)
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
        if event.event_type == "suspension":
            suspended_from = event.effective_date
        elif event.event_type == "resumption" and suspended_from is not None:
            result.update(
                _weekdays(
                    max(start, suspended_from),
                    min(end, event.effective_date - timedelta(days=1)),
                )
            )
            suspended_from = None
    if suspended_from is not None and instrument.status in (
        ListingStatus.SUSPENDED,
        ListingStatus.DELISTED,
    ):
        result.update(_weekdays(max(start, suspended_from), end))
    return result


def _bar_available_at(bar: Bar, instrument: ReleasedInstrument) -> datetime:
    if bar.period is not BarPeriod.D1:
        if bar.timestamp.tzinfo is None:
            raise ReleaseIntegrityError("冻结 Bar timestamp 必须带时区")
        return bar.timestamp
    if instrument.market in (Market.A_SHARE, Market.FUTURE):
        local_time = time(15, 30)
        timezone = ZoneInfo("Asia/Shanghai")
    elif instrument.market is Market.HK:
        local_time = time(16, 30)
        timezone = ZoneInfo("Asia/Hong_Kong")
    elif instrument.market is Market.US:
        local_time = time(16, 30)
        timezone = ZoneInfo("America/New_York")
    else:  # pragma: no cover - Market 是封闭枚举
        raise ReleaseCapabilityError(f"没有 {instrument.market.value} 的日线 available_at 规则")
    return datetime.combine(bar.timestamp.date(), local_time, tzinfo=timezone).astimezone(UTC)


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
    for item in instruments:
        categories[item.category] = categories.get(item.category, 0) + 1
        asset_classes[item.asset_class.value] = asset_classes.get(item.asset_class.value, 0) + 1
        markets[item.market.value] = markets.get(item.market.value, 0) + 1
    return {
        "categories": categories,
        "asset_classes": asset_classes,
        "markets": markets,
        "missing_sessions": sum(item.missing_sessions for item in instruments),
        "suspended_sessions": sum(item.suspended_sessions for item in instruments),
        "anomaly_count": sum(item.anomaly_count for item in instruments),
        "name_history_records": sum(len(item.name_history) for item in instruments),
        "lifecycle_event_records": sum(
            len(item.lifecycle_events) for item in instruments
        ),
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
        release.availability_rules,
        release.code_version,
        release.known_limitations,
        tuple(
            cast(list[str], release.quality_report.get("required_capabilities", []))
        ),
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
    "RELEASE_FIELDS",
    "RELEASE_MANIFEST_FILENAME",
    "RELEASE_SCHEMA_VERSION",
    "RESEARCH_ETF_CATALOG",
    "AssetCapability",
    "CapabilityStatus",
    "DatasetReleaseError",
    "DatasetReleaseQualityError",
    "DatasetReleaseSpec",
    "ExecutionMetadata",
    "FrozenDatasetReleaseBuilder",
    "FrozenReleaseProvider",
    "ImmutableReleaseError",
    "PointInTimeBar",
    "ReleaseCapabilityError",
    "ReleaseInstrumentSpec",
    "ReleaseIntegrityError",
    "ReleaseLifecycleEvent",
    "ReleasedInstrument",
    "ResearchDatasetRelease",
    "ResearchEtfCatalogEntry",
    "default_execution_metadata",
    "load_dataset_release",
    "research_etf_catalog_entry",
    "verify_dataset_release",
]
