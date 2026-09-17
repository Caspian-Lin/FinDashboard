"""issue #380:信号引擎特征截面与候选池口径统一(benchmark-only 排除)。

发布含指数 / 期货主连(benchmark-only)时,候选池已排除
(``is_benchmark_only_instrument``,#256/#267),但特征截面此前未同步:
RR-0f7b8a80 实测发布 51 标的(40 股票 + 7 指数 + 4 期货主连)全部进
rank 截面,rank_top 0.3 买池 12→~15,rank_bottom 0.5 的 ratio 因分母
变大整体缩小 ~21% → sell 区边界持仓滑入中性区失去信号 → 再平衡带保留
无信号持仓 → ``targets ⊄ signals`` → hard_constraint_rejected。

本文件锁定修复语义:
* 逐期价格特征重算源头收窄到发布可交易域(未声明 explicit_symbols 时);
* 装配兜底过滤覆盖快照 / 研究发布观测(single_shot 与 multi_period 统一);
* explicit_symbols 显式声明豁免(#254/#299 声明域优先);
* rank 分母恢复股票域(过滤后截面 vs 未过滤截面的命中对照)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from finboard_backtest.research_run.contracts import (
    FeatureValue,
    FrozenArtifactRef,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_run.signal_engine import (
    _compute_period_features,
    _exclude_benchmark_only_features,
    build_decision_inputs,
    evaluate_signal_rules,
)
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_backtest.strategy_spec.contracts import (
    ResearchStrategySpec,
    SignalAction,
    SignalComparator,
    SignalConflictPolicy,
    SignalRule,
    SignalRules,
)
from finboard_data.factor_lab import FeatureObservation
from finboard_data.releases import CloseHistoryColumns, ReleaseDatasetKind
from finboard_shared.types import AssetClass, InstrumentType, Market

_RELEASE_ID = "release-v1"
#: 4 只可交易股票 + 1 指数 + 1 期货主连(与 RR-0f7b8a80 的 51 = 40+7+4 同构)。
_STOCKS = ("000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ")
_INDEX = "000300.SH"
_FUTURES = "IM0.CFFEX"


def _instrument_type(code: str) -> InstrumentType:
    if code.endswith(".CFFEX"):
        return InstrumentType.FUTURES
    if code == _INDEX:
        return InstrumentType.INDEX
    return InstrumentType.STOCK


@dataclass(frozen=True, slots=True)
class _StubExecution:
    lot_size: Decimal = Decimal("100")
    price_tick: Decimal = Decimal("0.01")
    multiplier: Decimal = Decimal("1")
    margin_rate: Decimal | None = None
    stamp_tax_rate: Decimal = Decimal("0.001")
    commission_rate: Decimal = Decimal("0.0003")
    commission_min: Decimal = Decimal("5")


@dataclass(frozen=True, slots=True)
class _StubInstrument:
    code: str
    instrument_type: InstrumentType = InstrumentType.STOCK
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
    period: str = "d1"
    adjustment: str = "qfq"
    start_date: date = date(2024, 1, 2)
    end_date: date = date(2024, 3, 4)
    source: str = "stub"
    version: str = "v1"
    release_checksum: str = "c" * 64
    dataset_kind: object = ReleaseDatasetKind.BARS


@dataclass
class _StubBar:
    timestamp: datetime
    open: Decimal | None = None
    close: Decimal | None = None


@dataclass
class _StubPointInTimeBar:
    bar: _StubBar
    available_at: datetime


@dataclass
class _StubProvider:
    """按 symbol 返回 PIT close 列式的 stub 发布 provider(与 #300 路径对齐)。"""

    release: _StubRelease
    closes_by_symbol: dict[str, dict[date, Decimal]]

    async def fetch_bars(
        self,
        symbol: object,
        period: object,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[_StubBar]:
        """非 PIT 全量 bars(交易日历推导用,#334 多点采样)。"""
        del period, start, adjust
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        return [
            _StubBar(
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                close=close,
            )
            for day, close in sorted((by_date or {}).items())
            if day <= end
        ]

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
        """PIT bars(#336 执行价基读取;stub 中 open=close)。"""
        del period, start, adjust
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        return [
            _StubPointInTimeBar(
                _StubBar(
                    timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                    open=close,
                    close=close,
                ),
                datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            )
            for day, close in sorted((by_date or {}).items())
            if day <= end
            and datetime.combine(day, datetime.min.time(), tzinfo=UTC) <= decision_at
        ]

    async def fetch_close_history(
        self,
        symbol: object,
        period: object,
        start: date,
        end: date,
        *,
        decision_at: datetime,
        adjust: str = "qfq",
    ) -> CloseHistoryColumns:
        del period, start, adjust
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        visible = [
            (day, close)
            for day, close in sorted((by_date or {}).items())
            if day <= end
            and datetime.combine(day, datetime.min.time(), tzinfo=UTC) <= decision_at
        ]
        return CloseHistoryColumns(
            dates=tuple(day for day, _ in visible),
            available_at=tuple(
                datetime.combine(day, datetime.min.time(), tzinfo=UTC)
                for day, _ in visible
            ),
            closes=np.array([float(close) for _, close in visible], dtype=np.float64),
            last_timestamp=(
                datetime.combine(visible[-1][0], datetime.min.time(), tzinfo=UTC)
                if visible
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class _StubSnapshot:
    snapshot_id: str
    decision_at: datetime
    observations: tuple[FeatureObservation, ...]


def _weekdays(start: date, count: int) -> list[date]:
    result: list[date] = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _closes(count: int = 30) -> dict[str, dict[date, Decimal]]:
    days = _weekdays(date(2024, 1, 2), count)
    return {
        code: {
            day: Decimal(f"{10 + index * 0.5 + i * 0.01:.2f}")
            for i, day in enumerate(days)
        }
        for index, code in enumerate((*_STOCKS, _INDEX, _FUTURES))
    }


def _provider(
    instruments: tuple[_StubInstrument, ...] | None = None,
    closes: dict[str, dict[date, Decimal]] | None = None,
) -> _StubProvider:
    return _StubProvider(
        release=_StubRelease(_RELEASE_ID, instruments or _instruments()),
        closes_by_symbol=closes or _closes(),
    )


def _instruments() -> tuple[_StubInstrument, ...]:
    return tuple(
        _StubInstrument(code=code, instrument_type=_instrument_type(code))
        for code in (*_STOCKS, _INDEX, _FUTURES)
    )


def _spec() -> ResearchStrategySpec:
    return build_strategy_template(
        "multi_factor",
        strategy_id="issue380bench",
        dataset_release_ids=(_RELEASE_ID,),
    )


def _manifest(spec: ResearchStrategySpec | None = None) -> ResearchRunManifest:
    return ResearchRunManifest(
        run_id="RR-issue380bench0001",
        idempotency_key="issue-380-bench-0001",
        strategy_spec=spec or _spec(),
        strategy_spec_checksum=stable_checksum((spec or _spec()).canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id=_RELEASE_ID,
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


def _features(symbols: tuple[str, ...] | list[str]) -> tuple[FeatureValue, ...]:
    available_at = datetime(2024, 2, 29, tzinfo=UTC)
    return tuple(
        FeatureValue(
            symbol=symbol,
            feature_id="momentum",
            value=0.1,
            source_artifact_ids=(_RELEASE_ID,),
            available_at=available_at,
        )
        for symbol in symbols
    )


# ---- 装配兜底过滤(纯函数) ----------------------------------------------------


class TestExcludeBenchmarkOnlyFeatures:
    def test_filters_index_and_futures_observations(self) -> None:
        manifest = _manifest()
        kept = _exclude_benchmark_only_features(
            _features((*_STOCKS, _INDEX, _FUTURES)),
            _provider(),  # type: ignore[arg-type]
            manifest,
        )
        assert {item.symbol for item in kept} == set(_STOCKS)

    def test_explicit_symbols_exempt_benchmark_declaration(self) -> None:
        """声明域优先(#254/#299):explicit_symbols 显式声明的指数观测保留。"""
        spec = _spec().model_copy(
            update={
                "universe": _spec().universe.model_copy(
                    update={"explicit_symbols": (*_STOCKS, _INDEX)}
                )
            }
        )
        kept = _exclude_benchmark_only_features(
            _features((*_STOCKS, _INDEX, _FUTURES)),
            _provider(),  # type: ignore[arg-type]
            _manifest(spec),
        )
        assert {item.symbol for item in kept} == {*_STOCKS, _INDEX}

    def test_noop_when_release_has_no_benchmark_instruments(self) -> None:
        stocks_only = _provider(
            tuple(_StubInstrument(code=code) for code in _STOCKS)
        )
        features = _features(_STOCKS)
        assert (
            _exclude_benchmark_only_features(
                features,
                stocks_only,  # type: ignore[arg-type]
                _manifest(),
            )
            is features
        )


# ---- 逐期重算源头收窄 ----------------------------------------------------------


class TestComputePeriodFeaturesNarrowing:
    @pytest.mark.asyncio
    async def test_period_features_exclude_benchmark_instruments(self) -> None:
        decision_at = datetime(2024, 2, 25, 15, tzinfo=UTC)
        features = await _compute_period_features(
            _provider(),  # type: ignore[arg-type]
            _manifest(),
            decision_at,
            _RELEASE_ID,
        )
        symbols = {item.symbol for item in features}
        assert symbols == set(_STOCKS)
        assert {"momentum", "volatility_20d"} <= {item.feature_id for item in features}

    @pytest.mark.asyncio
    async def test_explicit_symbols_keep_index_features(self) -> None:
        """声明 explicit_symbols 含指数时按声明域重算,指数特征保留。"""
        spec = _spec().model_copy(
            update={
                "universe": _spec().universe.model_copy(
                    update={"explicit_symbols": (*_STOCKS, _INDEX)}
                )
            }
        )
        decision_at = datetime(2024, 2, 25, 15, tzinfo=UTC)
        features = await _compute_period_features(
            _provider(),  # type: ignore[arg-type]
            _manifest(spec),
            decision_at,
            _RELEASE_ID,
        )
        assert {item.symbol for item in features} == {*_STOCKS, _INDEX}


# ---- rank 分母语义(截面污染对照) ----------------------------------------------


class TestRankCrossSectionDomain:
    def _rank_spec(self) -> ResearchStrategySpec:
        rule = SignalRule(
            rule_id="top_buy",
            feature_id="momentum",
            comparator=SignalComparator.RANK_TOP,
            action=SignalAction.BUY,
            threshold=0.3,
            rationale="复合得分前 30% 纳入目标仓位",
        )
        return _spec().model_copy(
            update={
                "signal_rules": SignalRules(
                    rules=(rule,),
                    conflict_policy=SignalConflictPolicy.HIGHEST_PRIORITY,
                    default_action=SignalAction.NEUTRAL,
                )
            }
        )

    def test_rank_top_denominator_restored_to_stock_domain(self) -> None:
        """10 股票 + 2 高分指数:污染截面(12)与股票域截面(10)的买池对照。

        指数分数全场最高时,未过滤截面下 S02 的 ratio = 4/12 ≈ 0.333 落榜;
        过滤后 S02 的 ratio = 2/10 = 0.2 命中——分母恢复即恢复旧快照版行为。
        """
        stocks = {f"S{i:02d}.SZ": 3.0 - i * 0.1 for i in range(10)}
        benchmarks = {"IDX1.SH": 5.0, "IDX2.SH": 4.5}
        included = frozenset(stocks)
        spec = self._rank_spec()

        stock_domain = evaluate_signal_rules(
            spec,
            node_finals={"momentum": dict(stocks)},
            node_series={},
            included_symbols=included,
            factor_snapshot_id=None,
        )
        polluted = evaluate_signal_rules(
            spec,
            node_finals={"momentum": {**stocks, **benchmarks}},
            node_series={},
            included_symbols=included,
            factor_snapshot_id=None,
        )

        assert {s.symbol for s in stock_domain if s.action == "buy"} == {
            "S00.SZ",
            "S01.SZ",
            "S02.SZ",
        }
        assert {s.symbol for s in polluted if s.action == "buy"} == {
            "S00.SZ",
            "S01.SZ",
        }

    def test_nan_scores_excluded_from_rank_pool_and_denominator(self) -> None:
        """NaN 复合得分语义锁定:不进买池、不参与排名分母(现状即安全)。

        ``_rank_ratios`` 以 ``math.isfinite`` 过滤、``_rule_matches`` 对非有限
        值跳过——NaN 标的既不命中 RANK_TOP 也不影响其他标的的 ratio。本用例
        把该语义固化为契约:含 NaN 截面与移除 NaN 后的截面产出逐符号一致。
        """
        stocks = {f"S{i:02d}.SZ": 3.0 - i * 0.1 for i in range(10)}
        with_nan = dict(stocks, **{"S05.SZ": float("nan")})
        without = {code: value for code, value in stocks.items() if code != "S05.SZ"}
        spec = self._rank_spec()

        def _buy_signals(finals: dict[str, float], included: frozenset[str]) -> set[str]:
            return {
                s.symbol
                for s in evaluate_signal_rules(
                    spec,
                    node_finals={"momentum": finals},
                    node_series={},
                    included_symbols=included,
                    factor_snapshot_id=None,
                )
            }

        signals_with_nan = _buy_signals(with_nan, frozenset(with_nan))
        signals_without = _buy_signals(without, frozenset(without))
        assert "S05.SZ" not in signals_with_nan
        assert signals_with_nan == signals_without


# ---- single_shot 集成(快照观测装配过滤) ---------------------------------------


class TestBuildDecisionInputsBenchmarkScope:
    @pytest.mark.asyncio
    async def test_snapshot_features_exclude_benchmark_observations(self) -> None:
        """single_shot 快照观测含指数/期货时,装配后不进信号截面。"""
        decision_at = datetime(2024, 3, 1, 15, tzinfo=UTC)
        closes = {
            code: {
                date(2024, 2, 27): Decimal("9.6"),
                date(2024, 2, 28): Decimal("9.8"),
                date(2024, 3, 1): Decimal("10.0"),
                date(2024, 3, 4): Decimal("10.5"),
            }
            for code in (*_STOCKS, _INDEX, _FUTURES)
        }
        provider = _provider(_instruments(), closes)
        available_at = datetime(2024, 2, 29, tzinfo=UTC)

        def _obs(symbol: str, name: str, value: float) -> FeatureObservation:
            return FeatureObservation(
                symbol=symbol,
                feature_name=name,
                value=value,
                observed_at=available_at,
                available_at=available_at,
                source="factor-v1",
                source_version="v1",
            )

        observations = [
            _obs(code, name, value)
            for code in _STOCKS
            for name, value in (
                ("pb", 1.0),
                ("momentum", 0.1),
                ("volatility_20d", 0.2),
            )
        ] + [
            _obs(_INDEX, "momentum", 9.9),
            _obs(_FUTURES, "pb", 0.5),
        ]
        snapshots = {
            "factor-v1": _StubSnapshot(
                snapshot_id="factor-v1",
                decision_at=decision_at,
                observations=tuple(observations),
            )
        }

        def release_factory(release_id: str) -> _StubProvider:
            assert release_id == _RELEASE_ID
            return provider

        async def snapshot_provider(snapshot_id: str) -> _StubSnapshot | None:
            return snapshots.get(snapshot_id)

        inputs = await build_decision_inputs(
            _manifest(),
            release_provider_factory=release_factory,  # type: ignore[arg-type]
            snapshot_provider=snapshot_provider,  # type: ignore[arg-type]
        )

        assert len(inputs) == 1
        item = inputs[0]
        # 快照里的指数 momentum / 期货 pb 观测被装配兜底过滤,不进信号截面。
        assert {f.symbol for f in item.features} == set(_STOCKS)
        included = {c.symbol for c in item.candidates if c.included}
        assert included == set(_STOCKS)
        assert {s.symbol for s in item.signals} <= included
