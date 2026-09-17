"""factor_screen 指标计算(issue #217)。

用手工构造的两期决策输入验证 IC / IR / 分层收益 / 换手率 / 相关性矩阵
的数值正确性(因子与 forward return 完美单调 → IC=1;对照特征完全相同 /
相反 → 相关 ±1;期末交换两标的因子值 → 已知换手)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from finboard_backtest.portfolio.contracts import AssetLotInfo
from finboard_backtest.research_run.contracts import (
    FeatureValue,
    FrozenArtifactRef,
    NormalizedSignal,
    ResearchActorType,
    ResearchRunManifest,
    UniverseCandidate,
    stable_checksum,
)
from finboard_backtest.research_run.factor_screen import build_factor_screen
from finboard_backtest.research_run.portfolio_pipeline import PortfolioDecisionInput
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_data.releases import ReleaseDatasetKind
from finboard_shared.types import BarPeriod

_T1 = datetime(2024, 6, 3, 7, 0, tzinfo=UTC)
_T2 = datetime(2024, 7, 1, 7, 0, tzinfo=UTC)
_SYMBOLS = [f"S{i}" for i in range(10)]
# 期 1 因子值 = 序号;期 2 交换 S0/S9(制造已知换手与第二条 IC 样本)
_FACTOR1 = {s: float(i) for i, s in enumerate(_SYMBOLS)}
_FACTOR2 = dict(_FACTOR1)
_FACTOR2["S0"], _FACTOR2["S9"] = _FACTOR2["S9"], _FACTOR2["S0"]

# 价格:t1=100;t2 按期 1 因子涨;期末再按期 2 因子涨(每期 IC=1)
_PRICES1 = dict.fromkeys(_SYMBOLS, 100.0)
_PRICES2 = {s: 100.0 * (1 + 0.01 * _FACTOR1[s]) for s in _SYMBOLS}
_END = {s: _PRICES2[s] * (1 + 0.01 * _FACTOR2[s]) for s in _SYMBOLS}


class _FakeRelease:
    dataset_kind = ReleaseDatasetKind.BARS
    period = BarPeriod.D1
    start_date = date(2024, 1, 1)
    end_date = date(2024, 8, 31)
    adjustment = "qfq"


class _FakeProvider:
    release = _FakeRelease()

    async def fetch_point_in_time_bars(self, symbol: Any, *args: Any, **kwargs: Any):
        return [SimpleNamespace(bar=SimpleNamespace(close=_END[str(symbol.code)]))]


def _manifest() -> ResearchRunManifest:
    spec = build_strategy_template(
        "multi_factor", strategy_id="screen_test", dataset_release_ids=("rel-1",)
    )
    return ResearchRunManifest(
        run_id="RR-screen-test-00000001",
        idempotency_key="screen-test",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id="rel-1",
                version="2026-01-01",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        code_version="t",
        initial_capital=Decimal("100000"),
        requested_by="unit",
        actor_type=ResearchActorType.HUMAN,
    )


def _decision(
    decision_at: datetime,
    factor: dict[str, float],
    prices: dict[str, float],
    user_feature: str = "u_agent_alpha",
) -> PortfolioDecisionInput:
    features = [
        # user 因子 + 两个对照:momentum 完全同向(相关 +1),pb 完全反向(-1)
        FeatureValue(s, user_feature, v, ("feature-1",), decision_at)
        for s, v in factor.items()
    ] + [
        FeatureValue(s, "momentum", v, ("feature-1",), decision_at)
        for s, v in factor.items()
    ] + [
        FeatureValue(s, "pb", -v, ("feature-1",), decision_at)
        for s, v in factor.items()
    ]
    return PortfolioDecisionInput(
        business_date=decision_at.date(),
        decision_at=decision_at,
        # 成交须晚于决策(禁止同 Bar 成交)
        execution_at=decision_at.replace(hour=8),
        candidates=tuple(
            UniverseCandidate(s, True, ("ok",), "stock", "a_share") for s in _SYMBOLS
        ),
        features=tuple(features),
        signals=tuple(
            NormalizedSignal(s, factor[s], "buy", "rule-1", rationale="unit")
            for s in _SYMBOLS
        ),
        prices=dict(prices),
        execution_prices=dict(prices),
        lot_info={s: AssetLotInfo(code=s) for s in _SYMBOLS},
        input_artifact_ids=("feature-1",),
    )


async def test_factor_screen_metrics() -> None:
    inputs = (
        _decision(_T1, _FACTOR1, _PRICES1),
        _decision(_T2, _FACTOR2, _PRICES2),
    )
    screen = await build_factor_screen(_manifest(), inputs, lambda _id: _FakeProvider())
    assert screen is not None
    entry = screen["factors"]["u_agent_alpha"]
    assert entry["origin"] == "user_defined"
    assert entry["n_periods"] == 2

    # 每期因子与 forward return 完美单调 → IC=1;std=0 → IR 按约定记 0
    assert entry["rank_ic"] == pytest.approx(1.0)
    assert entry["ic_sample_count"] == 2
    assert entry["rank_ic_ir"] == pytest.approx(0.0)

    # 分层:Q1(低值)平均 0.5%,Q5(高值)平均 8.5%,单调
    quantiles = {q["quantile"]: q["average_return"] for q in entry["quantile_returns"]}
    assert quantiles[1] == pytest.approx(0.005)
    assert quantiles[5] == pytest.approx(0.085)
    assert all(quantiles[i] < quantiles[i + 1] for i in range(1, 5))

    # 换手:期1 Q5={S8,S9},期2 Q5={S8,S0} → 1/3
    assert entry["average_turnover"] == pytest.approx(1 / 3)

    # 相关性:momentum +1,pb -1
    assert entry["correlation"]["momentum"] == pytest.approx(1.0)
    assert entry["correlation"]["pb"] == pytest.approx(-1.0)
    assert set(screen["correlation_baselines"]) == {"momentum", "pb"}
    assert screen["method"].startswith("spearman")


async def test_factor_screen_none_without_user_factor() -> None:
    inputs = (_decision(_T1, _FACTOR1, _PRICES1, user_feature="momentum"),)
    screen = await build_factor_screen(
        _manifest(), inputs, lambda _id: _FakeProvider()
    )
    assert screen is None


async def test_factor_screen_single_period_ir_none() -> None:
    inputs = (_decision(_T1, _FACTOR1, _PRICES1),)
    screen = await build_factor_screen(_manifest(), inputs, lambda _id: _FakeProvider())
    assert screen is not None
    entry = screen["factors"]["u_agent_alpha"]
    assert entry["n_periods"] == 1
    # 单期:IC 有值(期末价含期 2 交换,不指定具体值),IR / 换手无样本
    assert entry["rank_ic"] is not None
    assert entry["rank_ic_ir"] is None
    assert entry["average_turnover"] is None
