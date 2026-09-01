"""跨发布标的集一致性校验集成测试(issue #252)。

覆盖 ``DatasetPublishExecutor`` 的发布期基线校验与 :func:`symbol_set_diff`:
* 差集 + ``consistency_fail_on_mismatch=true`` → 秒级 ``symbol_set_mismatch``
  (差集具名进 context),不写任何发布数据;
* 差集 + 默认(只 warning)→ 发布成功,phase 摘要携带 mismatch 计数;
* 基线发布不存在 → 具名 ``baseline_release_not_found``;
* ``symbol_set_diff`` 与 REST/MCP diff 查询同源,计数精确、差集具名。
需 PostgreSQL。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_backtest.background_jobs.contracts import ExecutorError, JobRecord
from finboard_backtest.background_jobs.executors.dataset_publish import (
    DatasetPublishExecutor,
)
from finboard_data.cache import ParquetCache
from finboard_data.releases import symbol_set_diff
from finboard_persistence import (
    InstrumentModel,
    ResearchDatasetReleaseModel,
    ResearchDatasetReleaseRepository,
    ResearchDatasetReleaseService,
    session_factory,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

pytestmark = pytest.mark.asyncio

_START = date(2024, 1, 2)
_END = date(2024, 1, 5)
_SYMBOLS = ["TST078.SH", "TST079.SH"]
_MISSING_SYMBOL = "TST079.SH"

_BASELINE_ID = "consistency-baseline-v1"
_CANDIDATE_ID = "consistency-candidate-v1"


@pytest_asyncio.fixture
async def test_stock(db_session: AsyncSession) -> AsyncIterator[None]:
    await db_session.execute(
        delete(InstrumentModel).where(InstrumentModel.code == "TST078.SH")
    )
    for code, name in (
        ("TST078.SH", "issue252 固定股票样本 A"),
        ("TST079.SH", "issue252 固定股票样本 B"),
    ):
        db_session.add(
            InstrumentModel(
                code=code,
                name=name,
                market="a_share",
                instrument_type="stock",
                exchange="SSE",
                list_date=date(2020, 1, 1),
                status="active",
                updated_at=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )
    await db_session.flush()
    yield
    await db_session.rollback()
    await db_session.execute(
        delete(InstrumentModel).where(
            InstrumentModel.code.in_(("TST078.SH", "TST079.SH"))
        )
    )
    await db_session.commit()


async def _seed_bars(cache_dir: Path) -> None:
    cache = ParquetCache(cache_dir)
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
                source="tushare",
            )
            for offset in range(4)
        ]
        await cache.write(symbol, BarPeriod.D1, "qfq", bars)


async def _publish_baseline(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """经完整发布流建立基线发布(全部 _SYMBOLS)。"""
    await _seed_bars(tmp_path / "cache")
    service = ResearchDatasetReleaseService(
        db_session,
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    )
    await service.publish(
        _spec(_BASELINE_ID, "a_share_tushare"),
        _SYMBOLS,
    )
    await db_session.commit()


def _spec(release_id: str, release_kind: str, version: str = "consistency-v1"):
    from finboard_data import DatasetReleaseSpec

    return DatasetReleaseSpec(
        release_id=release_id,
        dataset_name="multi_asset_daily_bars",
        source="tushare" if release_kind == "a_share_tushare" else release_kind,
        version=version,
        start_date=_START,
        end_date=_END,
        code_version="consistency-test",
        required_capabilities=("stock",),
    )


def _job(payload: dict[str, object]) -> JobRecord:
    return JobRecord(
        job_id="BJ-RCONS01",
        kind="dataset_publish",
        queue="data",
        payload=payload,
        attempt=1,
        max_attempts=3,
        requested_by="integration-test",
    )


def _candidate_payload(
    *, symbols: list[str], baseline: str | None, fail: bool = False
) -> dict[str, object]:
    return {
        "release_id": _CANDIDATE_ID,
        "dataset_name": "multi_asset_daily_bars",
        "release_kind": "a_share_tushare",
        "version": "consistency-cand-v1",
        "start_date": _START.isoformat(),
        "end_date": _END.isoformat(),
        "adjustment": "qfq",
        "symbols": symbols,
        "required_capabilities": ["stock"],
        "consistency_baseline_release_id": baseline,
        "consistency_fail_on_mismatch": fail,
    }


async def _cleanup(db_session: AsyncSession) -> None:
    await db_session.execute(
        delete(ResearchDatasetReleaseModel).where(
            ResearchDatasetReleaseModel.release_id.in_(
                (_BASELINE_ID, _CANDIDATE_ID)
            )
        )
    )
    await db_session.commit()


@pytest.mark.usefixtures("test_stock")
async def test_baseline_mismatch_fails_closed(
    _engine: AsyncEngine,  # noqa: PT019 - 共享集成测试 fixture 的既有命名
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """差集 + fail_on_mismatch → 具名秒级失败,不写发布数据。"""
    await _publish_baseline(db_session, tmp_path)
    monkeypatch.setattr(
        "finboard_backtest.background_jobs.executors.dataset_publish.cache_dir",
        lambda: str(tmp_path / "cache"),
    )
    monkeypatch.setattr(
        "finboard_backtest.background_jobs.executors.dataset_publish.release_root",
        lambda: str(tmp_path / "releases"),
    )
    executor = DatasetPublishExecutor(session_maker=session_factory(_engine))
    symbols = [s for s in _SYMBOLS if s != _MISSING_SYMBOL]
    with pytest.raises(ExecutorError) as exc_info:
        await executor.execute(
            _job(_candidate_payload(symbols=symbols, baseline=_BASELINE_ID, fail=True)),
            _noop_progress,
        )
    assert exc_info.value.code == "symbol_set_mismatch"
    assert exc_info.value.context is not None
    assert exc_info.value.context.get("missing_in_release") == [_MISSING_SYMBOL]
    assert exc_info.value.context.get("extra_in_release") == []
    # fail-closed:候选发布未登记。
    assert await ResearchDatasetReleaseRepository(db_session).get(_CANDIDATE_ID) is None
    await _cleanup(db_session)


@pytest.mark.usefixtures("test_stock")
async def test_baseline_mismatch_warns_by_default(
    _engine: AsyncEngine,  # noqa: PT019 - 共享集成测试 fixture 的既有命名
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """差集 + 默认策略 → 发布成功(warning 不阻断),phase 摘要带计数。"""
    await _publish_baseline(db_session, tmp_path)
    monkeypatch.setattr(
        "finboard_backtest.background_jobs.executors.dataset_publish.cache_dir",
        lambda: str(tmp_path / "cache"),
    )
    monkeypatch.setattr(
        "finboard_backtest.background_jobs.executors.dataset_publish.release_root",
        lambda: str(tmp_path / "releases"),
    )
    executor = DatasetPublishExecutor(session_maker=session_factory(_engine))
    phases: list[str] = []

    async def _progress(done: int, total: int | None, phase: str | None) -> None:
        del done, total
        phases.append(phase or "")

    symbols = [s for s in _SYMBOLS if s != _MISSING_SYMBOL]
    result = await executor.execute(
        _job(_candidate_payload(symbols=symbols, baseline=_BASELINE_ID)),
        _progress,
    )
    assert result.status == "succeeded"
    assert result.result_ref == _CANDIDATE_ID
    assert phases
    assert "symbol_set_mismatch" in phases[-1]

    # diff 查询同源函数:计数精确、差集具名(REST/MCP 同一实现)。
    repo = ResearchDatasetReleaseRepository(db_session)
    baseline = await repo.get(_BASELINE_ID)
    candidate = await repo.get(_CANDIDATE_ID)
    assert baseline is not None
    assert candidate is not None
    diff = symbol_set_diff(baseline, candidate)
    assert diff["consistent"] is False
    assert diff["common_count"] == len(symbols)
    assert diff["only_in_release"] == [_MISSING_SYMBOL]
    assert diff["only_in_other"] == []
    await _cleanup(db_session)


@pytest.mark.usefixtures("test_stock")
async def test_baseline_not_found_fails(
    _engine: AsyncEngine,  # noqa: PT019 - 共享集成测试 fixture 的既有命名
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """基线发布不存在 → 具名 baseline_release_not_found(不盲发)。"""
    await _publish_baseline(db_session, tmp_path)
    monkeypatch.setattr(
        "finboard_backtest.background_jobs.executors.dataset_publish.cache_dir",
        lambda: str(tmp_path / "cache"),
    )
    monkeypatch.setattr(
        "finboard_backtest.background_jobs.executors.dataset_publish.release_root",
        lambda: str(tmp_path / "releases"),
    )
    executor = DatasetPublishExecutor(session_maker=session_factory(_engine))
    with pytest.raises(ExecutorError) as exc_info:
        await executor.execute(
            _job(
                _candidate_payload(
                    symbols=list(_SYMBOLS), baseline="no-such-release"
                )
            ),
            _noop_progress,
        )
    assert exc_info.value.code == "baseline_release_not_found"
    await _cleanup(db_session)


async def _noop_progress(_done: int, _total: int | None, _phase: str | None) -> None:
    pass
