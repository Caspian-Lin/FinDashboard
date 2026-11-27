"""生成可独立下载的合成入门数据包。仅在打包时运行。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import tempfile
import zipfile
from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import exchange_calendars as xcals  # type: ignore[import-untyped]

from finboard_app.starter_data import (
    STARTER_BENCHMARK,
    STARTER_ID,
    STARTER_SOURCE,
    STARTER_SYMBOLS,
)
from finboard_data import trading_calendar
from finboard_data.cache import ParquetCache
from finboard_data.releases import (
    RELEASE_MANIFEST_FILENAME,
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    ReleaseInstrumentSpec,
    _release_checksum,
    _write_manifest,
    default_execution_metadata,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import AssetClass, BarPeriod, InstrumentType, ListingStatus, Market

_START = date(2023, 1, 3)
_END = date(2024, 2, 29)
_LISTED = date(2020, 1, 1)
_PUBLISHED_AT = datetime(2024, 3, 1, tzinfo=UTC)


async def build(output: Path) -> None:
    calendar = xcals.get_calendar("XSHG", start=_START.isoformat(), end=_END.isoformat())
    days = [session.date() for session in calendar.sessions]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".starter-build-", dir=output.parent) as directory:
        root = Path(directory)
        cache = ParquetCache(root / "cache")
        instruments: list[ReleaseInstrumentSpec] = []
        for n, code in enumerate((*STARTER_SYMBOLS, STARTER_BENCHMARK)):
            symbol = Symbol(code, Market.A_SHARE)
            kind = InstrumentType.INDEX if code == STARTER_BENCHMARK else InstrumentType.STOCK
            instruments.append(
                ReleaseInstrumentSpec(
                    code=code, name=f"合成样例 {code}", market=symbol.market, instrument_type=kind,
                    asset_class=AssetClass.EQUITY, available_at=datetime.combine(_LISTED, time(), UTC),
                    execution=default_execution_metadata(market=symbol.market, instrument_type=kind),
                    list_date=_LISTED, listing_board="main", status=ListingStatus.ACTIVE,
                    exchange="SSE", industry=f"Synthetic-{n % 3}",
                )
            )
            rng = random.Random(547 + n)
            price = Decimal(24 + 2 * n)
            bars: list[Bar] = []
            for day in days:
                close = price + Decimal(rng.randrange(-24, 36)) / 100
                bars.append(
                    Bar(
                        symbol=symbol, period=BarPeriod.D1, timestamp=datetime.combine(day, time(7), UTC),
                        open=price, high=max(price, close) + Decimal("0.03"),
                        low=min(price, close) - Decimal("0.03"), close=close,
                        volume=Decimal(10_000_000), amount=close * 10_000_000,
                        source=STARTER_SOURCE,
                    )
                )
                price = close
            await cache.write(symbol, BarPeriod.D1, "qfq", bars)

        release_root = root / "releases"
        builder = FrozenDatasetReleaseBuilder(cache_dir=root / "cache", release_root=release_root)
        # 固定交易日轴, 避免在线日历或构建日期改变数据。
        with patch.object(trading_calendar, "_CACHE", set(days)):
            release = await builder.publish(
                DatasetReleaseSpec(
                    release_id=STARTER_ID, dataset_name=STARTER_ID, source=STARTER_SOURCE,
                    version="v1", start_date=_START, end_date=_END, code_version=STARTER_ID,
                    required_capabilities=("stock", "index"),
                    known_limitations=("全量合成行情。仅供产品演示。",),
                ),
                instruments,
            )
        release = replace(release, published_at=_PUBLISHED_AT)
        release = replace(release, release_checksum=_release_checksum(release))
        folder = release_root / STARTER_ID
        _write_manifest(folder, release)

        content = {
            f"{STARTER_ID}/{path}": (folder / path).read_bytes()
            for path in [RELEASE_MANIFEST_FILENAME, *(item.artifact_path for item in release.instruments)]
        }
        index = {
            "format": STARTER_ID,
            "release_checksum": release.release_checksum,
            "trading_days": [day.isoformat() for day in days],
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in content.items()},
        }
        content["starter.json"] = json.dumps(index, sort_keys=True).encode()
        with zipfile.ZipFile(output, "w") as archive:
            for name, data in sorted(content.items()):
                archive.writestr(zipfile.ZipInfo(name), data, compress_type=zipfile.ZIP_DEFLATED)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    asyncio.run(build(parser.parse_args().output))
