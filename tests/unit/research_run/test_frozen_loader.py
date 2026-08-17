"""冻结产物加载器机械映射单元测试(issue #143)。

用 stub ``FrozenReleaseProvider`` / ``FeatureSnapshot`` 验证 ``FrozenInputLoader``
的机械字段映射(价格 / 执行元数据 / 候选池 / 特征 / artifact 绑定),不依赖
PostgreSQL 或 Parquet 文件。PIT 门控、价格读取由 stub 直接返回,断言聚焦映射正确性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_run.frozen_loader import FrozenInputLoader
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_data.factor_lab import FeatureObservation
from finboard_data.releases import ReleaseDatasetKind
from finboard_shared.types import AssetClass, Market

# ---- stubs ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StubBar:
    close: Decimal


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


@dataclass
class _StubRelease:
    release_id: str
    instruments: tuple[_StubInstrument, ...]
    period: object = "d1"
    adjustment: str = "qfq"
    start_date: date = date(2024, 1, 1)
    dataset_kind: object = ReleaseDatasetKind.BARS


@dataclass
class _StubProvider:
    release: _StubRelease
    close_by_symbol: dict[str, Decimal]

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
        del period, start, end, decision_at, adjust
        close = self.close_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        if close is None:
            return []
        return [_StubPointInTimeBar(_StubBar(close), datetime.now(UTC))]


@dataclass
class _StubSnapshot:
    snapshot_id: str
    observations: tuple[FeatureObservation, ...]


# ---- fixtures ---------------------------------------------------------------


def _manifest(
    *,
    release_id: str = "release-v1",
    snapshot_ids: tuple[str, ...] = ("factor-v1",),
) -> ResearchRunManifest:
    spec = build_strategy_template(
        "ma_cross",
        strategy_id="ma_cross_test",
        dataset_release_ids=(release_id,),
    )
    return ResearchRunManifest(
        run_id="RR-frozenloadertest01",
        idempotency_key="frozen-loader-test-0001",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id=release_id,
                version="2026-01-01",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        factor_snapshots=tuple(
            FrozenArtifactRef(
                artifact_id=sid,
                version="v1",
                checksum="b" * 64,
                capabilities=("factor:momentum",),
            )
            for sid in snapshot_ids
        ),
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
    )


def _loader(
    provider: _StubProvider,
    snapshots: dict[str, _StubSnapshot],
) -> FrozenInputLoader:
    def _release_factory(release_id: str) -> _StubProvider:
        assert release_id == provider.release.release_id
        return provider

    async def _snapshot_provider(snapshot_id: str) -> _StubSnapshot | None:
        return snapshots.get(snapshot_id)

    return FrozenInputLoader(
        release_provider_factory=_release_factory,  # type: ignore[arg-type]
        snapshot_provider=_snapshot_provider,  # type: ignore[arg-type]
    )


# ---- tests ------------------------------------------------------------------


@pytest.mark.asyncio
class TestFrozenInputLoader:
    async def test_loads_mechanical_fields(self) -> None:
        instruments = (
            _StubInstrument(code="510300.SH"),
            _StubInstrument(code="600519.SH"),
        )
        provider = _StubProvider(
            release=_StubRelease("release-v1", instruments),
            close_by_symbol={
                "510300.SH": Decimal("4.50"),
                "600519.SH": Decimal("1800.0"),
            },
        )
        decision_at = datetime(2024, 3, 1, 15, tzinfo=UTC)
        execution_at = datetime(2024, 3, 4, 9, 30, tzinfo=UTC)
        snapshot = _StubSnapshot(
            snapshot_id="factor-v1",
            observations=(
                FeatureObservation(
                    symbol="510300.SH",
                    feature_name="momentum",
                    value=0.12,
                    observed_at=datetime(2024, 2, 29, tzinfo=UTC),
                    available_at=datetime(2024, 2, 29, tzinfo=UTC),
                    source="release-v1",
                    source_version="v1",
                ),
            ),
        )
        loader = _loader(provider, {"factor-v1": snapshot})
        manifest = _manifest()

        ctx = await loader.load_context(
            manifest, decision_at=decision_at, execution_at=execution_at
        )

        # 候选池:全部 included,字段来自 instrument。
        assert len(ctx.candidates) == 2
        assert {c.symbol for c in ctx.candidates} == {"510300.SH", "600519.SH"}
        assert all(c.included for c in ctx.candidates)
        assert ctx.candidates[0].asset_class == "equity"

        # 价格:决策日 close。
        assert ctx.prices["510300.SH"] == pytest.approx(4.5)
        assert ctx.prices["600519.SH"] == pytest.approx(1800.0)
        # 成交价:本期 stub 返回同一 close。
        assert ctx.execution_prices["510300.SH"] == pytest.approx(4.5)

        # 执行元数据:ExecutionMetadata → AssetLotInfo 字段映射。
        lot = ctx.lot_info["510300.SH"]
        assert lot.code == "510300.SH"
        assert lot.lot_size == 100
        assert lot.multiplier == 1.0
        assert lot.commission_rate == pytest.approx(0.0003)
        assert lot.commission_min == pytest.approx(5.0)
        assert lot.stamp_tax_rate == pytest.approx(0.001)

        # 特征:FeatureObservation → FeatureValue 映射。
        assert len(ctx.features) == 1
        feat = ctx.features[0]
        assert feat.symbol == "510300.SH"
        assert feat.feature_id == "momentum"
        assert feat.value == pytest.approx(0.12)
        assert feat.source_artifact_ids == ("factor-v1",)

        # artifact 绑定:release + snapshot 去重保序。
        assert ctx.input_artifact_ids == ("release-v1", "factor-v1")

        # 时间戳透传。
        assert ctx.business_date == date(2024, 3, 1)
        assert ctx.decision_at == decision_at
        assert ctx.execution_at == execution_at
        assert ctx.included_symbols == ("510300.SH", "600519.SH")

    async def test_filters_features_by_decision_at(self) -> None:
        provider = _StubProvider(
            release=_StubRelease("release-v1", (_StubInstrument("510300.SH"),)),
            close_by_symbol={"510300.SH": Decimal("4.50")},
        )
        decision_at = datetime(2024, 3, 1, 15, tzinfo=UTC)
        snapshot = _StubSnapshot(
            snapshot_id="factor-v1",
            observations=(
                FeatureObservation(
                    symbol="510300.SH",
                    feature_name="momentum",
                    value=0.12,
                    observed_at=datetime(2024, 2, 29, tzinfo=UTC),
                    available_at=datetime(2024, 2, 29, tzinfo=UTC),
                    source="release-v1",
                    source_version="v1",
                ),
                # 这条 available_at 晚于 decision_at,应被过滤。
                FeatureObservation(
                    symbol="510300.SH",
                    feature_name="volatility_20d",
                    value=0.3,
                    observed_at=datetime(2024, 3, 5, tzinfo=UTC),
                    available_at=datetime(2024, 3, 5, tzinfo=UTC),
                    source="release-v1",
                    source_version="v1",
                ),
            ),
        )
        loader = _loader(provider, {"factor-v1": snapshot})

        ctx = await loader.load_context(
            _manifest(),
            decision_at=decision_at,
            execution_at=datetime(2024, 3, 4, 9, 30, tzinfo=UTC),
        )

        assert len(ctx.features) == 1
        assert ctx.features[0].feature_id == "momentum"

    async def test_skips_not_ready_instruments(self) -> None:
        provider = _StubProvider(
            release=_StubRelease(
                "release-v1",
                (
                    _StubInstrument(code="510300.SH", ready=True),
                    _StubInstrument(code="600519.SH", ready=False),
                ),
            ),
            close_by_symbol={
                "510300.SH": Decimal("4.50"),
                "600519.SH": Decimal("1800.0"),
            },
        )
        loader = _loader(provider, {})

        ctx = await loader.load_context(
            _manifest(snapshot_ids=()),
            decision_at=datetime(2024, 3, 1, 15, tzinfo=UTC),
            execution_at=datetime(2024, 3, 4, 9, 30, tzinfo=UTC),
        )

        # ready=False 的标的不应进候选池 / lot_info / prices。
        assert {c.symbol for c in ctx.candidates} == {"510300.SH"}
        assert "600519.SH" not in ctx.lot_info
        assert "600519.SH" not in ctx.prices

    async def test_missing_price_excluded_from_prices(self) -> None:
        """某标的在决策日无可见 Bar(停牌),prices 不含该标的,但不抛错。"""
        provider = _StubProvider(
            release=_StubRelease(
                "release-v1",
                (
                    _StubInstrument(code="510300.SH"),
                    _StubInstrument(code="000001.SZ"),
                ),
            ),
            close_by_symbol={"510300.SH": Decimal("4.50")},  # 缺 000001.SZ
        )
        loader = _loader(provider, {})

        ctx = await loader.load_context(
            _manifest(snapshot_ids=()),
            decision_at=datetime(2024, 3, 1, 15, tzinfo=UTC),
            execution_at=datetime(2024, 3, 4, 9, 30, tzinfo=UTC),
        )

        assert "510300.SH" in ctx.prices
        assert "000001.SZ" not in ctx.prices

    async def test_execution_before_decision_raises(self) -> None:
        provider = _StubProvider(
            release=_StubRelease("release-v1", (_StubInstrument("510300.SH"),)),
            close_by_symbol={"510300.SH": Decimal("4.50")},
        )
        loader = _loader(provider, {})
        decision_at = datetime(2024, 3, 4, 15, tzinfo=UTC)
        execution_at = datetime(2024, 3, 1, 9, 30, tzinfo=UTC)  # 早于 decision_at

        with pytest.raises(ValueError, match="execution_at"):
            await loader.load_context(
                _manifest(snapshot_ids=()),
                decision_at=decision_at,
                execution_at=execution_at,
            )

    async def test_dedupes_artifact_ids(self) -> None:
        """release_id 与 snapshot_id 相同时去重。"""
        provider = _StubProvider(
            release=_StubRelease("release-v1", (_StubInstrument("510300.SH"),)),
            close_by_symbol={"510300.SH": Decimal("4.50")},
        )
        loader = _loader(provider, {})

        # 构造 snapshot_id 与 release_id 相同的 manifest(理论边界)。
        spec = build_strategy_template(
            "ma_cross",
            strategy_id="ma_cross_test",
            dataset_release_ids=("release-v1",),
        )
        manifest = ResearchRunManifest(
            run_id="RR-dedupartidtest00001",
            idempotency_key="dedup-art-id-test-001",
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
                    artifact_id="release-v1",  # 与 release_id 重复
                    version="v1",
                    checksum="b" * 64,
                    capabilities=("factor:momentum",),
                ),
            ),
            code_version="abcdef0123456789",
            initial_capital=Decimal("100000"),
            requested_by="unit-test",
        )

        ctx = await loader.load_context(
            manifest,
            decision_at=datetime(2024, 3, 1, 15, tzinfo=UTC),
            execution_at=datetime(2024, 3, 4, 9, 30, tzinfo=UTC),
        )

        assert ctx.input_artifact_ids == ("release-v1",)


@pytest.mark.asyncio
class TestMultiReleaseMerge:
    """issue #187:多 release 按 kind 融合(bars + daily_metrics)。"""

    def _daily_provider(self) -> _StubProvider:
        """返回 daily_metrics 研究数据发布 stub(PIT 观测)。"""
        from finboard_data.research import DailySecurityMetrics

        daily_metric = DailySecurityMetrics(
            symbol="600519.SH",
            trade_date=date(2024, 2, 29),
            close=Decimal("1800.0"),
            turnover_rate=Decimal("0.005"),
            turnover_rate_free=Decimal("0.004"),
            volume_ratio=Decimal("1.1"),
            pe=Decimal("45.0"),
            pe_ttm=Decimal("44.0"),
            pb=Decimal("9.5"),
            ps=Decimal("9.0"),
            ps_ttm=Decimal("8.8"),
            dividend_yield=Decimal("0.01"),
            dividend_yield_ttm=Decimal("0.011"),
            total_shares=Decimal("1000000000"),
            float_shares=Decimal("900000000"),
            free_shares=Decimal("850000000"),
            total_market_cap=Decimal("2200000000000"),
            circulating_market_cap=Decimal("1980000000000"),
            limit_status=0,
            source="tushare",
            observed_at=datetime(2024, 2, 29, 12, tzinfo=UTC),
            available_at=datetime(2024, 2, 29, 15, 30, tzinfo=UTC),
        )
        daily_release = _StubRelease(
            "daily-release-v1",
            (),
            dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        )
        daily_provider = _StubProvider(
            release=daily_release,
            close_by_symbol={},
        )
        daily_provider._daily_metrics = {  # type: ignore[attr-defined]
            "600519.SH": (daily_metric,),
        }

        async def _fetch_daily(
            symbol: object,
            *,
            start: date,
            end: date,
            decision_at: datetime,
        ) -> list[DailySecurityMetrics]:
            del start, end
            return [
                item
                for item in daily_provider._daily_metrics.get(  # type: ignore[attr-defined]
                    symbol.code,  # type: ignore[attr-defined]
                    (),
                )
                if item.available_at <= decision_at
            ]

        daily_provider.fetch_daily_metrics = _fetch_daily  # type: ignore[attr-defined]
        daily_provider._release_ref = daily_release  # type: ignore[attr-defined]
        return daily_provider

    async def test_bars_plus_daily_metrics_merge_features(self) -> None:
        instruments = (_StubInstrument(code="600519.SH"),)
        bars_provider = _StubProvider(
            release=_StubRelease("bars-release-v1", instruments),
            close_by_symbol={"600519.SH": Decimal("1800.0")},
        )
        daily_provider = self._daily_provider()

        def _release_factory(release_id: str) -> _StubProvider:
            return {
                "bars-release-v1": bars_provider,
                "daily-release-v1": daily_provider,
            }[release_id]

        async def _snapshot_provider(snapshot_id: str) -> None:
            del snapshot_id
            return None

        loader = FrozenInputLoader(
            release_provider_factory=_release_factory,  # type: ignore[arg-type]
            snapshot_provider=_snapshot_provider,
        )
        spec = build_strategy_template(
            "ma_cross",
            strategy_id="ma_cross_test",
            dataset_release_ids=("bars-release-v1", "daily-release-v1"),
        )
        manifest = ResearchRunManifest(
            run_id="RR-multireleasetest001",
            idempotency_key="multi-release-test-001",
            strategy_spec=spec,
            strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
            dataset_releases=(
                FrozenArtifactRef(
                    artifact_id="bars-release-v1",
                    version="v1",
                    checksum="a" * 64,
                    capabilities=("stock",),
                ),
                FrozenArtifactRef(
                    artifact_id="daily-release-v1",
                    version="v1",
                    checksum="b" * 64,
                    capabilities=("stock",),
                ),
            ),
            code_version="abcdef0123456789",
            initial_capital=Decimal("100000"),
            requested_by="unit-test",
        )
        ctx = await loader.load_context(
            manifest,
            decision_at=datetime(2024, 2, 29, 16, 0, tzinfo=UTC),
            execution_at=datetime(2024, 3, 1, 9, 30, tzinfo=UTC),
        )
        # bars 主发布仍提供候选池与价格。
        assert ctx.candidates
        assert ctx.candidates[0].symbol == "600519.SH"
        assert "600519.SH" in ctx.prices
        # daily_metrics 研究发布被融合进特征:pbm / market_cap / turnover_rate。
        feature_ids = {item.feature_id for item in ctx.features}
        assert "pb" in feature_ids
        assert "market_cap" in feature_ids
        assert "turnover_rate" in feature_ids

    async def test_requires_exactly_one_bars_release(self) -> None:
        # 只有研究数据发布、没有 bars 主发布 → fail-closed。
        from finboard_data.releases import ReleaseDatasetKind as _Kind

        daily_release = _StubRelease(
            "daily-only",
            (),
            dataset_kind=_Kind.DAILY_METRICS,
        )
        daily_provider = _StubProvider(release=daily_release, close_by_symbol={})
        async def _fetch_daily(symbol: object, **_: object) -> list[object]:
            del symbol
            return []
        daily_provider.fetch_daily_metrics = _fetch_daily  # type: ignore[attr-defined]

        def _release_factory(release_id: str) -> _StubProvider:
            assert release_id == "daily-only"
            return daily_provider

        async def _snapshot_provider(snapshot_id: str) -> None:
            del snapshot_id
            return None

        loader = FrozenInputLoader(
            release_provider_factory=_release_factory,  # type: ignore[arg-type]
            snapshot_provider=_snapshot_provider,
        )
        spec = build_strategy_template(
            "ma_cross",
            strategy_id="ma_cross_test",
            dataset_release_ids=("daily-only",),
        )
        manifest = ResearchRunManifest(
            run_id="RR-nobarsrelease0001",
            idempotency_key="no-bars-release-001",
            strategy_spec=spec,
            strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
            dataset_releases=(
                FrozenArtifactRef(
                    artifact_id="daily-only",
                    version="v1",
                    checksum="a" * 64,
                    capabilities=("stock",),
                ),
            ),
            code_version="abcdef0123456789",
            initial_capital=Decimal("100000"),
            requested_by="unit-test",
        )
        with pytest.raises(ValueError, match="bars 主发布"):
            await loader.load_context(
                manifest,
                decision_at=datetime(2024, 2, 29, 16, 0, tzinfo=UTC),
                execution_at=datetime(2024, 3, 1, 9, 30, tzinfo=UTC),
            )
