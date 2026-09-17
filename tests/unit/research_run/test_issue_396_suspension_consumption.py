"""issue #396:停复牌消费接线(合成停牌日单测)。

* ``SuspensionView``:全天停牌 PIT 查询(available_at 门控);
* universe 候选标注:决策日停牌 → ``suspended=True``(叠加静态近似);
* 执行接线:执行日全天停牌 → 指令拒单(fail-visible),持仓保留,
  shortfall 记入;信号截面剔除执行日停牌标的;
* ``PortfolioDecisionInput`` checksum 空集省略键(存量 run 零漂移)。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from finboard_backtest.portfolio import AssetLotInfo, CovarianceEstimate
from finboard_backtest.research_run import (
    FeatureValue,
    NormalizedSignal,
    ResearchOrderStatus,
    UniverseCandidate,
)
from finboard_backtest.research_run.frozen_loader import SuspensionView
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioDecisionInput,
    PortfolioPipelineAdapter,
)
from finboard_backtest.research_run.signal_engine import _spec_universe_candidates
from finboard_data.releases import ReleasedInstrument

pytestmark = pytest.mark.unit

_SHANGHAI = ZoneInfo("Asia/Shanghai")
SYMBOLS = ("A.SH", "B.SH", "C.SH")
_DECISION_AT = datetime(2026, 7, 20, 15, tzinfo=UTC)
_EXECUTION_AT = datetime(2026, 7, 21, 15, tzinfo=UTC)


class TestSuspensionView:
    def test_full_day_records_only(self) -> None:
        view = SuspensionView(
            [
                (
                    date(2026, 7, 21),
                    "A.SH",
                    datetime(2026, 7, 21, 9, 30, tzinfo=_SHANGHAI),
                ),
                (
                    date(2026, 7, 21),
                    "B.SH",
                    datetime(2026, 7, 21, 9, 30, tzinfo=_SHANGHAI),
                ),
            ]
        )
        assert view.suspended(
            date(2026, 7, 21), visible_at=_EXECUTION_AT
        ) == frozenset({"A.SH", "B.SH"})
        assert view.suspended(date(2026, 7, 22), visible_at=_EXECUTION_AT) == frozenset()

    def test_pit_gate_hides_not_yet_available(self) -> None:
        view = SuspensionView(
            [
                (
                    date(2026, 7, 21),
                    "A.SH",
                    datetime(2026, 7, 21, 9, 30, tzinfo=_SHANGHAI),
                ),
            ]
        )
        # 决策时点在 D-1 15:00,D 日 09:30 的停牌记录不可见(PIT=当日)。
        assert (
            view.suspended(date(2026, 7, 21), visible_at=_DECISION_AT) == frozenset()
        )
        # 执行日日终门控可见(与执行价同一放宽口径)。
        assert view.suspended(
            date(2026, 7, 21), visible_at=_EXECUTION_AT
        ) == frozenset({"A.SH"})


def _released_instrument(code: str, suspended_sessions: int = 0) -> ReleasedInstrument:
    from decimal import Decimal

    from finboard_data.releases import default_execution_metadata
    from finboard_shared.types import AssetClass, InstrumentType, Market

    return ReleasedInstrument(
        code=code,
        name=code,
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2026, 1, 2, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE, instrument_type=InstrumentType.STOCK
        ),
        artifact_path=f"bars/{code}_D1_qfq.parquet",
        artifact_checksum="b" * 64,
        artifact_size=1024,
        row_count=130,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 7, 21),
        expected_sessions=130,
        missing_sessions=0,
        suspended_sessions=suspended_sessions,
        anomaly_count=0,
        coverage_pct=Decimal("1"),
        category="full",
        ready=True,
    )


class TestUniverseAnnotation:
    def test_decision_day_suspension_marks_suspended(self) -> None:
        from finboard_backtest.research_run.frozen_loader import LoadedDecisionContext

        instruments = [_released_instrument("A.SH"), _released_instrument("B.SH")]
        context = LoadedDecisionContext(
            business_date=_DECISION_AT.date(),
            decision_at=_DECISION_AT,
            execution_at=_EXECUTION_AT,
            candidates=(),
            features=(),
            prices={},
            execution_prices={},
            lot_info={},
            input_artifact_ids=("release-v1",),
            decision_suspended=frozenset({"B.SH"}),
        )
        candidates = _spec_universe_candidates(
            instruments, context, features_by_source={}
        )
        by_symbol = {item.symbol: item for item in candidates}
        assert by_symbol["A.SH"].suspended is False
        assert by_symbol["B.SH"].suspended is True

    def test_static_approximation_still_applies(self) -> None:
        from finboard_backtest.research_run.frozen_loader import LoadedDecisionContext

        instruments = [
            _released_instrument("A.SH", suspended_sessions=3),
            _released_instrument("B.SH"),
        ]
        context = LoadedDecisionContext(
            business_date=_DECISION_AT.date(),
            decision_at=_DECISION_AT,
            execution_at=_EXECUTION_AT,
            candidates=(),
            features=(),
            prices={},
            execution_prices={},
            lot_info={},
            input_artifact_ids=("release-v1",),
        )
        candidates = _spec_universe_candidates(
            instruments, context, features_by_source={}
        )
        by_symbol = {item.symbol: item for item in candidates}
        assert by_symbol["A.SH"].suspended is True
        assert by_symbol["B.SH"].suspended is False


def _covariance() -> CovarianceEstimate:
    return CovarianceEstimate(
        matrix=np.diag([0.09, 0.01, 0.01]),
        tickers=list(SYMBOLS),
        shrinkage=0.0,
        n_observations=252,
    )


def _input(
    *,
    suspended: frozenset[str] = frozenset(),
) -> PortfolioDecisionInput:
    mark_prices = dict.fromkeys(SYMBOLS, 10.0)
    return PortfolioDecisionInput(
        business_date=_DECISION_AT.date(),
        decision_at=_DECISION_AT,
        execution_at=_EXECUTION_AT,
        candidates=tuple(
            UniverseCandidate(
                symbol=symbol,
                included=True,
                reasons=("通过冻结候选池规则",),
                asset_class="equity",
                market="a_share",
            )
            for symbol in SYMBOLS
        ),
        features=tuple(
            FeatureValue(
                symbol=symbol,
                feature_id="close",
                value=10.0,
                source_artifact_ids=("release-v1",),
                available_at=_DECISION_AT,
            )
            for symbol in SYMBOLS
        ),
        signals=tuple(
            NormalizedSignal(
                symbol=symbol,
                score=1.0,
                action="buy",
                rule_id="fixed-research-signal",
                factor_snapshot_id="factor-v1",
                rationale="冻结输入中的标准化多头信号",
            )
            for symbol in SYMBOLS
        ),
        prices=mark_prices,
        execution_prices=mark_prices,
        lot_info={symbol: AssetLotInfo(code=symbol, lot_size=100) for symbol in SYMBOLS},
        input_artifact_ids=("release-v1", "factor-v1"),
        covariance=_covariance(),
        sleeve_map=dict.fromkeys(SYMBOLS, "equity"),
        suspended_symbols=suspended,
    )


class TestExecutionReject:
    @pytest.mark.asyncio
    async def test_suspended_symbol_rejected_not_filled_at_stale_close(
        self, manifest_factory
    ) -> None:
        manifest = manifest_factory(
            run_id="RR-suspend-396",
            idempotency_key="idempotency-suspend-396",
        )
        adapter = PortfolioPipelineAdapter(
            strategy_kind="ma_cross",
            decision_inputs=(_input(suspended=frozenset({"B.SH"})),),
        )
        decisions = [
            decision async for decision in adapter.decisions(manifest)
        ]
        assert len(decisions) == 1
        bundle = decisions[0]
        orders = {order.symbol: order for order in bundle.orders}
        fills = {fill.symbol for fill in bundle.fills}
        # 停牌标的:拒单 + 无成交(不再按停牌前 close 静默成交)。
        assert orders["B.SH"].status is ResearchOrderStatus.REJECTED
        assert "停牌日拒绝成交" in (orders["B.SH"].reject_reason or "")
        assert "B.SH" not in fills
        # 非停牌标的照常成交(其余撮合语义零变化)。
        assert orders["A.SH"].status is ResearchOrderStatus.FILLED
        assert {"A.SH", "C.SH"} <= fills


class TestChecksumOmitEmpty:
    def test_empty_suspended_symbols_checksum_matches_legacy_payload(self) -> None:
        item = _input()
        # 空集省略键:checksum payload 与旧字段集逐字节一致。
        from finboard_backtest.research_run.contracts import stable_checksum

        covariance = item.covariance
        assert covariance is not None
        covariance_payload = {
            "tickers": covariance.tickers,
            "matrix": covariance.matrix.tolist(),
            "shrinkage": covariance.shrinkage,
            "n_observations": covariance.n_observations,
            "method": covariance.method,
        }
        legacy = stable_checksum(
            {
                "business_date": item.business_date,
                "decision_at": item.decision_at,
                "execution_at": item.execution_at,
                "candidates": item.candidates,
                "features": item.features,
                "signals": item.signals,
                "prices": item.prices,
                "execution_prices": item.execution_prices,
                "lot_info": item.lot_info,
                "input_artifact_ids": item.input_artifact_ids,
                "covariance": covariance_payload,
                "sleeve_map": item.sleeve_map,
                "disabled_symbols": sorted(item.disabled_symbols),
                "betas": item.betas,
                "realized_volatility": item.realized_volatility,
                "atr": item.atr,
                "fill_ratio_by_symbol": item.fill_ratio_by_symbol,
                "rejected_symbols": sorted(item.rejected_symbols),
            }
        )
        assert item.checksum == legacy

    def test_non_empty_suspended_symbols_changes_checksum(self) -> None:
        base = _input()
        marked = replace(base, suspended_symbols=frozenset({"B.SH"}))
        assert base.checksum != marked.checksum
