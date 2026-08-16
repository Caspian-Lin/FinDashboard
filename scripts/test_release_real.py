"""真实数据验证:用本地 tushare 缓存发布数据集,观察修复前后 missing_sessions 变化。

运行: uv run python scripts/test_release_real.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from finboard_data import (
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    ReleaseInstrumentSpec,
    default_execution_metadata,
)
from finboard_data.cache import CacheMetadata, ParquetCache
from finboard_data.releases import _audit_bars, _covered_trading_dates
from finboard_shared.models import Symbol
from finboard_shared.types import (
    AssetClass,
    BarPeriod,
    InstrumentType,
    ListingStatus,
    Market,
)

CACHE_DIR = Path("data_cache")
RELEASE_ROOT = Path("C:/Users/28491/AppData/Local/Temp/opencode/test_releases_real")
START = date(2024, 1, 1)
END = date(2024, 6, 28)


def _make_spec(code: str, metadata: CacheMetadata | None) -> ReleaseInstrumentSpec:
    list_date = metadata.first_date if metadata and metadata.first_date else date(2010, 1, 1)
    if list_date > START:
        list_date = START
    return ReleaseInstrumentSpec(
        code=code,
        name=code,
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2010, 1, 1, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        exchange="SSE",
        list_date=list_date,
        status=ListingStatus.ACTIVE,
    )


async def _audit_one(code: str) -> dict[str, object]:
    cache = ParquetCache(CACHE_DIR)
    symbol = Symbol(code=code, market=Market.A_SHARE)
    bars = await cache.read(symbol, BarPeriod.D1, "qfq")
    bars = [b for b in bars if START <= b.timestamp.date() <= END]
    metadata = await cache.metadata_for(symbol, BarPeriod.D1, "qfq")
    spec = DatasetReleaseSpec(
        release_id="test",
        dataset_name="test",
        source="tushare",
        version="v1",
        start_date=START,
        end_date=END,
        code_version="x",
        required_capabilities=(),
    )
    instrument = _make_spec(code, metadata)

    # 不传 metadata(旧行为)
    audit_old = _audit_bars(bars, instrument=instrument, spec=spec, metadata=None)
    # 传 metadata(新行为)
    audit_new = _audit_bars(bars, instrument=instrument, spec=spec, metadata=metadata)
    covered_queried = _covered_trading_dates(metadata, START, END)
    return {
        "code": code,
        "bars": len(bars),
        "covered_ranges": [
            [a.isoformat(), b.isoformat()] for a, b in (metadata.covered_ranges if metadata else [])
        ],
        "first_last": (
            metadata.first_date.isoformat() if metadata and metadata.first_date else None,
            metadata.last_date.isoformat() if metadata and metadata.last_date else None,
        ),
        "expected": audit_new.expected_sessions,
        "missing_old": audit_old.missing_sessions,
        "missing_new": audit_new.missing_sessions,
        "coverage_old": str(audit_old.coverage_pct),
        "coverage_new": str(audit_new.coverage_pct),
        "queried_dates_in_range": len(covered_queried),
    }


async def main() -> None:
    # 挑几只有代表性的标的(含可能停牌过的)
    candidates = [
        "600519.SH",  # 茅台
        "000001.SZ",  # 平安银行
        "600000.SH",  # 浦发银行
        "300750.SZ",  # 宁德时代
        "688981.SH",  # 中芯国际(2020-07-16 上市)
        "002230.SZ",  # 科大讯飞
        "601398.SH",  # 工商银行
        "000651.SZ",  # 格力电器
        "600276.SH",  # 恒瑞医药
        "601318.SH",  # 中国平安
        "000002.SZ",  # 万科A(常停牌)
        "002594.SZ",  # 比亚迪
        "600036.SH",  # 招商银行
        "601166.SH",  # 兴业银行
        "600030.SH",  # 中信证券
    ]
    results = []
    for code in candidates:
        try:
            results.append(await _audit_one(code))
        except Exception as exc:
            print(f"[{code}] ERROR: {type(exc).__name__}: {exc}")
    for r in results:
        print(r)

    # 完整端到端发布测试(不进 DB,只验证文件发布)
    print("\n=== 端到端发布测试 ===")
    instruments: list[ReleaseInstrumentSpec] = []
    for code in candidates:
        cache = ParquetCache(CACHE_DIR)
        metadata = await cache.metadata_for(
            Symbol(code=code, market=Market.A_SHARE), BarPeriod.D1, "qfq"
        )
        if metadata is None:
            print(f"[{code}] skip (no cache)")
            continue
        instruments.append(_make_spec(code, metadata))

    if await asyncio.to_thread(RELEASE_ROOT.exists):
        import shutil

        await asyncio.to_thread(shutil.rmtree, RELEASE_ROOT, True)
    await asyncio.to_thread(lambda: RELEASE_ROOT.mkdir(parents=True, exist_ok=True))

    builder = FrozenDatasetReleaseBuilder(
        cache_dir=CACHE_DIR,
        release_root=RELEASE_ROOT,
        max_concurrency=4,
    )
    spec = DatasetReleaseSpec(
        release_id="real-test-v1",
        dataset_name="real_test",
        source="tushare",
        version="v1",
        start_date=START,
        end_date=END,
        code_version="x",
        required_capabilities=(),
        minimum_symbol_coverage=Decimal("0.98"),
        minimum_release_coverage=Decimal("0.98"),
    )
    try:
        release = await builder.publish(spec, instruments)
        print(f"PUBLISHED: {release.release_id}")
        print(f"  symbol_count={release.symbol_count}")
        print(f"  coverage_pct={release.coverage_pct}")
        print(f"  quality_status={release.quality_status.value}")
        for item in release.instruments:
            print(
                f"  {item.code}: coverage={item.coverage_pct} "
                f"missing={item.missing_sessions} suspended={item.suspended_sessions} "
                f"issues={item.issues}"
            )
    except Exception as exc:
        print(f"PUBLISH FAILED: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
