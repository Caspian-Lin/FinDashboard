"""不可变多资产研究数据发布单元/故障注入测试(issue #77)。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from finboard_data import (
    RELEASE_MANIFEST_FILENAME,
    DatasetReleaseQualityError,
    DatasetReleaseSpec,
    ExecutionMetadata,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
    ImmutableReleaseError,
    ReleaseCapabilityError,
    ReleasedInstrument,
    ReleaseInstrumentSpec,
    ReleaseIntegrityError,
    ReleaseLifecycleEvent,
    default_execution_metadata,
    load_dataset_release,
    verify_dataset_release,
)
from finboard_data.cache import ParquetCache
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import (
    AssetClass,
    BarPeriod,
    EtfCategory,
    InstrumentType,
    ListingBoard,
    ListingStatus,
    Market,
)

_START = date(2024, 1, 2)
_END = date(2024, 1, 5)

# 固定真实回归样本:从项目本地 yfinance qfq 缓存提取的 2024-01-02~05
# close/volume。amount 在该来源中不可用(为 0),作为发布已知限制保留。
_REAL_CLOSE_VOLUME = {
    "600519.SH": (
        ("1531.145630", "3215644"),
        ("1539.314697", "2022929"),
        ("1516.597534", "2155107"),
        ("1511.472534", "2024286"),
    ),
    "510300.SH": (
        ("3.222358", "942930578"),
        ("3.213026", "1061750273"),
        ("3.185030", "1666152481"),
        ("3.169165", "1699669666"),
    ),
    "513100.SH": (
        ("1.210000", "330084796"),
        ("1.191000", "682674776"),
        ("1.185000", "430004153"),
        ("1.176000", "429344300"),
    ),
    "518880.SH": (
        ("4.666000", "206278439"),
        ("4.663000", "111196216"),
        ("4.651000", "114966300"),
        ("4.664000", "126749700"),
    ),
    "511010.SH": (
        ("129.988083", "3166400"),
        ("129.953964", "4173900"),
        ("130.054459", "400500"),
        ("130.188080", "330700"),
    ),
}


def _bars(code: str, market: Market = Market.A_SHARE) -> list[Bar]:
    symbol = Symbol(code=code, market=market)
    samples = _REAL_CLOSE_VOLUME.get(
        code,
        (("10.5", "1000"),) * 4,
    )
    return [
        Bar(
            symbol=symbol,
            period=BarPeriod.D1,
            timestamp=datetime.combine(
                _START + timedelta(days=offset),
                datetime.min.time(),
                tzinfo=UTC,
            ),
            open=Decimal(close),
            high=Decimal(close) * Decimal("1.01"),
            low=Decimal(close) * Decimal("0.99"),
            close=Decimal(close),
            volume=Decimal(volume),
            amount=Decimal("0"),
            source="fixed_sample",
        )
        for offset, (close, volume) in enumerate(samples)
    ]


def _stock(code: str = "600519.SH") -> ReleaseInstrumentSpec:
    lifecycle_events = (
        ReleaseLifecycleEvent(
            event_type="dividend",
            effective_date=date(2023, 12, 29),
            available_at=datetime(2024, 1, 1, 8, tzinfo=UTC),
            source="fixed_sample",
            dataset_version="events-v1",
            details={"cash_per_share": "1"},
        ),
    )
    return ReleaseInstrumentSpec(
        code=code,
        name="固定股票样本",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        listing_board=ListingBoard.SSE_MAIN,
        available_at=datetime(2001, 8, 27, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        exchange="SSE",
        list_date=date(2001, 8, 27),
        status=ListingStatus.ACTIVE,
        lifecycle_events=lifecycle_events,
        present_event_types=("dividend",),
        name_history=(("固定股票样本", date(2001, 8, 27), None),),
    )


def _etf(
    code: str,
    category: EtfCategory,
    asset_class: AssetClass,
) -> ReleaseInstrumentSpec:
    return ReleaseInstrumentSpec(
        code=code,
        name=f"{category.value} ETF",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.ETF,
        asset_class=asset_class,
        available_at=datetime(2013, 1, 1, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.ETF,
            etf_category=category,
        ),
        exchange="SSE",
        etf_category=category,
        list_date=date(2013, 1, 1),
        status=ListingStatus.ACTIVE,
    )


def _multi_asset_instruments() -> list[ReleaseInstrumentSpec]:
    return [
        _stock(),
        _etf("510300.SH", EtfCategory.INDEX, AssetClass.EQUITY),
        _etf("513100.SH", EtfCategory.CROSS_BORDER, AssetClass.EQUITY),
        _etf("518880.SH", EtfCategory.COMMODITY, AssetClass.COMMODITY),
        _etf("511010.SH", EtfCategory.BOND, AssetClass.FIXED_INCOME),
    ]


def _spec(
    release_id: str = "fixed-r1",
    *,
    version: str = "2024.01",
    fields: tuple[str, ...] | None = None,
    required_capabilities: tuple[str, ...] | None = None,
) -> DatasetReleaseSpec:
    if fields is not None and required_capabilities is not None:
        return DatasetReleaseSpec(
            release_id=release_id,
            dataset_name="multi_asset_daily_bars",
            source="fixed_sample",
            version=version,
            start_date=_START,
            end_date=_END,
            code_version="deadbeef",
            fields=fields,
            required_capabilities=required_capabilities,
        )
    if fields is not None:
        return DatasetReleaseSpec(
            release_id=release_id,
            dataset_name="multi_asset_daily_bars",
            source="fixed_sample",
            version=version,
            start_date=_START,
            end_date=_END,
            code_version="deadbeef",
            fields=fields,
        )
    if required_capabilities is not None:
        return DatasetReleaseSpec(
            release_id=release_id,
            dataset_name="multi_asset_daily_bars",
            source="fixed_sample",
            version=version,
            start_date=_START,
            end_date=_END,
            code_version="deadbeef",
            required_capabilities=required_capabilities,
        )
    return DatasetReleaseSpec(
        release_id=release_id,
        dataset_name="multi_asset_daily_bars",
        source="fixed_sample",
        version=version,
        start_date=_START,
        end_date=_END,
        code_version="deadbeef",
    )


async def _seed(cache_dir: Path, instruments: list[ReleaseInstrumentSpec]) -> None:
    cache = ParquetCache(cache_dir)
    for instrument in instruments:
        await cache.write(
            Symbol(code=instrument.code, market=instrument.market),
            BarPeriod.D1,
            "qfq",
            _bars(instrument.code, instrument.market),
        )


@pytest.mark.asyncio
async def test_publish_multi_asset_release_and_read_only_provider(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    instruments = _multi_asset_instruments()
    await _seed(cache_dir, instruments)

    release = await FrozenDatasetReleaseBuilder(
        cache_dir=cache_dir,
        release_root=release_root,
    ).publish(_spec(), instruments)

    assert release.symbol_count == 5
    assert release.row_count == 20
    assert release.coverage_pct == Decimal("1")
    assert release.quality_report["adjustment"] == "qfq"
    assert release.quality_report["coverage"] == {
        "categories": {"full": 5},
        "asset_classes": {
            "fixed_income": 1,
            "equity": 3,
            "commodity": 1,
        },
        "markets": {"a_share": 5},
        "listing_boards": {"sse_main": 1, "unknown": 4},
        "missing_sessions": 0,
        "suspended_sessions": 0,
        "anomaly_count": 0,
        # issue #386:重复与 lifecycle 计数独立聚合(可见不阻断)。
        "duplicate_count": 0,
        "pre_list_bars": 0,
        "post_delist_bars": 0,
        "name_history_records": 1,
        "lifecycle_event_records": 1,
    }
    assert release.instrument("513100.SH").execution.settlement_days == 0
    assert release.instrument("511010.SH").execution.lot_size == Decimal("10")
    assert release.instrument("600519.SH").execution.stamp_tax_rate == Decimal("0.0005")
    assert release.instrument("600519.SH").listing_board == "sse_main"
    assert release.instrument("600519.SH").available_at == datetime(2001, 8, 27, tzinfo=UTC)
    assert release.instrument("600519.SH").lifecycle_events[0].available_at == datetime(
        2024, 1, 1, 8, tzinfo=UTC
    )
    for capability in (
        "stock",
        "etf:index",
        "etf:cross_border",
        "etf:commodity",
        "etf:bond",
    ):
        assert release.require_capability(capability).ready
    assert not next(item for item in release.capabilities if item.key == "futures").ready
    assert verify_dataset_release(release_root / "fixed-r1") == release

    provider = FrozenReleaseProvider(
        release_root=release_root,
        release_id="fixed-r1",
    )
    bars = await provider.fetch_bars(
        Symbol("518880.SH", Market.A_SHARE),
        BarPeriod.D1,
        _START,
        _END,
    )
    assert len(bars) == 4
    point_in_time = await provider.fetch_point_in_time_bars(
        Symbol("518880.SH", Market.A_SHARE),
        BarPeriod.D1,
        _START,
        _END,
        decision_at=datetime(2024, 1, 4, 7, 29, tzinfo=UTC),
    )
    assert [item.bar.timestamp.date() for item in point_in_time] == [
        date(2024, 1, 2),
        date(2024, 1, 3),
    ]
    assert point_in_time[-1].available_at == datetime(2024, 1, 3, 7, 30, tzinfo=UTC)
    with pytest.raises(ReleaseCapabilityError, match="禁止回退"):
        await provider.fetch_bars(
            Symbol("000001.SH", Market.A_SHARE),
            BarPeriod.D1,
            _START,
            _END,
        )
    with pytest.raises(ReleaseCapabilityError, match="超出发布范围"):
        await provider.fetch_bars(
            Symbol("518880.SH", Market.A_SHARE),
            BarPeriod.D1,
            _START - timedelta(days=1),
            _END,
        )


@pytest.mark.asyncio
async def test_mixed_release_records_actual_source_lineage(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    instruments = _multi_asset_instruments()
    await _seed(cache_dir, instruments)
    cache = ParquetCache(cache_dir)
    etf = instruments[1]
    await cache.write(
        Symbol(code=etf.code, market=etf.market),
        BarPeriod.D1,
        "qfq",
        [replace(bar, source="alternate") for bar in _bars(etf.code)],
    )

    release = await FrozenDatasetReleaseBuilder(
        cache_dir=cache_dir,
        release_root=tmp_path / "releases",
    ).publish(
        replace(_spec("mixed-r1"), source="mixed"),
        instruments,
    )

    assert release.source == "mixed"
    assert release.quality_report["source_policy"] == "mixed"
    assert release.quality_report["sources"] == ["alternate", "fixed_sample"]
    assert release.quality_report["source_symbol_counts"] == {
        "alternate": 1,
        "fixed_sample": 4,
    }
    assert release.instrument(etf.code).sources == ("alternate",)


@pytest.mark.asyncio
async def test_mixed_release_requires_at_least_two_actual_sources(tmp_path: Path) -> None:
    instruments = _multi_asset_instruments()
    await _seed(tmp_path / "cache", instruments)

    with pytest.raises(
        DatasetReleaseQualityError,
        match="mixed_source_requires_multiple_sources",
    ):
        await FrozenDatasetReleaseBuilder(
            cache_dir=tmp_path / "cache",
            release_root=tmp_path / "releases",
        ).publish(
            replace(_spec("mixed-r2"), source="mixed"),
            instruments,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "list_date", "delist_date", "bar_slice", "category"),
    [
        ("600001.SH", date(2024, 1, 4), None, slice(2, None), "short_history"),
        ("600002.SH", date(2000, 1, 1), date(2024, 1, 3), slice(None, 2), "delisted"),
    ],
)
async def test_release_coverage_is_clipped_to_listing_lifecycle(
    tmp_path: Path,
    code: str,
    list_date: date,
    delist_date: date | None,
    bar_slice: slice,
    category: str,
) -> None:
    """发布周期可跨越上市/退市边界,边界外不计为缺失。"""
    instrument = replace(
        _stock(code),
        list_date=list_date,
        delist_date=delist_date,
        status=ListingStatus.DELISTED if delist_date else ListingStatus.ACTIVE,
    )
    cache = ParquetCache(tmp_path / "cache")
    await cache.write(
        Symbol(code=code, market=Market.A_SHARE),
        BarPeriod.D1,
        "qfq",
        _bars(code)[bar_slice],
    )
    spec = replace(
        _spec(f"lifecycle-{code.replace('.', '-')}"),
        required_capabilities=(),
    )

    release = await FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    ).publish(spec, [instrument])

    item = release.instrument(code)
    assert item.coverage_pct == Decimal("1")
    assert item.missing_sessions == 0
    assert item.category == category


@pytest.mark.asyncio
async def test_suspended_zero_volume_bar_is_preserved_as_warning(tmp_path: Path) -> None:
    """有停牌代理 Bar 时保留真实时间线,不把停牌当数据异常。"""
    instrument = _stock("600003.SH")
    bars = _bars(instrument.code)
    bars[1] = replace(
        bars[1],
        open=bars[1].close,
        high=bars[1].close,
        low=bars[1].close,
        volume=Decimal("0"),
    )
    cache = ParquetCache(tmp_path / "cache")
    await cache.write(
        Symbol(code=instrument.code, market=instrument.market),
        BarPeriod.D1,
        "qfq",
        bars,
    )
    spec = replace(_spec("suspended-r1"), required_capabilities=())

    release = await FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    ).publish(spec, [instrument])

    item = release.instrument(instrument.code)
    assert item.ready
    assert item.suspended_sessions == 1
    assert item.coverage_pct == Decimal("1")
    assert item.issues == ("suspended_sessions:1",)


@pytest.mark.asyncio
async def test_missing_bar_in_authoritative_suspension_window_is_not_a_gap(
    tmp_path: Path,
) -> None:
    """有停复牌事件时,数据源省略停牌 Bar 也不降低有效交易日覆盖率。"""
    code = "600004.SH"
    events = (
        ReleaseLifecycleEvent(
            event_type="suspension",
            effective_date=date(2024, 1, 3),
            available_at=datetime(2024, 1, 3, tzinfo=UTC),
            source="exchange",
            dataset_version="events-v1",
        ),
        ReleaseLifecycleEvent(
            event_type="resumption",
            effective_date=date(2024, 1, 4),
            available_at=datetime(2024, 1, 4, tzinfo=UTC),
            source="exchange",
            dataset_version="events-v1",
        ),
    )
    instrument = replace(
        _stock(code),
        lifecycle_events=events,
        present_event_types=("resumption", "suspension"),
    )
    bars = [bar for bar in _bars(code) if bar.timestamp.date() != date(2024, 1, 3)]
    cache = ParquetCache(tmp_path / "cache")
    await cache.write(
        Symbol(code=code, market=Market.A_SHARE),
        BarPeriod.D1,
        "qfq",
        bars,
    )
    spec = replace(_spec("suspension-events-r1"), required_capabilities=())

    release = await FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    ).publish(spec, [instrument])

    item = release.instrument(code)
    assert item.ready
    assert item.expected_sessions == 3
    assert item.missing_sessions == 0
    assert item.suspended_sessions == 1
    assert item.coverage_pct == Decimal("1")


@pytest.mark.asyncio
async def test_tushare_suspension_day_is_not_a_release_gap(tmp_path: Path) -> None:
    """suspend_d 的逐日停牌事件只豁免对应的整日停牌日期。"""
    code = "600004.SH"
    event = ReleaseLifecycleEvent(
        event_type="suspension_day",
        effective_date=date(2024, 1, 3),
        available_at=datetime(2024, 1, 6, tzinfo=UTC),
        source="tushare",
        dataset_version="suspend_d-v1",
    )
    instrument = replace(
        _stock(code),
        lifecycle_events=(event,),
        present_event_types=("suspension_day",),
    )
    bars = [bar for bar in _bars(code) if bar.timestamp.date() != date(2024, 1, 3)]
    cache = ParquetCache(tmp_path / "cache")
    await cache.write(
        Symbol(code=code, market=Market.A_SHARE),
        BarPeriod.D1,
        "qfq",
        bars,
    )

    release = await FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    ).publish(replace(_spec("suspend-day-r1"), required_capabilities=()), [instrument])

    item = release.instrument(code)
    assert item.ready
    assert item.expected_sessions == 3
    assert item.missing_sessions == 0
    assert item.suspended_sessions == 1
    assert item.coverage_pct == Decimal("1")


@pytest.mark.asyncio
async def test_checksum_corruption_is_fail_closed(tmp_path: Path) -> None:
    instruments = _multi_asset_instruments()
    await _seed(tmp_path / "cache", instruments)
    release = await FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    ).publish(_spec(), instruments)
    item = release.instrument("510300.SH")
    artifact = tmp_path / "releases" / release.release_id / item.artifact_path
    artifact.write_bytes(artifact.read_bytes() + b"corrupt")

    with pytest.raises(ReleaseIntegrityError, match="校验和不一致"):
        verify_dataset_release(tmp_path / "releases" / release.release_id)
    with pytest.raises(ReleaseIntegrityError, match="校验和不一致"):
        await FrozenDatasetReleaseBuilder(
            cache_dir=tmp_path / "cache",
            release_root=tmp_path / "releases",
        ).publish(_spec(), instruments)
    provider = FrozenReleaseProvider(
        release_root=tmp_path / "releases",
        release_id=release.release_id,
    )
    with pytest.raises(ReleaseIntegrityError, match="校验和不一致"):
        await provider.fetch_bars(
            Symbol("510300.SH", Market.A_SHARE),
            BarPeriod.D1,
            _START,
            _END,
        )


@pytest.mark.asyncio
async def test_expected_checksum_anchors_to_frozen_value_not_recompute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB 锚定校验不做代码版本敏感的重算(issue #202)。

    模拟「两端 ``as_dict`` 输出不同」(字段增删/git SHA 漂移/dirty 工作区):
    重算值漂移时,旧路径(未传 expected_checksum)误报 ReleaseIntegrityError,
    锚定路径以冻结的 release_checksum 正常加载。
    """
    instruments = _multi_asset_instruments()
    await _seed(tmp_path / "cache", instruments)
    release = await FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    ).publish(_spec(), instruments)

    from finboard_data import releases as releases_module

    monkeypatch.setattr(
        releases_module, "_release_checksum", lambda _release: "drifted"
    )

    release_dir = tmp_path / "releases" / release.release_id
    with pytest.raises(ReleaseIntegrityError, match="manifest checksum 不一致"):
        load_dataset_release(release_dir)
    anchored = load_dataset_release(
        release_dir,
        expected_checksum=release.release_checksum,
    )
    assert anchored.release_id == release.release_id
    provider = FrozenReleaseProvider(
        release_root=tmp_path / "releases",
        release_id=release.release_id,
        expected_checksum=release.release_checksum,
    )
    assert provider.release.release_id == release.release_id


@pytest.mark.asyncio
async def test_expected_checksum_mismatch_is_fail_closed(tmp_path: Path) -> None:
    """manifest 内嵌 release_checksum 被篡改时,DB 锚定校验仍拦截。"""
    instruments = _multi_asset_instruments()
    await _seed(tmp_path / "cache", instruments)
    release = await FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    ).publish(_spec(), instruments)
    manifest_path = (
        tmp_path / "releases" / release.release_id / RELEASE_MANIFEST_FILENAME
    )
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["release_checksum"] = "0" * 64
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ReleaseIntegrityError, match="DB 锚定不一致"):
        load_dataset_release(
            tmp_path / "releases" / release.release_id,
            expected_checksum=release.release_checksum,
        )


@pytest.mark.asyncio
async def test_expected_checksum_keeps_file_level_sha256(tmp_path: Path) -> None:
    """锚定路径不跳过逐文件 artifact_checksum sha256 校验。"""
    instruments = _multi_asset_instruments()
    await _seed(tmp_path / "cache", instruments)
    release = await FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    ).publish(_spec(), instruments)
    item = release.instrument("510300.SH")
    artifact = tmp_path / "releases" / release.release_id / item.artifact_path
    artifact.write_bytes(artifact.read_bytes() + b"corrupt")

    provider = FrozenReleaseProvider(
        release_root=tmp_path / "releases",
        release_id=release.release_id,
        expected_checksum=release.release_checksum,
    )
    with pytest.raises(ReleaseIntegrityError, match="校验和不一致"):
        await provider.fetch_bars(
            Symbol("510300.SH", Market.A_SHARE),
            BarPeriod.D1,
            _START,
            _END,
        )


@pytest.mark.asyncio
async def test_failed_release_preserves_previous_and_cleans_staging(tmp_path: Path) -> None:
    instruments = _multi_asset_instruments()
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    await _seed(cache_dir, instruments)
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=cache_dir,
        release_root=release_root,
    )
    previous = await builder.publish(_spec(), instruments)
    missing = _stock("000001.SZ")

    with pytest.raises(DatasetReleaseQualityError, match="source_artifact_missing"):
        await builder.publish(
            _spec("fixed-r2", version="2024.02"),
            [*instruments, missing],
            previous_release=previous,
        )

    assert verify_dataset_release(release_root / previous.release_id) == previous
    assert not (release_root / "fixed-r2").exists()
    assert not list(release_root.glob(".fixed-r2-*"))


@pytest.mark.asyncio
async def test_release_identity_and_schema_are_immutable(tmp_path: Path) -> None:
    instruments = _multi_asset_instruments()
    await _seed(tmp_path / "cache", instruments)
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    )
    previous = await builder.publish(_spec(), instruments)
    assert await builder.publish(_spec(), instruments) == previous

    with pytest.raises(ImmutableReleaseError, match="禁止覆盖"):
        await builder.publish(
            DatasetReleaseSpec(
                release_id="fixed-r1",
                dataset_name="multi_asset_daily_bars",
                source="fixed_sample",
                version="different",
                start_date=_START,
                end_date=_END,
                code_version="deadbeef",
            ),
            instruments,
        )
    with pytest.raises(DatasetReleaseQualityError, match="schema_version 未递增"):
        await builder.publish(
            _spec(
                "fixed-r2",
                version="2024.02",
                fields=("timestamp", "close"),
            ),
            instruments,
            previous_release=previous,
        )


@pytest.mark.asyncio
async def test_release_instrument_industry_flows_to_manifest(tmp_path: Path) -> None:
    """发布产物携带 industry,as_dict/from_dict 往返不丢(issue #185)。"""
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    instrument = replace(_stock(), name="贵州茅台", industry="白酒")
    await _seed(cache_dir, [instrument])

    release = await FrozenDatasetReleaseBuilder(
        cache_dir=cache_dir,
        release_root=release_root,
    ).publish(
        _spec(
            release_id="fixed-r185-industry",
            required_capabilities=("stock",),
        ),
        [instrument],
    )

    released = release.instrument("600519.SH")
    assert released.industry == "白酒"
    assert released.as_dict()["industry"] == "白酒"
    # manifest 序列化往返(DB jsonb / 磁盘 manifest.json 同路径)。
    assert ReleasedInstrument.from_dict(released.as_dict()) == released


@pytest.mark.asyncio
async def test_convertible_missing_metadata_events_cannot_publish(tmp_path: Path) -> None:
    convertible = ReleaseInstrumentSpec(
        code="110000.SH",
        name="不完整可转债样本",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.CONVERTIBLE,
        asset_class=AssetClass.CONVERTIBLE,
        available_at=datetime(2024, 1, 1, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.CONVERTIBLE,
        ),
        metadata_complete=False,
        required_event_types=(
            "forced_redemption",
            "sell_back",
            "downward_revision",
            "conversion_price_adjust",
        ),
    )
    await _seed(tmp_path / "cache", [convertible])
    spec = DatasetReleaseSpec(
        release_id="convertible-r1",
        dataset_name="convertible_bars",
        source="fixed_sample",
        version="2024.01",
        start_date=_START,
        end_date=_END,
        code_version="deadbeef",
        required_capabilities=("convertible",),
    )
    with pytest.raises(DatasetReleaseQualityError) as exc_info:
        await FrozenDatasetReleaseBuilder(
            cache_dir=tmp_path / "cache",
            release_root=tmp_path / "releases",
        ).publish(spec, [convertible])
    message = str(exc_info.value)
    assert "metadata_incomplete" in message
    assert "missing_events" in message
    assert not (tmp_path / "releases" / "convertible-r1").exists()


@pytest.mark.asyncio
async def test_futures_missing_contract_events_cannot_publish(tmp_path: Path) -> None:
    future = ReleaseInstrumentSpec(
        code="IF2406.CFFEX",
        name="沪深300期货固定样本",
        market=Market.FUTURE,
        instrument_type=InstrumentType.FUTURES,
        asset_class=AssetClass.DERIVATIVE,
        available_at=datetime(2024, 1, 1, tzinfo=UTC),
        execution=ExecutionMetadata(
            lot_size=Decimal("1"),
            price_tick=Decimal("0.2"),
            settlement_days=0,
            multiplier=Decimal("300"),
            margin_rate=Decimal("0.12"),
            commission_min=Decimal("0"),
            trading_calendar="CFFEX",
            allows_short=True,
        ),
        exchange="CFFEX",
        required_event_types=("roll", "expiration", "delivery"),
    )
    await _seed(tmp_path / "cache", [future])
    spec = DatasetReleaseSpec(
        release_id="futures-r1",
        dataset_name="futures_bars",
        source="fixed_sample",
        version="2024.01",
        start_date=_START,
        end_date=_END,
        code_version="deadbeef",
        required_capabilities=("futures",),
    )
    with pytest.raises(DatasetReleaseQualityError, match="missing_events"):
        await FrozenDatasetReleaseBuilder(
            cache_dir=tmp_path / "cache",
            release_root=tmp_path / "releases",
        ).publish(spec, [future])
    assert future.execution.multiplier == Decimal("300")
    assert future.execution.margin_rate == Decimal("0.12")
    assert not (tmp_path / "releases" / "futures-r1").exists()


@pytest.mark.asyncio
async def test_source_interruption_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instruments = _multi_asset_instruments()
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    await _seed(cache_dir, instruments)
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=cache_dir,
        release_root=release_root,
    )
    previous = await builder.publish(_spec(), instruments)

    async def interrupted_read(*args: object, **kwargs: object) -> list[Bar]:
        del args, kwargs
        raise ConnectionError("simulated rate limit/network interruption")

    monkeypatch.setattr(ParquetCache, "read", interrupted_read)
    with pytest.raises(DatasetReleaseQualityError, match="source_read_failed"):
        await builder.publish(
            _spec("fixed-r2", version="2024.02"),
            instruments,
            previous_release=previous,
        )
    assert (release_root / previous.release_id / "manifest.json").is_file()
    assert not (release_root / "fixed-r2").exists()
    assert not list(release_root.glob(".fixed-r2-*"))


@pytest.mark.asyncio
async def test_covered_ranges_marks_queried_no_bar_days_as_covered(
    tmp_path: Path,
) -> None:
    """批量拉取已成功查询过的合法无 Bar 日(停牌/上市前)不计入 missing。

    覆盖率口径必须与 :meth:`CacheMetadata.covers` / 批量拉取的
    ``_cache_fetch_ranges`` 一致(issue #99),否则会重复把数据源省略的
    停牌 Bar 误判为数据缺口。
    """
    code = "600005.SH"
    instrument = _stock(code)
    # 缺 2024-01-03 和 2024-01-04 两条 Bar,模拟数据源对停牌日不返回 Bar。
    bars = [
        bar
        for bar in _bars(code)
        if bar.timestamp.date() not in {date(2024, 1, 3), date(2024, 1, 4)}
    ]
    cache = ParquetCache(tmp_path / "cache")
    await cache.write(
        Symbol(code=code, market=Market.A_SHARE),
        BarPeriod.D1,
        "qfq",
        bars,
        covered_ranges=((date(2024, 1, 2), date(2024, 1, 5)),),
    )

    release = await FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    ).publish(replace(_spec("covered-ranges-r1"), required_capabilities=()), [instrument])

    item = release.instrument(code)
    assert item.ready
    # 4 个交易日全部视为已覆盖,即使只有 2 条 Bar。
    assert item.expected_sessions == 4
    assert item.missing_sessions == 0
    assert item.coverage_pct == Decimal("1")


@pytest.mark.asyncio
@pytest.mark.parametrize("max_concurrency", [1, 4])
async def test_parallel_publish_is_deterministic_and_equal_to_serial(
    tmp_path: Path,
    max_concurrency: int,
) -> None:
    """并行冻结的产物(逐标的 artifact checksum / row_count / instrument 顺序)
    必须与串行一致(可复现)。``published_at`` 时间戳不纳入比较。"""
    instruments = _multi_asset_instruments()
    cache_dir = tmp_path / "cache"
    await _seed(cache_dir, instruments)

    serial = await FrozenDatasetReleaseBuilder(
        cache_dir=cache_dir,
        release_root=tmp_path / "releases-serial",
        max_concurrency=1,
    ).publish(_spec("parallel-serial"), instruments)

    parallel = await FrozenDatasetReleaseBuilder(
        cache_dir=cache_dir,
        release_root=tmp_path / "releases-parallel",
        max_concurrency=max_concurrency,
    ).publish(_spec(f"parallel-n{max_concurrency}"), instruments)

    # 发布顺序按 code 排序,确保并行结果与串行一致。
    assert [item.code for item in parallel.instruments] == [
        item.code for item in serial.instruments
    ]
    assert parallel.row_count == serial.row_count
    assert parallel.symbol_count == serial.symbol_count
    assert parallel.coverage_pct == serial.coverage_pct
    for serial_item, parallel_item in zip(serial.instruments, parallel.instruments, strict=True):
        assert parallel_item.code == serial_item.code
        assert parallel_item.artifact_checksum == serial_item.artifact_checksum
        assert parallel_item.row_count == serial_item.row_count
        assert parallel_item.coverage_pct == serial_item.coverage_pct
        assert parallel_item.missing_sessions == serial_item.missing_sessions
        assert parallel_item.suspended_sessions == serial_item.suspended_sessions
        assert parallel_item.expected_sessions == serial_item.expected_sessions
