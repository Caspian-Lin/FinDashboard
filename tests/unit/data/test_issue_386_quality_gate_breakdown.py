"""发布质量门异常口径拆分(issue #386)。

事故:`a-share-full-20260908-v4` 中北交所 920 系因「bar 早于 list_date」
(代码切换带出的新三板/精选层合法历史)被全部拦截,且错误只有总数没有
类型构成;`data_quality_check`(BarQualityChecker 视角)与发布门计数无法
互相解释。锁定的新语义:

* ``anomaly_count`` 只含 OHLCV 异常(与 BarQualityChecker 同义),OHLCV
  异常与重复 bar 仍然阻断 ready;
* ``pre_list_bars`` / ``post_delist_bars`` 独立计数、进 issues 与
  quality_report,可见但不阻断 ready;
* 异常明细带 reasons 构成(``anomaly_reasons:``),错误可定位;
* 序列化零值省略键(干净标的 manifest 与旧版本逐字节一致)。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from finboard_data import (
    DatasetReleaseQualityError,
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    ReleaseInstrumentSpec,
    default_execution_metadata,
)
from finboard_data.cache import ParquetCache
from finboard_data.quality import BarQualityChecker
from finboard_data.releases import DatasetQualityStatus, _audit_bars
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import (
    AssetClass,
    BarPeriod,
    InstrumentType,
    ListingStatus,
    Market,
)

_START = date(2024, 1, 2)
_END = date(2024, 1, 5)


def _bars(
    code: str,
    *,
    market: Market = Market.A_SHARE,
    start: date = _START,
    count: int = 4,
    source: str = "fixed_sample",
) -> list[Bar]:
    """OHLCV 干净的连续日线样本(同日不重复)。"""
    symbol = Symbol(code=code, market=market)
    return [
        Bar(
            symbol=symbol,
            period=BarPeriod.D1,
            timestamp=datetime.combine(
                start + timedelta(days=offset),
                datetime.min.time(),
                tzinfo=UTC,
            ),
            open=Decimal("10"),
            high=Decimal("10.2"),
            low=Decimal("9.8"),
            close=Decimal("10.1"),
            volume=Decimal("1000"),
            amount=Decimal("10100"),
            source=source,
        )
        for offset in range(count)
    ]


def _stock(
    code: str = "920023.BJ",
    *,
    list_date: date | None = None,
    delist_date: date | None = None,
    status: ListingStatus = ListingStatus.ACTIVE,
) -> ReleaseInstrumentSpec:
    """模拟北交所代码切换形态:list_date 晚于 bar 历史起点。"""
    return ReleaseInstrumentSpec(
        code=code,
        name="代码切换样本",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2015, 1, 1, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        exchange="BSE",
        list_date=list_date,
        delist_date=delist_date,
        status=status,
    )


def _spec(release_id: str = "issue386-r1") -> DatasetReleaseSpec:
    return DatasetReleaseSpec(
        release_id=release_id,
        dataset_name="multi_asset_daily_bars",
        source="fixed_sample",
        version="r1",
        start_date=_START,
        end_date=_END,
        code_version="deadbeef",
        # 默认值还要求四类 ETF 能力;本文件只发股票单标的。
        required_capabilities=(InstrumentType.STOCK.value,),
    )


# --------------------------------------------------------------------------- #
# _audit_bars:计数拆分
# --------------------------------------------------------------------------- #


def test_pre_list_bars_counted_separately_not_anomalies() -> None:
    """list_date 之前的合法历史:pre_list_bars 独立计数,anomaly_count 归零。"""
    instrument = _stock(list_date=date(2024, 1, 4))
    bars = _bars("920023.BJ")  # 01-02..01-05,前两根早于 list_date

    audit = _audit_bars(bars, instrument=instrument, spec=_spec())

    assert audit.pre_list_bars == 2
    assert audit.anomaly_count == 0
    # 与 BarQualityChecker(data_quality_check 同源)口径一致。
    assert audit.anomaly_count == BarQualityChecker().check(bars).anomaly_count
    assert "pre_list_bars:2" in audit.issues
    assert not any(issue.startswith("anomalies:") for issue in audit.issues)


def test_post_delist_bars_counted_separately_not_anomalies() -> None:
    instrument = _stock(
        list_date=date(2020, 1, 1),
        delist_date=date(2024, 1, 3),
        status=ListingStatus.DELISTED,
    )
    bars = _bars("600001.SH")  # delist(01-03)之后仍有两根(01-04/01-05)

    audit = _audit_bars(bars, instrument=instrument, spec=_spec())

    assert audit.post_delist_bars == 2
    assert audit.anomaly_count == 0
    assert "post_delist_bars:2" in audit.issues


# --------------------------------------------------------------------------- #
# 发布门:pre/post 可见不阻断,OHLCV/重复仍然阻断
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_publish_passes_with_pre_list_history_as_warnings(tmp_path: Path) -> None:
    """920 系事故验收:合法 pre-list 历史不再拦截,以 WARNINGS 可见。"""
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    instrument = _stock("920023.BJ", list_date=date(2024, 1, 4))
    await ParquetCache(cache_dir).write(
        Symbol(code="920023.BJ", market=Market.A_SHARE),
        BarPeriod.D1,
        "qfq",
        _bars("920023.BJ"),
    )

    release = await FrozenDatasetReleaseBuilder(
        cache_dir=cache_dir,
        release_root=release_root,
    ).publish(_spec(), [instrument])

    item = release.instrument("920023.BJ")
    assert item.ready
    assert item.anomaly_count == 0
    assert item.pre_list_bars == 2
    assert "pre_list_bars:2" in item.issues
    assert release.quality_status is DatasetQualityStatus.WARNINGS
    coverage_summary = cast(dict[str, object], release.quality_report["coverage"])
    assert coverage_summary["pre_list_bars"] == 2
    # 零值省略键、非零落键(干净标的的 manifest 不携带新键)。
    assert item.as_dict()["pre_list_bars"] == 2


@pytest.mark.asyncio
async def test_publish_rejects_ohlcv_anomaly_with_reason_breakdown(
    tmp_path: Path,
) -> None:
    """OHLCV 异常仍然阻断,且错误带 reasons 构成。"""
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    instrument = _stock("600519.SH", list_date=date(2020, 1, 1))
    bars = _bars("600519.SH")
    bars[1] = replace(bars[1], volume=Decimal("-5"))  # neg_volume
    await ParquetCache(cache_dir).write(
        Symbol(code="600519.SH", market=Market.A_SHARE),
        BarPeriod.D1,
        "qfq",
        bars,
    )

    with pytest.raises(DatasetReleaseQualityError) as exc_info:
        await FrozenDatasetReleaseBuilder(
            cache_dir=cache_dir,
            release_root=release_root,
        ).publish(_spec(), [instrument])

    message = str(exc_info.value)
    assert "600519.SH" in message
    assert "anomalies:1" in message
    assert "anomaly_reasons:neg_volume=1" in message


@pytest.mark.asyncio
async def test_publish_rejects_duplicate_bars_with_named_count(
    tmp_path: Path,
) -> None:
    """重复日期 bar 仍然阻断(与 BarQualityChecker.passed 语义一致)。"""
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    instrument = _stock("600519.SH", list_date=date(2020, 1, 1))
    bars = _bars("600519.SH")
    bars.append(bars[0])  # 同日重复
    await ParquetCache(cache_dir).write(
        Symbol(code="600519.SH", market=Market.A_SHARE),
        BarPeriod.D1,
        "qfq",
        bars,
    )

    with pytest.raises(DatasetReleaseQualityError) as exc_info:
        await FrozenDatasetReleaseBuilder(
            cache_dir=cache_dir,
            release_root=release_root,
        ).publish(_spec(), [instrument])

    message = str(exc_info.value)
    assert "duplicate_bars:1" in message


def test_manifest_roundtrip_preserves_split_counts() -> None:
    from finboard_data import ReleasedInstrument
    from finboard_shared.types import ListingBoard

    base = ReleasedInstrument(
        code="920023.BJ",
        name="代码切换样本",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2015, 1, 1, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        artifact_path="bars/920023.BJ_1d_qfq.parquet",
        artifact_checksum="0" * 64,
        artifact_size=1,
        row_count=4,
        start_date=_START,
        end_date=_END,
        expected_sessions=4,
        missing_sessions=0,
        suspended_sessions=0,
        anomaly_count=0,
        coverage_pct=Decimal("1"),
        category="short_history",
        ready=True,
        issues=("pre_list_bars:2",),
        listing_board=ListingBoard.BSE.value,
        duplicate_count=0,
        pre_list_bars=2,
        post_delist_bars=0,
    )
    as_dict = base.as_dict()
    # 零值省略、非零落键。
    assert "duplicate_count" not in as_dict
    assert "post_delist_bars" not in as_dict
    assert as_dict["pre_list_bars"] == 2
    restored = ReleasedInstrument.from_dict(as_dict)
    assert restored.pre_list_bars == 2
    assert restored.duplicate_count == 0
    assert restored.post_delist_bars == 0
    # 旧 manifest(缺键)回退 0。
    legacy = dict(as_dict)
    legacy.pop("pre_list_bars")
    restored_legacy = ReleasedInstrument.from_dict(legacy)
    assert restored_legacy.pre_list_bars == 0
