"""因子快照持久化与幂等回读。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_data import (
    FactorName,
    FactorSnapshot,
    FactorSnapshotStatus,
    FactorValue,
)
from finboard_persistence import FactorSnapshotRepository


@pytest.mark.asyncio
async def test_factor_snapshot_round_trip_is_idempotent(
    db_session: AsyncSession,
) -> None:
    snapshot = FactorSnapshot(
        decision_at=datetime(2026, 7, 24, 17, tzinfo=UTC),
        business_date=date(2026, 7, 24),
        effective_date=date(2026, 7, 27),
        source="tushare",
        dataset_versions={
            "daily_metrics": "daily-20260724",
            "instrument_profiles": "profiles-v1",
        },
        factor_version="v1",
        static_universe=("000001.SZ",),
        selected_symbols=("000001.SZ",),
        values=(
            FactorValue(
                symbol="000001.SZ",
                factor_name=FactorName.MARKET_CAP,
                value=Decimal("1234000000"),
                global_rank=1,
                industry_rank=1,
                industry_code="461101",
            ),
        ),
        status=FactorSnapshotStatus.PUBLISHED,
        skip_reason=None,
        config={"enabled": True, "ranking_factor": "market_cap"},
        checksum="a" * 64,
    )
    repo = FactorSnapshotRepository(db_session)

    first_id = await repo.save_factor_snapshot(snapshot)
    repeated_id = await repo.save_factor_snapshot(snapshot)
    await db_session.commit()
    loaded = await repo.get_by_checksum(snapshot.checksum)

    assert repeated_id == first_id
    assert loaded is not None
    assert loaded.snapshot_id == first_id
    assert loaded.dataset_versions == snapshot.dataset_versions
    assert loaded.selected_symbols == snapshot.selected_symbols
    assert loaded.values[0].value == Decimal("1234000000.00000000")
