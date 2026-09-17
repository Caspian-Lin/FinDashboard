"""因子快照持久化与幂等回读。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_backtest import BacktestConfig, BacktestEngine
from finboard_backtest.selection import PointInTimeFactorSelector
from finboard_core import Strategy
from finboard_data import (
    FactorInputBatch,
    FactorInputRecord,
    FactorName,
    FactorSelectionConfig,
    FactorSnapshot,
    FactorSnapshotStatus,
    FactorValue,
    InputsMode,
)
from finboard_persistence import FactorSnapshotRepository, session_factory
from finboard_persistence.models import FactorSnapshotModel, FactorValueModel
from finboard_shared.identifiers import StrategyId
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod


def _published_snapshot(checksum: str) -> FactorSnapshot:
    return FactorSnapshot(
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
        checksum=checksum,
    )


@pytest.mark.asyncio
async def test_factor_snapshot_round_trip_is_idempotent(
    db_session: AsyncSession,
) -> None:
    snapshot = _published_snapshot(checksum="a" * 64)
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


@pytest.mark.asyncio
async def test_factor_snapshot_concurrent_sessions_same_checksum(
    db_session: AsyncSession,
    _engine: AsyncEngine,  # noqa: PT019
) -> None:
    """并发双会话保存同一 checksum:唯一索引原子仲裁,只留一条(#204)。

    显式编排 TOCTOU 窗口:A 插入后不提交,B 在 A 未提交期间发起保存
    (SELECT-then-INSERT 实现下 B 看不到 A 的行、双 INSERT 撞唯一约束),
    B 的 INSERT 在服务端阻塞等 A 的事务结局, A 提交后 B 才继续。
    """
    snapshot = _published_snapshot(checksum="b" * 64)
    smaker = session_factory(_engine)
    a_may_commit = asyncio.Event()
    b_started = asyncio.Event()

    async def _save_a_hold_then_commit() -> int:
        async with smaker() as session:
            repo = FactorSnapshotRepository(session)
            snapshot_id = await repo.save_factor_snapshot(snapshot)
            b_started.set()
            await a_may_commit.wait()
            await session.commit()
            return snapshot_id

    async def _save_b() -> int:
        await b_started.wait()
        async with smaker() as session:
            repo = FactorSnapshotRepository(session)
            snapshot_id = await repo.save_factor_snapshot(snapshot)
            await session.commit()
            return snapshot_id

    async def _release_a_after_b_reaches_conflict() -> None:
        await b_started.wait()
        # 给 B 的 INSERT 时间到达服务端、卡在 A 的未提交行上;
        # 若 B 尚未到达,提交后的冲突路径同样幂等,只是不再覆盖竞态。
        await asyncio.sleep(0.2)
        a_may_commit.set()

    first_id, second_id, _ = await asyncio.gather(
        _save_a_hold_then_commit(),
        _save_b(),
        _release_a_after_b_reaches_conflict(),
    )

    assert first_id == second_id
    snapshot_rows = (
        (
            await db_session.execute(
                select(FactorSnapshotModel).where(
                    FactorSnapshotModel.checksum == snapshot.checksum
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(snapshot_rows) == 1
    value_rows = (
        (
            await db_session.execute(
                select(FactorValueModel).where(
                    FactorValueModel.snapshot_id == first_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(value_rows) == len(snapshot.values)


class _NoopStrategy(Strategy):
    """只吃事件不做交易,让引擎把 selection 快照走完持久化链路。"""

    @property
    def strategy_id(self) -> StrategyId:
        return StrategyId("noop")

    async def on_market_data(self, event: object, ctx: object) -> None:
        del event, ctx

    async def on_universe_selection(self, event: object, ctx: object) -> None:
        del event, ctx


class _RisingBarProvider:
    """5 个交易日递增收盘价,最后一天对齐 end。"""

    async def fetch_bars(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[Bar]:
        del period, start, adjust
        first = end - timedelta(days=4)
        return [
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime.combine(
                    first + timedelta(days=i), datetime.min.time(), tzinfo=UTC
                ),
                open=Decimal(str(10 + i)),
                high=Decimal(str(10 + i)),
                low=Decimal(str(10 + i)),
                close=Decimal(str(10 + i)),
                volume=Decimal("100"),
            )
            for i in range(5)
        ]


class _BareBarsReader:
    """bars 模式的裸 reader:无 profile/daily/financial 的因子输入记录。"""

    async def load_factor_inputs(
        self,
        *,
        symbols: tuple[str, ...],
        business_date: date,
        decision_at: datetime,
        source: str,
        required_datasets: frozenset[str],
        dataset_versions: dict[str, str],
    ) -> FactorInputBatch:
        del business_date, decision_at, source, dataset_versions
        return FactorInputBatch(
            records=tuple(
                FactorInputRecord(
                    symbol=symbol,
                    profile=None,
                    daily=None,
                    financial=None,
                    industry=None,
                )
                for symbol in symbols
            ),
            source="tushare",
            dataset_versions={},
            issues=(),
        )


@pytest.mark.asyncio
async def test_selection_backtest_repeat_run_different_capital_reuses_snapshot(
    db_session: AsyncSession,
) -> None:
    """相同 selection 配置不同资金重复回测:checksum 不含 capital(#204)。

    第二次运行的逐日快照与第一次逐字节同 checksum,必须原子复用既有
    记录而不是撞 ix_factor_snapshots_checksum 唯一约束。
    """
    selection = FactorSelectionConfig(
        enabled=True,
        inputs_mode=InputsMode.BARS,
        ranking_factor=FactorName.MOMENTUM,
        momentum_lookback=2,
        max_symbols=2,
    )

    async def _run(capital: Decimal) -> Sequence[FactorSnapshot]:
        engine = BacktestEngine(
            strategy=_NoopStrategy(),
            data_provider=_RisingBarProvider(),
            config=BacktestConfig(
                symbols=["000002.SZ", "000001.SZ"],
                start=date(2024, 1, 1),
                end=date(2024, 1, 5),
                initial_capital=capital,
                selection=selection,
            ),
            factor_selector=PointInTimeFactorSelector(
                reader=_BareBarsReader(),
                writer=FactorSnapshotRepository(db_session),
            ),
        )
        result = await engine.run()
        await db_session.commit()
        return result.selection_snapshots

    first_run = await _run(Decimal("100000"))
    second_run = await _run(Decimal("250000"))

    assert first_run, "回测应产出逐日 selection 快照"
    assert [s.checksum for s in second_run] == [s.checksum for s in first_run]
    assert [s.snapshot_id for s in second_run] == [s.snapshot_id for s in first_run]

    stored = (
        (await db_session.execute(select(FactorSnapshotModel.checksum)))
        .scalars()
        .all()
    )
    assert sorted(stored) == sorted(s.checksum for s in first_run)
    expected_values = sum(len(s.values) for s in first_run)
    value_rows = (
        (
            await db_session.execute(
                select(FactorValueModel).where(
                    FactorValueModel.snapshot_id.in_(
                        s.snapshot_id for s in first_run if s.snapshot_id
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(value_rows) == expected_values, "因子值不得随重复运行翻倍"
