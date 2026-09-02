"""multi_factor 信号引擎单元测试(issue #170)。

用 stub 冻结快照 / 发布验证 FeatureGraph 算子、SignalRules comparator、
available_at 过滤与候选池过滤;不依赖 PostgreSQL 或 Parquet 文件。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from finboard_backtest.research_run.contracts import (
    DecisionBundle,
    FeatureValue,
    FrozenArtifactRef,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_run.failure_context import (
    read_decision_load_context,
)
from finboard_backtest.research_run.frozen_loader import (
    FrozenInputLoader,
    LoadedDecisionContext,
)
from finboard_backtest.research_run.signal_engine import (
    build_decision_inputs,
    build_decision_load_contexts,
    build_normalized_signals,
    evaluate_feature_graph,
    evaluate_signal_rules,
    single_shot_snapshot_gate_error,
)
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_backtest.strategy_spec.contracts import ResearchStrategySpec
from finboard_data.factor_lab import FeatureObservation
from finboard_data.releases import ReleaseDatasetKind
from finboard_shared.types import AssetClass, Market

# ---- stubs ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StubBar:
    close: Decimal
    timestamp: datetime | None = None


@dataclass(frozen=True, slots=True)
class _StubPointInTimeBar:
    bar: _StubBar
    available_at: datetime


@dataclass(frozen=True, slots=True)
class _StubPointInTimePrice:
    timestamp: datetime
    close: Decimal
    available_at: datetime


@dataclass(frozen=True, slots=True)
class _StubExecution:
    lot_size: Decimal = Decimal("100")
    price_tick: Decimal = Decimal("0.01")
    settlement_days: int = 1
    multiplier: Decimal = Decimal("1")
    margin_rate: Decimal | None = None
    stamp_tax_rate: Decimal = Decimal("0.001")
    commission_rate: Decimal = Decimal("0.0003")
    commission_min: Decimal = Decimal("5")
    trading_calendar: str = "SSE"
    allows_short: bool = False


@dataclass(frozen=True, slots=True)
class _StubInstrument:
    code: str
    # 注意:名称不得含 "ST" 子串(大写后),否则被 ST 过滤排除(issue #213)。
    name: str = "sample"
    name_history: tuple[tuple[str, date, date | None], ...] = ()
    market: Market = Market.A_SHARE
    asset_class: AssetClass = AssetClass.EQUITY
    ready: bool = True
    execution: _StubExecution = field(default_factory=_StubExecution)
    list_date: date | None = date(2020, 1, 1)
    delist_date: date | None = None
    coverage_pct: Decimal = Decimal("1.0")
    suspended_sessions: int = 0


@dataclass
class _StubRelease:
    release_id: str
    instruments: tuple[_StubInstrument, ...]
    period: object = "d1"
    adjustment: str = "qfq"
    start_date: date = date(2024, 1, 1)
    end_date: date = date(2024, 12, 31)
    source: str = "stub"
    version: str = "v1"
    release_checksum: str = "c" * 64
    is_usable: bool = True
    dataset_kind: object = ReleaseDatasetKind.BARS


@dataclass
class _StubProvider:
    """按 (symbol, 请求结束日) 返回多根 PIT bars 的 stub 发布 provider。"""

    release: _StubRelease
    closes_by_symbol: dict[str, dict[date, Decimal]]

    async def fetch_point_in_time_bars(
        self,
        symbol: object,
        period: object,
        start: date,
        end: date,
        *,
        decision_at: datetime,
        adjust: str = "qfq",
    ) -> list[_StubPointInTimeBar]:
        """返回请求结束日及之前的全部 PIT bars(时间升序)。

        与真实 provider 的 PIT 语义一致:停牌日返回最近历史 bar;
        交易日历由 bar 日期推断。
        """
        del period, start, decision_at, adjust
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        if not by_date:
            return []
        return [
            _StubPointInTimeBar(
                _StubBar(
                    close,
                    timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                ),
                datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            )
            for day, close in sorted(by_date.items())
            if day <= end
        ]

    async def fetch_point_in_time_prices(
        self,
        symbol: object,
        period: object,
        start: date,
        end: date,
        *,
        decision_at: datetime,
        adjust: str = "qfq",
    ) -> list[_StubPointInTimePrice]:
        """价格特征所需的轻量 PIT 收盘价视图(多期回放每期重算 features 用)。"""
        del period, start, adjust
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        if not by_date:
            return []
        return [
            _StubPointInTimePrice(
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                close=close,
                available_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            )
            for day, close in sorted(by_date.items())
            if day <= end and datetime.combine(day, datetime.min.time(), tzinfo=UTC) <= decision_at
        ]

    async def fetch_bars(
        self,
        symbol: object,
        period: object,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[_StubBar]:
        """非 PIT 全量 bars(交易日历推断用),返回带 timestamp 的 Bar 形状。"""
        del period, start, adjust
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        if not by_date:
            return []
        return [
            _StubBar(
                close,
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            )
            for day, close in sorted(by_date.items())
            if day <= end
        ]


@dataclass
class _StubSnapshot:
    snapshot_id: str
    decision_at: datetime
    observations: tuple[FeatureObservation, ...]


def _obs(
    symbol: str,
    feature_name: str,
    value: float,
    *,
    available_at: datetime | None = None,
) -> FeatureObservation:
    at = available_at or datetime(2024, 2, 29, tzinfo=UTC)
    return FeatureObservation(
        symbol=symbol,
        feature_name=feature_name,
        value=value,
        observed_at=at,
        available_at=at,
        source="release-v1",
        source_version="v1",
    )


def _features(
    pairs: list[tuple[str, str, float]],
) -> tuple[FeatureValue, ...]:
    return tuple(
        FeatureValue(
            symbol=symbol,
            feature_id=name,
            value=value,
            source_artifact_ids=("factor-v1",),
            available_at=datetime(2024, 2, 29, tzinfo=UTC),
        )
        for symbol, name, value in pairs
    )


def _spec() -> ResearchStrategySpec:
    return build_strategy_template(
        "multi_factor",
        strategy_id="signal_engine_test",
        dataset_release_ids=("release-v1",),
    )


def _manifest(spec: ResearchStrategySpec) -> ResearchRunManifest:
    return ResearchRunManifest(
        run_id="RR-signalenginetest0001",
        idempotency_key="signal-engine-test-0001",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id="release-v1",
                version="2026-01-01",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        factor_snapshots=(
            FrozenArtifactRef(
                artifact_id="factor-v1",
                version="v1",
                checksum="b" * 64,
                capabilities=("factor:momentum",),
            ),
        ),
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
    )


def _features_by_source(
    features: tuple[FeatureValue, ...],
) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for feature in features:
        value = feature.value
        if value is None:
            continue
        out.setdefault(feature.feature_id, {})[feature.symbol] = value
    return out


# ---- FeatureGraph 算子 ------------------------------------------------------


class TestFeatureGraphEvaluation:
    def test_multi_factor_template_pipeline(self) -> None:
        """multi_factor 模板:pb 排名 → 负向;动量 / 波动率 → 负向;加权复合。"""
        spec = _spec()
        features = _features(
            [
                ("000001.SZ", "pb", 0.8),
                ("000002.SZ", "pb", 1.2),
                ("000003.SZ", "pb", 0.5),
                ("000001.SZ", "momentum", 0.1),
                ("000002.SZ", "momentum", 0.2),
                ("000003.SZ", "momentum", 0.05),
                ("000001.SZ", "volatility_20d", 0.2),
                ("000002.SZ", "volatility_20d", 0.5),
                ("000003.SZ", "volatility_20d", 0.1),
            ]
        )
        _, finals = evaluate_feature_graph(
            spec,
            features_by_source=_features_by_source(features),
            prices={"000001.SZ": 10.0, "000002.SZ": 10.0, "000003.SZ": 10.0},
        )
        composite = finals["composite"]
        # pb 最低(0.5)的 000003.SZ 价值得分最高;加权后综合分最高。
        assert composite["000003.SZ"] > composite["000001.SZ"]
        assert composite["000001.SZ"] > composite["000002.SZ"]

    def test_sma_series_from_price_series(self) -> None:
        """identity(close) 输出价格序列,sma 输出全序列,最终值 = 窗口均值。"""
        from finboard_backtest.strategy_spec.contracts import (
            FeatureGraph,
            FeatureKind,
            FeatureNode,
            FeatureOperator,
        )

        spec = _spec()
        modified = spec.model_copy(
            update={
                "feature_graph": FeatureGraph(
                    nodes=(
                        FeatureNode(
                            node_id="close",
                            label="收盘价",
                            kind=FeatureKind.MARKET_INPUT,
                            operator=FeatureOperator.IDENTITY,
                            source="close",
                        ),
                        FeatureNode(
                            node_id="ma5",
                            label="5 日均线",
                            kind=FeatureKind.TRANSFORM,
                            operator=FeatureOperator.SIMPLE_MOVING_AVERAGE,
                            inputs=("close",),
                            window=5,
                        ),
                    ),
                    outputs=("ma5",),
                ),
            }
        )
        series, finals = evaluate_feature_graph(
            modified,
            features_by_source={},
            prices={"000001.SZ": 11.0},
            price_series={"000001.SZ": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]},
        )
        # sma5 全序列:最后值 = (2+3+4+5+6)/5 = 4.0。
        assert series["ma5"]["000001.SZ"][-1] == pytest.approx(4.0)
        assert finals["ma5"]["000001.SZ"] == pytest.approx(4.0)

    def test_unknown_market_input_raises(self) -> None:
        """identity 引用无数据源(open)时 fail-closed。"""
        from finboard_backtest.strategy_spec.contracts import (
            FeatureGraph,
            FeatureKind,
            FeatureNode,
            FeatureOperator,
        )

        spec = _spec()
        modified = spec.model_copy(
            update={
                "feature_graph": FeatureGraph(
                    nodes=(
                        FeatureNode(
                            node_id="x",
                            label="x",
                            kind=FeatureKind.MARKET_INPUT,
                            operator=FeatureOperator.IDENTITY,
                            source="open",
                        ),
                    ),
                    outputs=("x",),
                ),
            }
        )
        with pytest.raises(ValueError, match="缺少数据源"):
            evaluate_feature_graph(
                modified,
                features_by_source={},
                prices={},
            )


# ---- SignalRules ------------------------------------------------------------


class TestSignalRulesEvaluation:
    def test_rank_top_buy_and_rank_bottom_sell(self) -> None:
        """multi_factor 模板:复合得分前 20% BUY、后 50% SELL。"""
        spec = _spec()
        included = frozenset({"000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ", "000005.SZ"})
        features = _features(
            [
                ("000001.SZ", "pb", 0.8),
                ("000002.SZ", "pb", 1.2),
                ("000003.SZ", "pb", 0.5),
                ("000004.SZ", "pb", 1.5),
                ("000005.SZ", "pb", 2.0),
                ("000001.SZ", "momentum", 0.1),
                ("000002.SZ", "momentum", 0.2),
                ("000003.SZ", "momentum", 0.05),
                ("000004.SZ", "momentum", 0.3),
                ("000005.SZ", "momentum", 0.4),
                ("000001.SZ", "volatility_20d", 0.2),
                ("000002.SZ", "volatility_20d", 0.5),
                ("000003.SZ", "volatility_20d", 0.1),
                ("000004.SZ", "volatility_20d", 0.3),
                ("000005.SZ", "volatility_20d", 0.25),
            ]
        )
        signals = build_normalized_signals(
            spec,
            features=features,
            prices=dict.fromkeys(included, 10.0),
            included_symbols=included,
            factor_snapshot_id="factor-v1",
        )
        buy_signals = [s for s in signals if s.action == "buy"]
        sell_signals = [s for s in signals if s.action == "sell"]
        # 5 标的:前 20% 恰好 1 个 BUY,后 50% 恰好 2 个 SELL,中间 2 个无信号。
        assert len(buy_signals) == 1
        assert len(sell_signals) == 2
        assert buy_signals[0].score > 0
        assert all(s.score < 0 for s in sell_signals)
        assert buy_signals[0].rule_id == "top_score_buy"
        assert all(s.rule_id == "bottom_score_sell" for s in sell_signals)
        assert all(s.factor_snapshot_id == "factor-v1" for s in signals)

    def test_non_included_symbols_rejected(self) -> None:
        """非 included 候选不产出信号(信号 ⊆ included 候选)。"""
        spec = _spec()
        included = frozenset({"000001.SZ"})
        features = _features(
            [
                ("000001.SZ", "pb", 0.8),
                ("000002.SZ", "pb", 1.2),
                ("000001.SZ", "momentum", 0.1),
                ("000002.SZ", "momentum", 0.2),
                ("000001.SZ", "volatility_20d", 0.2),
                ("000002.SZ", "volatility_20d", 0.5),
            ]
        )
        signals = build_normalized_signals(
            spec,
            features=features,
            prices={"000001.SZ": 10.0},
            included_symbols=included,
            factor_snapshot_id="factor-v1",
        )
        assert {s.symbol for s in signals} <= included

    def test_conflict_highest_priority_wins(self) -> None:
        """同组冲突:highest_priority 保留 priority 最高规则。"""
        from finboard_backtest.strategy_spec.contracts import (
            SignalAction,
            SignalComparator,
            SignalRule,
            SignalRules,
        )

        spec = _spec()
        modified = spec.model_copy(
            update={
                "signal_rules": SignalRules(
                    rules=(
                        SignalRule(
                            rule_id="high",
                            feature_id="composite",
                            comparator=SignalComparator.GREATER_THAN,
                            threshold=0,
                            action=SignalAction.BUY,
                            priority=100,
                            rationale="高优先级买",
                        ),
                        SignalRule(
                            rule_id="low",
                            feature_id="composite",
                            comparator=SignalComparator.GREATER_THAN,
                            threshold=0,
                            action=SignalAction.SELL,
                            priority=10,
                            rationale="低优先级卖",
                        ),
                    ),
                    conflict_policy="highest_priority",  # type: ignore[arg-type]
                ),
            }
        )
        resolved = evaluate_signal_rules(
            modified,
            node_finals={"composite": {"000001.SZ": 1.0}},
            node_series={},
            included_symbols=frozenset({"000001.SZ"}),
            factor_snapshot_id=None,
        )
        assert len(resolved) == 1
        assert resolved[0].rule_id == "high"
        assert resolved[0].action == "buy"

    def test_conflict_neutralize_drops_buy_sell(self) -> None:
        """同组冲突:neutralize 策略下正负并存整组中性化。"""
        from finboard_backtest.strategy_spec.contracts import (
            SignalAction,
            SignalComparator,
            SignalRule,
            SignalRules,
        )

        spec = _spec()
        modified = spec.model_copy(
            update={
                "signal_rules": SignalRules(
                    rules=(
                        SignalRule(
                            rule_id="buy_rule",
                            feature_id="composite",
                            comparator=SignalComparator.GREATER_THAN,
                            threshold=0,
                            action=SignalAction.BUY,
                            priority=100,
                            rationale="买",
                        ),
                        SignalRule(
                            rule_id="sell_rule",
                            feature_id="composite",
                            comparator=SignalComparator.GREATER_THAN,
                            threshold=0,
                            action=SignalAction.SELL,
                            priority=10,
                            rationale="卖",
                        ),
                    ),
                    conflict_policy="neutralize",  # type: ignore[arg-type]
                ),
            }
        )
        # 同一标的、同一条件同时命中 buy 与 sell → 整组中性化,不产出信号。
        resolved = evaluate_signal_rules(
            modified,
            node_finals={"composite": {"000001.SZ": 1.0}},
            node_series={},
            included_symbols=frozenset({"000001.SZ"}),
            factor_snapshot_id=None,
        )
        assert resolved == ()

    def test_cross_above_and_below(self) -> None:
        """cross_above / cross_below:序列最后两点判断穿越。"""
        from finboard_backtest.strategy_spec.contracts import (
            SignalAction,
            SignalComparator,
            SignalRule,
            SignalRules,
        )

        spec = _spec()
        modified = spec.model_copy(
            update={
                "feature_graph": _spec().feature_graph,
                "signal_rules": SignalRules(
                    rules=(
                        SignalRule(
                            rule_id="golden",
                            feature_id="short",
                            reference_feature_id="long",
                            comparator=SignalComparator.CROSS_ABOVE,
                            action=SignalAction.BUY,
                            rationale="上穿",
                        ),
                        SignalRule(
                            rule_id="death",
                            feature_id="short",
                            reference_feature_id="long",
                            comparator=SignalComparator.CROSS_BELOW,
                            action=SignalAction.SELL,
                            priority=10,
                            rationale="下穿",
                        ),
                    ),
                ),
            }
        )
        # 上穿:前一日 1.0<=2.0,当日 5.0>4.0。
        above = evaluate_signal_rules(
            modified,
            node_finals={"short": {"A": 5.0}, "long": {"A": 4.0}},
            node_series={
                "short": {"A": [1.0, 5.0]},
                "long": {"A": [2.0, 4.0]},
            },
            included_symbols=frozenset({"A"}),
            factor_snapshot_id=None,
        )
        assert [s.rule_id for s in above] == ["golden"]
        # 下穿:前一日 5.0>4.0,当日 1.0<2.0。
        below = evaluate_signal_rules(
            modified,
            node_finals={"short": {"A": 1.0}, "long": {"A": 2.0}},
            node_series={
                "short": {"A": [5.0, 1.0]},
                "long": {"A": [4.0, 2.0]},
            },
            included_symbols=frozenset({"A"}),
            factor_snapshot_id=None,
        )
        assert [s.rule_id for s in below] == ["death"]


# ---- build_decision_inputs --------------------------------------------------


@pytest.mark.asyncio
class TestBuildDecisionInputs:
    def _provider(
        self,
        instruments: tuple[_StubInstrument, ...],
        closes_by_symbol: dict[str, dict[date, Decimal]],
    ) -> _StubProvider:
        return _StubProvider(
            release=_StubRelease("release-v1", instruments),
            closes_by_symbol=closes_by_symbol,
        )

    def _snapshots(self, decision_at: datetime) -> dict[str, _StubSnapshot]:
        snapshot = _StubSnapshot(
            snapshot_id="factor-v1",
            decision_at=decision_at,
            observations=(
                _obs("000001.SZ", "pb", 0.8),
                _obs("000002.SZ", "pb", 1.2),
                _obs("000001.SZ", "momentum", 0.1),
                _obs("000002.SZ", "momentum", 0.2),
                _obs("000001.SZ", "volatility_20d", 0.2),
                _obs("000002.SZ", "volatility_20d", 0.5),
            ),
        )
        return {"factor-v1": snapshot}

    def _factories(
        self,
        provider: _StubProvider,
        snapshots: dict[str, _StubSnapshot],
    ) -> tuple[object, object]:
        def release_factory(release_id: str) -> _StubProvider:
            assert release_id == "release-v1"
            return provider

        async def snapshot_provider(
            snapshot_id: str,
        ) -> _StubSnapshot | None:
            return snapshots.get(snapshot_id)

        return release_factory, snapshot_provider

    async def test_builds_single_decision_input(self) -> None:
        """真实工厂路径:冻结快照决策日 → 机械字段 + 信号 → PortfolioDecisionInput。"""
        decision_at = datetime(2024, 3, 1, 15, tzinfo=UTC)
        closes = {
            symbol: {
                date(2024, 2, 27): Decimal("9.6"),
                date(2024, 2, 28): Decimal("9.8"),
                date(2024, 3, 1): Decimal("10.0"),
                date(2024, 3, 4): Decimal("10.5"),
            }
            for symbol in ("000001.SZ", "000002.SZ")
        }
        provider = self._provider(
            (
                _StubInstrument(code="000001.SZ"),
                _StubInstrument(code="000002.SZ"),
            ),
            closes,
        )
        release_factory, snapshot_provider = self._factories(provider, self._snapshots(decision_at))

        inputs = await build_decision_inputs(
            _manifest(_spec()),
            release_provider_factory=release_factory,  # type: ignore[arg-type]
            snapshot_provider=snapshot_provider,  # type: ignore[arg-type]
        )

        assert len(inputs) == 1
        item = inputs[0]
        assert item.business_date == date(2024, 3, 1)
        assert item.decision_at == decision_at
        # execution_at = 决策后最近交易日(2024-03-04 有行情)。
        assert item.execution_at.date() == date(2024, 3, 4)
        included = {c.symbol for c in item.candidates if c.included}
        assert included
        # 信号 ⊆ included 且价格 / 成交价 / 执行元数据齐备。
        assert {s.symbol for s in item.signals} <= included
        assert {s.symbol for s in item.signals} <= set(item.prices)
        assert {s.symbol for s in item.signals} <= set(item.execution_prices)
        assert {s.symbol for s in item.signals} <= set(item.lot_info)
        assert all(s.factor_snapshot_id == "factor-v1" for s in item.signals)
        assert item.input_artifact_ids == ("release-v1", "factor-v1")
        # 协方差覆盖全部信号标的(风险贡献硬约束需可用协方差)。
        assert item.covariance is not None
        assert set(item.covariance.tickers) >= {s.symbol for s in item.signals}

    async def test_universe_selection_limit_applied(self) -> None:
        """selection_limit 生效:候选被过滤到限额内。"""
        decision_at = datetime(2024, 3, 1, 15, tzinfo=UTC)
        instruments = tuple(_StubInstrument(code=f"00000{i}.SZ") for i in range(1, 6))
        closes = {
            inst.code: {
                date(2024, 2, 27): Decimal("9.6"),
                date(2024, 2, 28): Decimal("9.8"),
                date(2024, 3, 1): Decimal("10.0"),
                date(2024, 3, 4): Decimal("10.5"),
            }
            for inst in instruments
        }
        provider = self._provider(instruments, closes)
        snapshots = {
            "factor-v1": _StubSnapshot(
                snapshot_id="factor-v1",
                decision_at=decision_at,
                observations=tuple(
                    _obs(inst.code, "momentum", float(index + 1))
                    for index, inst in enumerate(instruments)
                )
                + tuple(_obs(inst.code, "pb", 1.0) for inst in instruments)
                + tuple(_obs(inst.code, "volatility_20d", 0.3) for inst in instruments),
            ),
        }
        release_factory, snapshot_provider = self._factories(provider, snapshots)
        # ranking 用目录内因子 momentum;selection_limit 收窄验证过滤生效。
        from finboard_backtest.strategy_spec.contracts import (
            RankingDirection,
            UniverseRanking,
        )

        modified = _spec().model_copy(
            update={
                "universe": _spec().universe.model_copy(
                    update={
                        "selection_limit": 2,
                        "ranking": UniverseRanking(
                            field="momentum",
                            direction=RankingDirection.TOP,
                        ),
                        "required_data_fields": ("price",),
                    }
                )
            }
        )

        inputs = await build_decision_inputs(
            _manifest(modified),
            release_provider_factory=release_factory,  # type: ignore[arg-type]
            snapshot_provider=snapshot_provider,  # type: ignore[arg-type]
        )
        included = {c.symbol for c in inputs[0].candidates if c.included}
        assert len(included) == 2

    async def test_universe_market_cap_filter_applies_from_snapshot_features(self) -> None:
        """issue #213:min_market_cap 用冻结快照的 market_cap 特征观测过滤候选。"""
        decision_at = datetime(2024, 3, 1, 15, tzinfo=UTC)
        instruments = (
            _StubInstrument(code="000001.SZ"),  # 小市值 50 亿
            _StubInstrument(code="000002.SZ"),  # 大市值 2000 亿
        )
        closes = {
            inst.code: {
                date(2024, 2, 27): Decimal("9.6"),
                date(2024, 2, 28): Decimal("9.8"),
                date(2024, 3, 1): Decimal("10.0"),
                date(2024, 3, 4): Decimal("10.5"),
            }
            for inst in instruments
        }
        provider = self._provider(instruments, closes)
        snapshots = {
            "factor-v1": _StubSnapshot(
                snapshot_id="factor-v1",
                decision_at=decision_at,
                observations=tuple(
                    _obs(inst.code, name, value)
                    for inst in instruments
                    for name, value in (
                        ("pb", 1.0),
                        ("momentum", 0.1),
                        ("volatility_20d", 0.3),
                        (
                            "market_cap",
                            5e9 if inst.code == "000001.SZ" else 2e11,
                        ),
                    )
                ),
            ),
        }
        release_factory, snapshot_provider = self._factories(provider, snapshots)
        modified = _spec().model_copy(
            update={
                "universe": _spec().universe.model_copy(
                    update={"min_market_cap": 1e10}
                )
            }
        )

        inputs = await build_decision_inputs(
            _manifest(modified),
            release_provider_factory=release_factory,  # type: ignore[arg-type]
            snapshot_provider=snapshot_provider,  # type: ignore[arg-type]
        )
        included = {c.symbol for c in inputs[0].candidates if c.included}
        assert included == {"000002.SZ"}
        reasons = {
            c.symbol: c.reasons
            for c in inputs[0].candidates
            if not c.included
        }
        assert reasons["000001.SZ"] == ("market_cap_below_minimum",)

    async def test_universe_st_excluded_via_name_history_pit(self) -> None:
        """issue #213:exclude_st 按决策日名称历史 PIT 判定,当前名称不作数。"""
        decision_at = datetime(2024, 3, 1, 15, tzinfo=UTC)
        instruments = (
            # 当前名称是 ST,但决策日名称历史为正常 → 不排除(PIT 正确性)。
            _StubInstrument(
                code="000001.SZ",
                name="ST样本",
                name_history=(
                    ("样本股份", date(2020, 1, 1), None),
                ),
            ),
            # 决策日历史名称为 ST → 排除。
            _StubInstrument(
                code="000002.SZ",
                name="ST问题",
                name_history=(
                    ("正常股份", date(2020, 1, 1), date(2024, 1, 1)),
                    ("ST问题股份", date(2024, 1, 1), None),
                ),
            ),
        )
        closes = {
            inst.code: {
                date(2024, 2, 27): Decimal("9.6"),
                date(2024, 2, 28): Decimal("9.8"),
                date(2024, 3, 1): Decimal("10.0"),
                date(2024, 3, 4): Decimal("10.5"),
            }
            for inst in instruments
        }
        provider = self._provider(instruments, closes)
        release_factory, snapshot_provider = self._factories(
            provider, self._snapshots(decision_at)
        )

        inputs = await build_decision_inputs(
            _manifest(_spec()),
            release_provider_factory=release_factory,  # type: ignore[arg-type]
            snapshot_provider=snapshot_provider,  # type: ignore[arg-type]
        )
        included = {c.symbol for c in inputs[0].candidates if c.included}
        assert included == {"000001.SZ"}
        reasons = {
            c.symbol: c.reasons
            for c in inputs[0].candidates
            if not c.included
        }
        assert reasons["000002.SZ"] == ("st_security",)

    async def test_missing_snapshot_fails_closed(self) -> None:
        """冻结快照缺失时 fail-closed(不会静默跑出空信号)。"""
        provider = self._provider(
            (_StubInstrument(code="000001.SZ"),),
            {"000001.SZ": {date(2024, 3, 1): Decimal("10.0")}},
        )

        def release_factory(release_id: str) -> _StubProvider:
            return provider

        async def snapshot_provider(snapshot_id: str) -> None:
            return None

        with pytest.raises(ValueError, match="因子快照缺失"):
            await build_decision_inputs(
                _manifest(_spec()),
                release_provider_factory=release_factory,  # type: ignore[arg-type]
                snapshot_provider=snapshot_provider,
            )


# ---- 多期再平衡(issue #183) ---------------------------------------------------


def _calendar(start: date, end: date) -> list[date]:
    """周内连续交易日 stub 日历(跳过周末)。"""
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _multi_provider(
    start: date,
    end: date,
    symbols: tuple[str, ...] = ("000001.SZ", "000002.SZ", "000003.SZ"),
) -> _StubProvider:
    """跨多月的价格序列(每标的固定漂移,保证协方差/信号可算)。"""
    days = _calendar(start, end)
    closes: dict[str, dict[date, Decimal]] = {}
    for symbol in symbols:
        series: dict[date, Decimal] = {}
        base = Decimal("10.0")
        for index, day in enumerate(days):
            drift = Decimal("1.001") ** index
            if symbol == "000001.SZ":
                drift *= Decimal("1.02") if index % 21 == 0 else Decimal("1.0")
            series[day] = base * drift
        closes[symbol] = series
    return _StubProvider(
        release=_StubRelease(
            "release-multi",
            tuple(_StubInstrument(code=code) for code in symbols),
            start_date=start,
            end_date=end,
        ),
        closes_by_symbol=closes,
    )


def _multi_manifest(
    spec: ResearchStrategySpec,
    *,
    frequency: str = "monthly",
) -> ResearchRunManifest:
    return ResearchRunManifest(
        run_id="RR-multiperiod-test0001",
        idempotency_key="multi-period-0001",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id="release-multi",
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        parameters={"rebalance_frequency": frequency},
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
    )


def _price_only_spec() -> ResearchStrategySpec:
    """价格因子专用规格(仅 momentum/volatility,无需冻结基本面快照)。"""
    from finboard_backtest.strategy_spec.contracts import (
        FeatureGraph,
        FeatureKind,
        FeatureNode,
        FeatureOperator,
        SignalAction,
        SignalComparator,
        SignalRule,
        SignalRules,
    )

    spec = build_strategy_template(
        "multi_factor",
        strategy_id="signal_engine_multiperiod",
        dataset_release_ids=("release-multi",),
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
        FeatureNode(
            node_id="composite",
            label="复合得分",
            kind=FeatureKind.COMPOSITE,
            operator=FeatureOperator.WEIGHTED_SUM,
            inputs=("momentum", "volatility"),
            weights=(0.6, 0.4),
        ),
    )
    return spec.model_copy(
        update={
            "feature_graph": FeatureGraph(nodes=nodes, outputs=("composite",)),
            "signal_rules": SignalRules(
                rules=(
                    SignalRule(
                        rule_id="top_score_buy",
                        feature_id="composite",
                        comparator=SignalComparator.RANK_TOP,
                        threshold=0.5,
                        action=SignalAction.BUY,
                        rationale="复合得分前 50% 纳入目标仓位。",
                    ),
                )
            ),
        }
    )


@pytest.mark.asyncio
class TestMultiPeriodDecisionInputs:
    async def test_monthly_derives_multiple_decision_days(self) -> None:
        """monthly 推导:每自然月最后一个交易日决策;发布末尾无成交日的期末剔除。"""
        from finboard_backtest.research_run.signal_engine import (
            _derive_rebalance_decision_days,
            _release_trading_days,
        )

        start, end = date(2024, 1, 1), date(2024, 3, 31)
        provider = _multi_provider(start, end)
        days = await _release_trading_days(provider)  # type: ignore[arg-type]
        decisions = await _derive_rebalance_decision_days(
            provider, "monthly"  # type: ignore[arg-type]
        )
        # 1 月末 / 2 月末各一次;3 月末是发布最后交易日,没有下一交易日可成交,剔除。
        assert len(decisions) == 2
        assert [item[0].day for item in decisions] == [31, 29]
        # 决策日必须是交易日,且每个决策日之后都有可成交的交易日。
        for item in decisions:
            assert item[0].date() in days
        assert all(any(item[0].date() < day for day in days) for item in decisions)

    async def test_quarterly_derives_quarter_ends(self) -> None:
        """quarterly 推导:每季度最后一个交易日决策;发布末尾期末剔除。"""
        from finboard_backtest.research_run.signal_engine import (
            _derive_rebalance_decision_days,
        )

        start, end = date(2024, 1, 1), date(2024, 12, 31)
        provider = _multi_provider(start, end)
        decisions = await _derive_rebalance_decision_days(
            provider, "quarterly"  # type: ignore[arg-type]
        )
        decision_dates = [item[0].date() for item in decisions]
        # 3 月末 / 6 月末 / 9 月末各一次;12 月末是发布最后交易日,无下一成交日。
        assert len(decision_dates) == 3
        assert {item.month for item in decision_dates} == {3, 6, 9}

    async def test_invalid_frequency_fails_closed(self) -> None:
        """非法 rebalance_frequency:fail-closed(不会静默降级)。"""
        from finboard_backtest.research_run.signal_engine import build_decision_inputs

        provider = _multi_provider(date(2024, 1, 1), date(2024, 3, 31))
        manifest = _multi_manifest(_price_only_spec(), frequency="daily")

        def release_factory(release_id: str) -> _StubProvider:
            return provider

        async def snapshot_provider(snapshot_id: str) -> None:
            return None

        with pytest.raises(ValueError, match="rebalance_frequency"):
            await build_decision_inputs(
                manifest,
                release_provider_factory=release_factory,  # type: ignore[arg-type]
                snapshot_provider=snapshot_provider,
            )

    async def test_builds_period_inputs_with_recomputed_features(self) -> None:
        """多期路径:每期重算 price features 并组装决策输入。"""
        start, end = date(2024, 1, 1), date(2024, 3, 31)
        provider = _multi_provider(start, end)
        manifest = _multi_manifest(_price_only_spec(), frequency="monthly")

        def release_factory(release_id: str) -> _StubProvider:
            return provider

        async def snapshot_provider(snapshot_id: str) -> None:
            return None

        inputs = await build_decision_inputs(
            manifest,
            release_provider_factory=release_factory,  # type: ignore[arg-type]
            snapshot_provider=snapshot_provider,
        )
        # 1 月末 / 2 月末两次决策(3 月末为发布末日,无下一成交日)。
        assert len(inputs) == 2
        # 每期 features 由发布的行情重算,来源绑定冻结 release。
        for item in inputs:
            feature_names = {feature.feature_id for feature in item.features}
            assert "momentum" in feature_names
            assert "volatility_20d" in feature_names
            assert all(
                feature.source_artifact_ids == ("release-multi",) for feature in item.features
            )
            assert all(s.factor_snapshot_id is None for s in item.signals)
        # 决策时间严格递增。
        assert [item.decision_at for item in inputs] == sorted(item.decision_at for item in inputs)

    async def test_multi_period_empty_derivation_error_names_root_cause(self) -> None:
        """issue #203:声明多期但日历推导为空 → 报错区分根因,不再混报缺快照。"""
        provider = _multi_provider(date(2024, 1, 1), date(2024, 1, 31))
        manifest = _multi_manifest(_price_only_spec(), frequency="monthly")

        def release_factory(release_id: str) -> _StubProvider:
            return provider

        async def snapshot_provider(snapshot_id: str) -> None:
            return None

        # 发布只覆盖一个月,期末即发布末日(无后续成交日)→ 推导为空。
        with pytest.raises(ValueError, match="execution_mode=multi_period") as excinfo:
            await build_decision_inputs(
                manifest,
                release_provider_factory=release_factory,  # type: ignore[arg-type]
                snapshot_provider=snapshot_provider,
            )
        message = str(excinfo.value)
        assert "execution_mode=multi_period" in message
        assert "rebalance_frequency=monthly" in message
        assert "决策时点" in message
        # 不再与「未冻结快照」根因混报。
        assert "factor_snapshots" not in message

    async def test_single_shot_without_snapshots_error_names_root_cause(self) -> None:
        """issue #203:未声明频率 + 无快照 → single_shot 根因 + 修复路径。"""
        provider = _multi_provider(date(2024, 1, 1), date(2024, 3, 31))
        manifest = replace(_multi_manifest(_price_only_spec()), parameters={})

        def release_factory(release_id: str) -> _StubProvider:
            return provider

        async def snapshot_provider(snapshot_id: str) -> None:
            return None

        with pytest.raises(ValueError, match="execution_mode=single_shot") as excinfo:
            await build_decision_inputs(
                manifest,
                release_provider_factory=release_factory,  # type: ignore[arg-type]
                snapshot_provider=snapshot_provider,
            )
        message = str(excinfo.value)
        assert "factor_snapshot_ids" in message
        assert "rebalance_frequency=monthly|quarterly" in message
        # 不再误指 multi_period 的日历推导根因。
        assert "交易日历" not in message

    async def test_mid_series_load_failure_carries_decision_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """issue #263:中期加载失败在 bare raise 前挂决策标记,消息不改写。

        第 2 期(2024-02-29)loader 抛裸 RuntimeError —— 异常类型 / 消息 /
        traceback 保持原样,仅附带失败期次决策时点与 bars 主发布标记,
        runner 通用收口据此在 error_summary 头部补全定位上下文。
        """
        start, end = date(2024, 1, 1), date(2024, 3, 31)
        provider = _multi_provider(start, end)
        manifest = _multi_manifest(_price_only_spec(), frequency="monthly")
        real_load_context = FrozenInputLoader.load_context

        async def failing_second_period(
            self: FrozenInputLoader,
            manifest_arg: ResearchRunManifest,
            *,
            decision_at: datetime,
            execution_at: datetime,
        ) -> LoadedDecisionContext:
            if decision_at.date() == date(2024, 2, 29):
                raise RuntimeError("中期数据缺失:2 月发布损坏")
            return await real_load_context(
                self,
                manifest_arg,
                decision_at=decision_at,
                execution_at=execution_at,
            )

        monkeypatch.setattr(
            FrozenInputLoader, "load_context", failing_second_period
        )

        def release_factory(release_id: str) -> _StubProvider:
            return provider

        async def snapshot_provider(snapshot_id: str) -> None:
            return None

        with pytest.raises(RuntimeError, match="中期数据缺失") as excinfo:
            await build_decision_load_contexts(
                manifest,
                release_provider_factory=release_factory,  # type: ignore[arg-type]
                snapshot_provider=snapshot_provider,
            )

        marker = read_decision_load_context(excinfo.value)
        assert marker is not None
        assert marker.decision_at.date() == date(2024, 2, 29)
        assert marker.release_id == "release-multi"
        # bare raise 语义:消息与类型均不被改写(既有具名文案零破坏)。
        assert str(excinfo.value) == "中期数据缺失:2 月发布损坏"


class TestSingleShotSnapshotGate:
    """issue #203:入队期 single_shot 缺快照门控(REST / MCP 共用)。"""

    def test_multi_period_with_frequency_passes_without_snapshots(self) -> None:
        """声明合法频率(multi_period)不要求预建快照,行为不受 #203 影响。"""
        for frequency in ("monthly", "quarterly"):
            assert (
                single_shot_snapshot_gate_error(
                    strategy_kind="multi_factor",
                    required_factor_sources={"pb"},
                    frozen_snapshot_count=0,
                    parameters={"rebalance_frequency": frequency},
                )
                is None
            )

    def test_factor_dependencies_without_snapshots_rejected(self) -> None:
        """single_shot 依赖因子输入但缺快照:拒绝并附 execution_mode 与缺失源。"""
        error = single_shot_snapshot_gate_error(
            strategy_kind="multi_factor",
            required_factor_sources={"pb", "momentum"},
            frozen_snapshot_count=0,
            parameters={},
        )
        assert error is not None
        assert "execution_mode=single_shot" in error
        assert "['momentum', 'pb']" in error
        assert "factor_snapshot_ids" in error

    def test_executable_kind_without_factor_sources_rejected(self) -> None:
        """multi_factor 即便无因子源,single_shot 决策时点也只能来自快照。"""
        error = single_shot_snapshot_gate_error(
            strategy_kind="multi_factor",
            required_factor_sources=set(),
            frozen_snapshot_count=0,
            parameters={},
        )
        assert error is not None
        assert "execution_mode=single_shot" in error
        assert "factor_snapshot_ids 为空" in error

    def test_snapshots_frozen_passes(self) -> None:
        """single_shot 已冻结快照:放行(fail-closed 只拦缺快照)。"""
        assert (
            single_shot_snapshot_gate_error(
                strategy_kind="multi_factor",
                required_factor_sources={"pb"},
                frozen_snapshot_count=1,
                parameters={},
            )
            is None
        )

    def test_unsupported_kind_without_snapshots_not_gated(self) -> None:
        """其余 kind 由 worker 报 not_implemented,入队不拦截以免遮蔽根因。"""
        assert (
            single_shot_snapshot_gate_error(
                strategy_kind="etf_rotation",
                required_factor_sources=set(),
                frozen_snapshot_count=0,
                parameters={},
            )
            is None
        )


@pytest.mark.asyncio
class TestDailyEquityCurve:
    async def test_curve_covers_all_trading_days_and_segments(self) -> None:
        """每日权益曲线:覆盖全区间交易日,并按成交执行日切换账本段。"""
        from finboard_backtest.research_run.contracts import (
            LedgerSnapshot,
            RebalanceInstruction,
            ResearchFill,
            ResearchFillAction,
            ResearchOrder,
            ResearchOrderStatus,
            ResearchPipelineEvidence,
            ResearchPosition,
            ResearchPositionSide,
            ResearchRiskState,
            UniverseCandidate,
        )
        from finboard_backtest.research_run.signal_engine import (
            build_daily_equity_curve,
        )

        start, end = date(2024, 1, 1), date(2024, 2, 29)
        provider = _multi_provider(start, end)
        manifest = _multi_manifest(_price_only_spec(), frequency="monthly")
        business_date = date(2024, 1, 31)
        decision_at = datetime(2024, 1, 31, 15, 0, tzinfo=UTC)
        execution_at = datetime(2024, 2, 1, 15, 0, tzinfo=UTC)
        execution_close = {
            symbol: provider.closes_by_symbol[symbol][date(2024, 2, 1)]
            for symbol in ("000001.SZ", "000002.SZ")
        }

        candidates = tuple(
            UniverseCandidate(
                symbol=symbol,
                included=True,
                reasons=("测试候选",),
                asset_class="equity",
                market="a_share",
            )
            for symbol in ("000001.SZ", "000002.SZ")
        )
        fills = tuple(
            ResearchFill(
                research_fill_id=f"RR-MT:F:{symbol}:0",
                research_order_id=f"RR-MT:O:{symbol}:0",
                symbol=symbol,
                action=ResearchFillAction.OPEN_LONG,
                quantity=Decimal("3000"),
                price=execution_close[symbol],
                filled_at=execution_at,
            )
            for symbol in ("000001.SZ", "000002.SZ")
        )
        orders = tuple(
            ResearchOrder(
                research_order_id=f"RR-MT:O:{symbol}:0",
                instruction_id=f"RR-MT:I:{symbol}:0",
                symbol=symbol,
                action=ResearchFillAction.OPEN_LONG,
                quantity=Decimal("3000"),
                status=ResearchOrderStatus.FILLED,
            )
            for symbol in ("000001.SZ", "000002.SZ")
        )
        instructions = tuple(
            RebalanceInstruction(
                instruction_id=f"RR-MT:I:{symbol}:0",
                symbol=symbol,
                action=ResearchFillAction.OPEN_LONG,
                target_quantity=Decimal("3000"),
                current_quantity=Decimal("0"),
                delta_quantity=Decimal("3000"),
                lot_size=100,
                estimated_value=Decimal("31500"),
                reason="测试调仓",
            )
            for symbol in ("000001.SZ", "000002.SZ")
        )
        positions = tuple(
            ResearchPosition(
                symbol=symbol,
                position_side=ResearchPositionSide.LONG,
                quantity=Decimal("3000"),
                average_price=execution_close[symbol],
                market_price=execution_close[symbol],
                market_value=Decimal("3000") * execution_close[symbol],
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
            )
            for symbol in ("000001.SZ", "000002.SZ")
        )
        invested = sum(
            (Decimal("3000") * execution_close[position.symbol] for position in positions),
            Decimal(),
        )
        ledger = LedgerSnapshot(
            cash=manifest.initial_capital - invested,
            market_value=invested,
            margin_used=Decimal("0"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            equity=manifest.initial_capital,
            fees_paid=Decimal("0"),
            tax_paid=Decimal("0"),
            slippage_paid=Decimal("0"),
        )
        decision = DecisionBundle(
            business_date=business_date,
            decision_at=decision_at,
            candidates=candidates,
            features=(),
            signals=(),
            targets_before_constraints=(),
            constraints=(),
            targets_after_constraints=(),
            risk_exits=(),
            targets_after_risk=(),
            risk_state=ResearchRiskState(
                cooldown_until={},
                opened_on={"000001.SZ": business_date, "000002.SZ": business_date},
                high_water_prices={"000001.SZ": 10.5, "000002.SZ": 10.5},
                portfolio_equity_high_water=Decimal("100000"),
                portfolio_drawdown=0.0,
                portfolio_paused=False,
            ),
            capital_feasibility=(),
            rebalance_plan=instructions,
            orders=orders,
            fills=fills,
            positions=positions,
            ledger=ledger,
            pipeline_evidence=ResearchPipelineEvidence(
                manifest_input_checksum=manifest.input_checksum,
                input_checksum=stable_checksum({"test": "input"}),
                output_checksum=stable_checksum({"test": "output"}),
                hard_constraints_passed=True,
            ),
        )
        curve = await build_daily_equity_curve(
            provider,  # type: ignore[arg-type]
            manifest,
            (decision,),
        )
        assert curve
        # 覆盖发布全部交易日(1 月至 2 月末)。
        trading_days = _calendar(start, end)
        assert [item.trade_date for item in curve] == trading_days
        # 执行日(2024-02-01)前权益 = 初始资金;执行日按市价成交,权益仍为初始值。
        feb1 = trading_days.index(date(2024, 2, 1))
        assert curve[feb1 - 1].equity == manifest.initial_capital
        assert curve[feb1].equity == manifest.initial_capital
        # 执行日后持仓随行情漂移,曲线末端(2 月末)权益高于初始资金
        # (纯回放不强制平仓,期末持仓按最后行情持续计值)。
        assert curve[-1].equity > manifest.initial_capital
