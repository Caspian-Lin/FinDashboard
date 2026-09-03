"""issue #306:加载期分块探针 —— run status / cancel 轮询挂载点行为。

锁定四组不变量:

* 正常存活:探针在每个分块边界被调用 ``(done, total)``,全部期次照常加载
  (contexts 顺序与无探针路径一致);
* 中途打断:第 N 块边界抛 ``ResearchRunInterruptedError`` → 原样透传,
  不经 #263 ``attach_decision_load_context`` 数据失败标记(打断是协作取消,
  不是数据失败);
* 首块前打断:零期次加载即中止;
* 探针按 chunk 数量调用(6 期 = ceil(6/4) = 2 次分块边界),不随 symbol
  数量放大。

真实发布用 :class:`FrozenDatasetReleaseBuilder` 在临时目录构建,不依赖
PostgreSQL(探针本体由集成测试经真实 DB 会话覆盖)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchRunInterruptedError,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_run.failure_context import (
    read_decision_load_context,
)
from finboard_backtest.research_run.signal_engine import (
    build_decision_load_contexts,
)
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_backtest.strategy_spec.contracts import (
    FeatureGraph,
    FeatureKind,
    FeatureNode,
    FeatureOperator,
    ResearchStrategySpec,
    SignalAction,
    SignalComparator,
    SignalRule,
    SignalRules,
)
from finboard_data.cache import ParquetCache
from finboard_data.releases import (
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
    ReleaseInstrumentSpec,
    default_execution_metadata,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import AssetClass, BarPeriod, InstrumentType, Market

# ---- 真实发布构造(6 个决策期 → 2 个分块)-----------------------------------

_SESSIONS_COUNT = 22 * 6
_SYMBOLS = ("600519.SH", "000001.SZ", "600036.SH")
_RELEASE_ID = "load-probe-306-r1"


def _sessions() -> list[date]:
    days: list[date] = []
    cursor = date(2024, 1, 2)
    while len(days) < _SESSIONS_COUNT:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


SESSIONS = _sessions()
_START = SESSIONS[0]
_END = SESSIONS[-1]


def _close(code: str, day: date) -> Decimal:
    base = {"600519.SH": "1500.00", "000001.SZ": "10.00", "600036.SH": "30.00"}[code]
    step = SESSIONS.index(day)
    wiggle = Decimal("1.01") if step % 2 == 0 else Decimal("0.99")
    return Decimal(base) * (Decimal("1") + Decimal(step) / Decimal("200")) * wiggle


def _bars(code: str) -> list[Bar]:
    symbol = Symbol(code=code, market=Market.A_SHARE)
    return [
        Bar(
            symbol=symbol,
            period=BarPeriod.D1,
            timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            open=_close(code, day) * Decimal("1.01"),
            high=_close(code, day) * Decimal("1.02"),
            low=_close(code, day) * Decimal("0.98"),
            close=_close(code, day),
            volume=Decimal(10000),
            amount=Decimal("100000"),
            source="fixed_sample",
        )
        for day in SESSIONS
    ]


def _instrument(code: str) -> ReleaseInstrumentSpec:
    return ReleaseInstrumentSpec(
        code=code,
        name=f"样本{code}",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2001, 8, 27, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        list_date=date(2001, 8, 27),
    )


async def _build_release(tmp_path: Path) -> FrozenReleaseProvider:
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    instruments = [_instrument(code) for code in _SYMBOLS]
    cache = ParquetCache(cache_dir)
    for instrument in instruments:
        await cache.write(
            Symbol(code=instrument.code, market=instrument.market),
            BarPeriod.D1,
            "qfq",
            _bars(instrument.code),
        )
    await FrozenDatasetReleaseBuilder(
        cache_dir=cache_dir,
        release_root=release_root,
    ).publish(
        DatasetReleaseSpec(
            release_id=_RELEASE_ID,
            dataset_name="load_probe_306_daily_bars",
            source="fixed_sample",
            version="2024.01",
            start_date=_START,
            end_date=_END,
            code_version="deadbeef",
            required_capabilities=("stock",),
        ),
        instruments,
    )
    return FrozenReleaseProvider(release_root=release_root, release_id=_RELEASE_ID)


def _price_only_spec() -> ResearchStrategySpec:
    spec = build_strategy_template(
        "multi_factor",
        strategy_id="load_probe_306_test",
        dataset_release_ids=(_RELEASE_ID,),
    )
    nodes = (
        FeatureNode(
            node_id="momentum",
            label="动量",
            kind=FeatureKind.FACTOR,
            operator=FeatureOperator.IDENTITY,
            source="momentum",
        ),
        FeatureNode(
            node_id="volatility",
            label="波动率",
            kind=FeatureKind.FACTOR,
            operator=FeatureOperator.IDENTITY,
            source="volatility_20d",
        ),
    )
    return spec.model_copy(
        update={
            "feature_graph": FeatureGraph(nodes=nodes, outputs=("momentum",)),
            "signal_rules": SignalRules(
                rules=(
                    SignalRule(
                        rule_id="top_score_buy",
                        feature_id="momentum",
                        comparator=SignalComparator.RANK_TOP,
                        threshold=0.5,
                        action=SignalAction.BUY,
                        rationale="动量前 50% 纳入目标仓位。",
                    ),
                )
            ),
        }
    )


def _manifest() -> ResearchRunManifest:
    spec = _price_only_spec()
    return ResearchRunManifest(
        run_id="RR-loadprobe306001",
        idempotency_key="load-probe-306-0001",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id=_RELEASE_ID,
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        factor_snapshots=(),
        parameters={"rebalance_frequency": "monthly"},
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
    )


def _month_end_decisions() -> list[date]:
    ends: dict[tuple[int, int], date] = {}
    for day in SESSIONS:
        ends[(day.year, day.month)] = day
    return [
        day
        for day in sorted(ends.values())
        if any(item > day for item in SESSIONS)
    ]


def _decision_at(day: date) -> datetime:
    return datetime.combine(day, time(15, 0), tzinfo=UTC)


async def _noop_snapshot_provider(snapshot_id: str) -> None:
    del snapshot_id


class _ScriptedProbe:
    """按脚本抛错的探针;记录每次调用的 (done, total)。"""

    def __init__(self, raise_on_call: int | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self._raise_on_call = raise_on_call

    async def __call__(self, done: int, total: int) -> None:
        self.calls.append((done, total))
        if self._raise_on_call is not None and len(self.calls) == self._raise_on_call:
            raise ResearchRunInterruptedError("模拟外部打断")


@pytest.mark.asyncio
async def test_chunk_probe_called_per_chunk_and_load_completes(tmp_path: Path) -> None:
    """存活路径:每分块边界一次 (done, total),全部 6 期照常加载。"""

    provider = await _build_release(tmp_path)
    probe = _ScriptedProbe()
    contexts = await build_decision_load_contexts(
        _manifest(),
        release_provider_factory=lambda _release_id: provider,
        snapshot_provider=_noop_snapshot_provider,
        chunk_probe=probe,
    )
    assert len(_month_end_decisions()) == 6
    assert probe.calls == [(0, 6), (4, 6)]  # ceil(6 / _DECISION_LOAD_CHUNK) = 2 次
    assert [item.context.decision_at for item in contexts] == [
        _decision_at(day) for day in _month_end_decisions()
    ]


@pytest.mark.asyncio
async def test_chunk_probe_interrupt_mid_load_propagates_without_263_marker(
    tmp_path: Path,
) -> None:
    """第 2 块边界被打断:异常原样透传,无 #263 数据失败标记。"""

    provider = await _build_release(tmp_path)
    probe = _ScriptedProbe(raise_on_call=2)
    with pytest.raises(ResearchRunInterruptedError, match="模拟外部打断") as exc_info:
        await build_decision_load_contexts(
            _manifest(),
            release_provider_factory=lambda _release_id: provider,
            snapshot_provider=_noop_snapshot_provider,
            chunk_probe=probe,
        )
    # 打断不是数据失败:#263 的逐期失败标记不得挂上(否则 error_summary 会
    # 被拼接成数据加载失败的样子,掩盖「外部打断」根因)。
    assert read_decision_load_context(exc_info.value) is None
    # 两次调用:第 1 次存活通过,第 2 次(第 2 块边界)记录后抛错。
    assert probe.calls == [(0, 6), (4, 6)]


@pytest.mark.asyncio
async def test_chunk_probe_interrupt_before_first_chunk(tmp_path: Path) -> None:
    """首块前被打断:零期次加载即中止。"""

    provider = await _build_release(tmp_path)
    probe = _ScriptedProbe(raise_on_call=1)
    with pytest.raises(ResearchRunInterruptedError):
        await build_decision_load_contexts(
            _manifest(),
            release_provider_factory=lambda _release_id: provider,
            snapshot_provider=_noop_snapshot_provider,
            chunk_probe=probe,
        )
    assert probe.calls == [(0, 6)]


@pytest.mark.asyncio
async def test_no_probe_keeps_load_behavior_identical(tmp_path: Path) -> None:
    """对照:无探针(默认 None)加载结果与有探针存活路径逐期一致。"""

    provider = await _build_release(tmp_path)
    without_probe = await build_decision_load_contexts(
        _manifest(),
        release_provider_factory=lambda _release_id: provider,
        snapshot_provider=_noop_snapshot_provider,
    )
    probe = _ScriptedProbe()
    with_probe = await build_decision_load_contexts(
        _manifest(),
        release_provider_factory=lambda _release_id: provider,
        snapshot_provider=_noop_snapshot_provider,
        chunk_probe=probe,
    )
    assert [item.context.business_date for item in without_probe] == [
        item.context.business_date for item in with_probe
    ]
    assert len(with_probe) == 6
