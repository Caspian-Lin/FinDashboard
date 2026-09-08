"""数据集发布分段短事务回归测试(全市场发布事故 BJ-2307A2188D1645BA)。

物化(7000+ 符号 x 全历史流式冻结)是分钟级纯文件 I/O。事故根因:整个
发布包在一个打开的 PG 事务里,物化期间连接呈 ``idle in transaction``,
被 ``idle_in_transaction_session_timeout`` 杀掉,重试确定性复现。

本测试在物化收尾(``_atomically_publish`` 前)检查 ``pg_stat_activity``:
``session_factory`` 分段模式下,测试库不允许存在任何 idle in transaction
会话(旧单事务模式下 executor 的连接正处于该状态,断言必然失败)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_data import DatasetReleaseSpec
from finboard_data.cache import ParquetCache
from finboard_data.releases import FrozenDatasetReleaseBuilder
from finboard_persistence import (
    InstrumentModel,
    ResearchDatasetReleaseModel,
    ResearchDatasetReleaseService,
    session_factory,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market
from tests.integration.conftest import TEST_DB_URL

pytestmark = pytest.mark.asyncio

_START = date(2024, 1, 2)
_END = date(2024, 1, 5)
_STOCK = "TST078.SH"
# 与 test_dataset_releases 同一套样本:五类 ETF 分别满足
# etf:index / cross_border / commodity / bond capability 门。
_SYMBOLS = [
    _STOCK,
    "510300.SH",
    "513100.SH",
    "518880.SH",
    "511010.SH",
]


async def test_materialization_runs_without_idle_in_transaction(
    _engine: AsyncEngine,  # noqa: PT019 - 共享集成测试 fixture 的既有命名
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_name = make_url(TEST_DB_URL).database

    # 标的登记与缓存种子各自用独立短事务,不留打开的事务。
    async with session_factory(_engine)() as session:
        await session.execute(
            delete(InstrumentModel).where(InstrumentModel.code == _STOCK)
        )
        session.add(
            InstrumentModel(
                code=_STOCK,
                name="分段事务回归样本",
                market="a_share",
                instrument_type="stock",
                exchange="SSE",
                list_date=date(2020, 1, 1),
                status="active",
                updated_at=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )
        await session.commit()

    cache = ParquetCache(tmp_path / "cache")
    for code in _SYMBOLS:
        symbol = Symbol(code, Market.A_SHARE)
        bars = [
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime.combine(
                    _START + timedelta(days=offset),
                    datetime.min.time(),
                    tzinfo=UTC,
                ),
                open=Decimal("10"),
                high=Decimal("11"),
                low=Decimal("9"),
                close=Decimal("10.5"),
                volume=Decimal("1000"),
                amount=Decimal("10500"),
                source="fixed_sample",
            )
            for offset in range(4)
        ]
        await cache.write(symbol, BarPeriod.D1, "qfq", bars)

    # 在物化收尾(尚未进入登记段)探测 pg_stat_activity。
    original = FrozenDatasetReleaseBuilder._atomically_publish
    observed: dict[str, int] = {}

    async def spy(
        self: FrozenDatasetReleaseBuilder,
        spec: DatasetReleaseSpec,
        staging: Path,
        final_dir: Path,
    ) -> None:
        async with session_factory(_engine)() as probe:
            row: Any = (
                await probe.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = :db AND state = 'idle in transaction' "
                        "AND pid <> pg_backend_pid()"
                    ),
                    {"db": db_name},
                )
            ).scalar_one()
        observed["idle_in_transaction"] = int(row)
        await original(self, spec, staging, final_dir)

    monkeypatch.setattr(FrozenDatasetReleaseBuilder, "_atomically_publish", spy)

    service = ResearchDatasetReleaseService(
        None,
        session_factory=session_factory(_engine),
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    )
    release = await service.publish(
        DatasetReleaseSpec(
            release_id="integration-batched-v1",
            dataset_name="multi_asset_daily_bars",
            source="fixed_sample",
            version="batched-v1",
            start_date=_START,
            end_date=_END,
            code_version="integration-test",
        ),
        _SYMBOLS,
    )

    assert release.release_id == "integration-batched-v1"
    assert observed["idle_in_transaction"] == 0, (
        "物化阶段存在 idle in transaction 会话:"
        "发布退回了单事务模式,全市场规模发布会被"
        " idle_in_transaction_session_timeout 杀掉"
    )

    # 登记行可见(分段登记段已 commit),随后自清理,不留跨测试状态。
    async with session_factory(_engine)() as session:
        registered = (
            await session.execute(
                select(ResearchDatasetReleaseModel).where(
                    ResearchDatasetReleaseModel.release_id == release.release_id
                )
            )
        ).scalar_one()
        assert registered.release_checksum == release.release_checksum
        await session.execute(
            delete(ResearchDatasetReleaseModel).where(
                ResearchDatasetReleaseModel.release_id == release.release_id
            )
        )
        await session.execute(
            delete(InstrumentModel).where(InstrumentModel.code == _STOCK)
        )
        await session.commit()
