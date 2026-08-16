"""multi_factor 信号引擎单元测试(issue #170)。

用 stub 冻结快照 / 发布验证 FeatureGraph 算子、SignalRules comparator、
available_at 过滤与候选池过滤;不依赖 PostgreSQL 或 Parquet 文件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from finboard_backtest.research_run.contracts import (
    FeatureValue,
    FrozenArtifactRef,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_run.signal_engine import (
    build_decision_inputs,
    build_normalized_signals,
    evaluate_feature_graph,
    evaluate_signal_rules,
)
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_backtest.strategy_spec.contracts import ResearchStrategySpec
from finboard_data.factor_lab import FeatureObservation
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
    name: str = "stub"
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
                    timestamp=datetime.combine(
                        day, datetime.min.time(), tzinfo=UTC
                    ),
                ),
                datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            )
            for day, close in sorted(by_date.items())
            if day <= end
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
        included = frozenset(
            {"000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ", "000005.SZ"}
        )
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

    def _snapshots(
        self, decision_at: datetime
    ) -> dict[str, _StubSnapshot]:
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
        release_factory, snapshot_provider = self._factories(
            provider, self._snapshots(decision_at)
        )

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
        instruments = tuple(
            _StubInstrument(code=f"00000{i}.SZ") for i in range(1, 6)
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
                    _obs(inst.code, "momentum", float(index + 1))
                    for index, inst in enumerate(instruments)
                )
                + tuple(
                    _obs(inst.code, "pb", 1.0) for inst in instruments
                )
                + tuple(
                    _obs(inst.code, "volatility_20d", 0.3)
                    for inst in instruments
                ),
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
