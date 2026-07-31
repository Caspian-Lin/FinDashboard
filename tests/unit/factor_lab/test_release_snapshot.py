"""从 #77 冻结发布构建多资产价格特征的固定样本测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from finboard_backtest.factor_lab import build_price_feature_snapshot
from finboard_data.cache import ParquetCache
from finboard_data.releases import (
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
    ReleaseInstrumentSpec,
    default_execution_metadata,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import (
    AssetClass,
    BarPeriod,
    EtfCategory,
    InstrumentType,
    ListingStatus,
    Market,
)


def _weekdays(start: date, count: int) -> list[date]:
    result: list[date] = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _instrument(
    code: str,
    category: EtfCategory,
    asset_class: AssetClass,
) -> ReleaseInstrumentSpec:
    return ReleaseInstrumentSpec(
        code=code,
        name=f"fixed {category.value}",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.ETF,
        asset_class=asset_class,
        available_at=datetime(2013, 1, 1, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.ETF,
            etf_category=category,
        ),
        etf_category=category,
        list_date=date(2013, 1, 1),
        status=ListingStatus.ACTIVE,
    )


@pytest.mark.asyncio
async def test_frozen_release_to_feature_snapshot_has_no_future_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _weekdays(date(2024, 1, 2), 35)
    date_set = set(dates)

    def _mock_trading_days(start: date, end: date) -> set[date]:
        return {d for d in date_set if start <= d <= end}

    monkeypatch.setattr(
        "finboard_data.releases._trading_days",
        _mock_trading_days,
    )
    instruments = [
        _instrument("510300.SH", EtfCategory.INDEX, AssetClass.EQUITY),
        _instrument(
            "518880.SH",
            EtfCategory.COMMODITY,
            AssetClass.COMMODITY,
        ),
    ]
    cache = ParquetCache(tmp_path / "cache")
    for instrument_index, instrument in enumerate(instruments):
        bars: list[Bar] = []
        for index, business_date in enumerate(dates):
            # 固定的趋势 + 周期扰动同时提供动量和下行波动样本。
            close = Decimal(
                str(
                    3.0
                    + instrument_index
                    + index * 0.01
                    + (0.03 if index % 5 else -0.04)
                )
            )
            bars.append(
                Bar(
                    symbol=Symbol(instrument.code, instrument.market),
                    period=BarPeriod.D1,
                    timestamp=datetime.combine(
                        business_date,
                        datetime.min.time(),
                        tzinfo=UTC,
                    ),
                    open=close,
                    high=close * Decimal("1.01"),
                    low=close * Decimal("0.99"),
                    close=close,
                    volume=Decimal("1000000"),
                    amount=close * Decimal("1000000"),
                )
            )
        await cache.write(
            Symbol(instrument.code, instrument.market),
            BarPeriod.D1,
            "qfq",
            bars,
        )
    spec = DatasetReleaseSpec(
        release_id="factor-lab-fixed-v1",
        dataset_name="factor_lab_fixed",
        source="fixed_sample",
        version="2024.01",
        start_date=dates[0],
        end_date=dates[-1],
        code_version="deadbeef",
        required_capabilities=("etf:index", "etf:commodity"),
    )
    release_root = tmp_path / "releases"
    release = await FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=release_root,
    ).publish(spec, instruments)
    decision_at = datetime.combine(
        dates[-1],
        datetime.min.time(),
        tzinfo=UTC,
    ) + timedelta(hours=8)
    snapshot = await build_price_feature_snapshot(
        provider=FrozenReleaseProvider(
            release_root=release_root,
            release_id=release.release_id,
        ),
        decision_at=decision_at,
        code_version="deadbeef",
        momentum_lookback=20,
        volatility_windows=(20,),
    )
    assert snapshot.dataset_release_id == release.release_id
    assert snapshot.dataset_release_checksum == release.release_checksum
    assert {item.feature_name for item in snapshot.observations} >= {
        "momentum",
        "volatility_20d",
    }
    assert {item.asset_class for item in snapshot.observations} == {
        "equity",
        "commodity",
    }
    assert all(
        item.available_at <= snapshot.decision_at
        for item in snapshot.observations
    )
