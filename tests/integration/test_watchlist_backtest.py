"""Watchlist + Backtest 集成测试(需 PostgreSQL)。

验证 WatchlistRepository / BacktestRunRepository 的完整 CRUD 链路。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence import (
    BacktestRunModel,
    BacktestRunRepository,
    WatchlistRepository,
)


@pytest.mark.asyncio
class TestWatchlistRepository:
    """标的组(watchlist)仓储集成测试。"""

    async def test_create_and_list(self, db_session: AsyncSession) -> None:
        repo = WatchlistRepository(db_session)
        w1 = await repo.create("ETF池", "ETF 组合")
        w2 = await repo.create("蓝筹股")
        await db_session.commit()

        rows = await repo.list_all()
        codes = {r.id for r in rows}
        assert w1.id in codes
        assert w2.id in codes

    async def test_add_and_list_items(self, db_session: AsyncSession) -> None:
        repo = WatchlistRepository(db_session)
        w = await repo.create("我的组合")
        await db_session.commit()

        added = await repo.add_symbols(w.id, ["510300.SH", "159915.SZ"])
        assert len(added) == 2
        # 去重
        added2 = await repo.add_symbols(w.id, ["510300.SH", "600519.SH"])
        assert len(added2) == 1  # 510300.SH 已存在,只新增 600519.SH
        await db_session.commit()

        items = await repo.items(w.id)
        codes = {it.symbol_code for it in items}
        assert codes == {"510300.SH", "159915.SZ", "600519.SH"}

    async def test_remove_symbol(self, db_session: AsyncSession) -> None:
        repo = WatchlistRepository(db_session)
        w = await repo.create("测试组")
        await repo.add_symbols(w.id, ["510300.SH", "600519.SH"])
        await db_session.commit()

        ok = await repo.remove_symbol(w.id, "510300.SH")
        assert ok is True
        await db_session.commit()

        items = await repo.items(w.id)
        assert len(items) == 1
        assert items[0].symbol_code == "600519.SH"

    async def test_remove_nonexistent_symbol(self, db_session: AsyncSession) -> None:
        repo = WatchlistRepository(db_session)
        w = await repo.create("空组")
        await db_session.commit()

        ok = await repo.remove_symbol(w.id, "不存在.SH")
        assert ok is False

    async def test_rename(self, db_session: AsyncSession) -> None:
        repo = WatchlistRepository(db_session)
        w = await repo.create("旧名")
        await db_session.commit()

        row = await repo.rename(w.id, "新名", "描述")
        assert row is not None
        assert row.name == "新名"
        assert row.description == "描述"

    async def test_delete_cascades_items(self, db_session: AsyncSession) -> None:
        repo = WatchlistRepository(db_session)
        w = await repo.create("待删组")
        await repo.add_symbols(w.id, ["510300.SH", "600519.SH"])
        await db_session.commit()
        wid = w.id

        ok = await repo.delete(wid)
        assert ok is True
        await db_session.commit()

        assert await repo.get(wid) is None
        items = await repo.items(wid)
        assert len(items) == 0


@pytest.mark.asyncio
class TestBacktestRunRepository:
    """回测历史仓储集成测试。"""

    async def test_save_and_get(self, db_session: AsyncSession) -> None:
        repo = BacktestRunRepository(db_session)
        run = BacktestRunModel(
            strategy="ma_cross",
            symbols=["510300.SH"],
            start="2024-01-01",
            end="2024-06-01",
            capital=Decimal("100000"),
            adjust="qfq",
            params={"short_window": 5, "long_window": 20},
            metrics={"total_return": 0.05, "sharpe_ratio": 1.5},
            equity_curve=[{"date": "2024-01-01", "equity": 100000.0, "benchmark": None}],
            fills=[
                {"date": "2024-01-05", "symbol": "510300.SH", "side": "buy",
                 "quantity": "100", "price": "4.50", "commission": "5.00"}
            ],
            summary="回测报告",
        )
        saved = await repo.save(run)
        await db_session.commit()

        fetched = await repo.get(saved.id)
        assert fetched is not None
        assert fetched.strategy == "ma_cross"
        assert fetched.symbols == ["510300.SH"]
        assert fetched.metrics["total_return"] == 0.05
        assert len(fetched.equity_curve) == 1
        assert len(fetched.fills) == 1

    async def test_list_recent(self, db_session: AsyncSession) -> None:
        repo = BacktestRunRepository(db_session)
        for i in range(3):
            await repo.save(
                BacktestRunModel(
                    strategy="ma_cross",
                    symbols=["510300.SH"],
                    start="2024-01-01",
                    end="2024-06-01",
                    capital=Decimal("100000"),
                    adjust="qfq",
                    params={},
                    metrics={"total_return": float(i) / 100},
                    equity_curve=[],
                    fills=[],
                    summary=f"run {i}",
                )
            )
        await db_session.commit()

        rows = await repo.list_recent(limit=10)
        assert len(rows) == 3
        # 按创建时间倒序,最新的在前
        assert "run 2" in rows[0].summary

    async def test_delete(self, db_session: AsyncSession) -> None:
        repo = BacktestRunRepository(db_session)
        run = await repo.save(
            BacktestRunModel(
                strategy="ma_cross",
                symbols=["510300.SH"],
                start="2024-01-01",
                end="2024-06-01",
                capital=Decimal("100000"),
                adjust="qfq",
                params={},
                metrics={},
                equity_curve=[],
                fills=[],
                summary="",
            )
        )
        await db_session.commit()
        rid = run.id

        ok = await repo.delete(rid)
        assert ok is True
        await db_session.commit()
        assert await repo.get(rid) is None
