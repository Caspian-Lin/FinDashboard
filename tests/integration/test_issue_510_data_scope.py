"""统一数据范围解析 / 预览 / 提交复验(issue #510,需 PostgreSQL)。

真实链路:instruments 元数据 + trade_cal + 两份 bars 主发布 → 解析窗口与
发布选择 → REST 预览/复验 → 提交期 ``apply_scope_to_parameters`` 复验并
注入 ``research_window``;预览之后上线新发布必须 409 漂移而不是静默换最新。
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.routes.data_scopes import router as data_scopes_router
from finboard_api.routes.research_runs import router as research_runs_router
from finboard_backtest.data_scope import (
    DataScopeSpec,
    ScopeGateError,
    ScopeResolutionError,
    apply_scope_to_parameters,
    resolve_data_scope,
)
from finboard_backtest.research_run.contracts import manifest_from_json
from finboard_backtest.strategy_spec import (
    build_strategy_template,
    compile_registered_strategy_spec,
)
from finboard_backtest.strategy_spec.contracts import UniverseRanking
from finboard_data import AssetCapability, CapabilityStatus
from finboard_data.releases import (
    RELEASE_FIELDS,
    ReleaseDatasetKind,
    ResearchDatasetRelease,
)
from finboard_persistence import (
    ResearchRunRepository,
    ResearchStrategySpecRepository,
    TradeCalRepository,
    session_factory,
)
from finboard_persistence.dataset_release_repo import ResearchDatasetReleaseRepository
from finboard_persistence.models import InstrumentModel, TradeCalModel
from finboard_shared.types import (
    BarPeriod,
    DatasetQualityStatus,
    InstrumentType,
    Market,
)
from tests.integration.test_issue_355_snapshot_anchor_hint import _instrument
from tests.integration.test_research_run_signal_engine_worker import SYMBOLS

pytestmark = pytest.mark.asyncio

BARS_V1 = "DR-bars-510-v1"
BARS_V2 = "DR-bars-510-v2"
DAILY_V1 = "DR-daily-510-v1"
STRATEGY_ID = "ma_cross_scope_510"
WINDOW_START = date(2024, 1, 2)
WINDOW_END = date(2024, 1, 31)
CAL_DAYS = [
    date(2024, 1, 1) + timedelta(days=offset)
    for offset in range(0, 45)
    if (date(2024, 1, 1) + timedelta(days=offset)).weekday() < 5
]
#: 日历漂移用例专用:不放进 fixture 的基准日历(否则会改变其他用例的预热基数)。
EXTRA_CAL_DAY = date(2023, 12, 29)


@pytest_asyncio.fixture(scope="module")
async def engine(_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    yield _engine


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> AsyncIterator[None]:
    async with engine.begin() as conn:
        for table in (
            "research_run_artifacts",
            "research_runs",
            "background_jobs",
            "factor_feature_snapshots",
            "research_strategy_specs",
            "research_dataset_release_instruments",
            "research_dataset_releases",
        ):
            await conn.execute(text(f"DELETE FROM {table}"))
        await conn.execute(
            delete(InstrumentModel).where(
                InstrumentModel.code.in_(list(SYMBOLS))
            )
        )
    yield
    async with engine.begin() as conn:
        await conn.execute(
            delete(TradeCalModel).where(
                TradeCalModel.exchange.in_(("SSE", "SZSE")),
                TradeCalModel.cal_date.in_([*CAL_DAYS, EXTRA_CAL_DAY]),
            )
        )
        await conn.execute(
            delete(InstrumentModel).where(
                InstrumentModel.code.in_(list(SYMBOLS))
            )
        )


@pytest_asyncio.fixture
async def seeded(engine: AsyncEngine) -> AsyncEngine:
    """instruments + trade_cal + v1 bars 发布。"""

    async with session_factory(engine)() as session:
        session.add_all(
            [
                InstrumentModel(
                    code=code,
                    name=code,
                    market=Market.A_SHARE.value,
                    instrument_type=InstrumentType.STOCK.value,
                    exchange="SSE",
                    listing_board="main",
                    status="active",
                    list_date=date(2020, 1, 1),
                )
                for code in SYMBOLS
            ]
        )
        await session.commit()
        await TradeCalRepository(session).upsert_trading_days(
            set(CAL_DAYS), source="issue510"
        )
        await session.commit()
        await _publish_bars(session, BARS_V1, published_at=datetime(2024, 1, 1, tzinfo=UTC))
        await _publish_daily(session, DAILY_V1)
    return engine


async def _publish_daily(
    session: AsyncSession,
    release_id: str,
    *,
    symbols: tuple[str, ...] = SYMBOLS,
) -> None:
    """研究数据发布(daily_metrics):只提供因子观测,不参与候选池口径。"""

    release = ResearchDatasetRelease(
        release_id=release_id,
        dataset_name=f"{release_id}-daily",
        source="issue510",
        version="v1",
        schema_version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 2, 29),
        period=BarPeriod.D1,
        adjustment="none",
        fields=("trade_date", "close", "amount", "vol"),
        availability_rules=(("daily_metrics", "available_at <= decision_at"),),
        code_version="abcdef0123456789",
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
        instruments=tuple(_instrument(code) for code in symbols),
        capabilities=(),
        quality_status=DatasetQualityStatus.PASSED,
        quality_report={"release_coverage": "1.0"},
        dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        release_checksum=hashlib.sha256(release_id.encode()).hexdigest(),
    )
    await ResearchDatasetReleaseRepository(session).publish(release)
    await session.commit()


async def _publish_bars(
    session: AsyncSession, release_id: str, *, published_at: datetime
) -> None:
    release = ResearchDatasetRelease(
        release_id=release_id,
        dataset_name=f"{release_id}-bars",
        source="issue510",
        version="v1",
        schema_version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 2, 29),
        period=BarPeriod.D1,
        adjustment="qfq",
        fields=RELEASE_FIELDS,
        availability_rules=(("instrument_metadata", "available_at <= decision_at"),),
        code_version="abcdef0123456789",
        published_at=published_at,
        instruments=tuple(_instrument(code) for code in SYMBOLS),
        capabilities=(
            AssetCapability(
                key="stock",
                status=CapabilityStatus.READY,
                symbol_count=len(SYMBOLS),
                ready_count=len(SYMBOLS),
            ),
        ),
        quality_status=DatasetQualityStatus.PASSED,
        quality_report={"release_coverage": "1.0"},
        dataset_kind=ReleaseDatasetKind.BARS,
        release_checksum=hashlib.sha256(release_id.encode()).hexdigest(),
    )
    await ResearchDatasetReleaseRepository(session).publish(release)
    await session.commit()


def _scope(**overrides: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "universe": {
            "explicit_symbols": list(SYMBOLS),
            "filters": {"min_listing_days": 60},
        },
        "decision": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "warmup_sessions": 0,
        "tail_sessions": 0,
        "datasets": {"required": ["bars"]},
        "bars_adjustment": "qfq",
    }
    payload.update(overrides)
    return payload


async def _publish_spec(
    engine: AsyncEngine,
    *,
    release_ids: tuple[str, ...] = (BARS_V1,),
    strategy_id: str = STRATEGY_ID,
) -> None:
    spec = build_strategy_template(
        "ma_cross",
        strategy_id=strategy_id,
        dataset_release_ids=release_ids,
    )
    # 模板默认按 average_amount(每日成交额)排序,而本测试只冻 bars + daily_metrics
    # 发布、没有成交额特征;换成标准价格特征 close,入队预检才能通过(与 scope
    # 解析无关,只为让 run 真正落库)。
    universe = spec.universe.model_copy(
        update={
            "ranking": UniverseRanking(field="close"),
            "required_data_fields": ("price",),
            "min_average_amount": None,
        }
    )
    spec = spec.model_copy(update={"universe": universe})
    plan = compile_registered_strategy_spec(spec, user_factor_sources=frozenset())
    async with session_factory(engine)() as session:
        repo = ResearchStrategySpecRepository(session)
        await repo.create_draft(
            strategy_id=spec.strategy_id,
            schema_version=spec.schema_version,
            name=spec.name,
            strategy_kind=spec.strategy_kind,
            checksum=plan.checksum,
            payload=spec.canonical_payload(),
        )
        await repo.publish(spec.strategy_id, 1, expected_version=1)
        await session.commit()


@pytest_asyncio.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    smaker = session_factory(engine)

    async def _override() -> AsyncIterator[AsyncSession]:
        async with smaker() as session:
            yield session

    app = FastAPI()
    app.include_router(data_scopes_router)
    app.include_router(research_runs_router)
    app.dependency_overrides[get_db_session] = _override
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        yield http_client


class TestResolvePreview:
    async def test_resolves_bars_release_and_window(self, seeded: AsyncEngine) -> None:
        async with session_factory(seeded)() as session:
            resolution = await resolve_data_scope(
                session, DataScopeSpec.model_validate(_scope())
            )
        assert resolution.blockers == ()
        assert resolution.selection_mode == "auto"
        assert [item.artifact_id for item in resolution.releases] == [BARS_V1]
        release = resolution.releases[0]
        assert release.dataset.value == "bars"
        assert release.role == "bars_primary"
        assert release.reason == "窗口覆盖 + 标的齐全"
        window = resolution.window
        assert window.decision_start == WINDOW_START
        assert window.decision_end == WINDOW_END
        assert window.warmup_start == WINDOW_START
        assert window.valuation_end == WINDOW_END
        assert window.calendar_markets == ("SSE",)
        assert resolution.universe.resolved_count == len(SYMBOLS)
        assert resolution.resolution_id.startswith("SC-")

    async def test_locked_mode_requires_listed_release(
        self, seeded: AsyncEngine
    ) -> None:
        async with session_factory(seeded)() as session:
            resolution = await resolve_data_scope(
                session,
                DataScopeSpec.model_validate(
                    _scope(
                        release_selection="locked",
                        locked_release_ids=[BARS_V1],
                    )
                ),
            )
        assert [item.artifact_id for item in resolution.releases] == [BARS_V1]
        assert resolution.selection_mode == "locked"

    async def test_warmup_shortfall_is_warning(self, seeded: AsyncEngine) -> None:
        async with session_factory(seeded)() as session:
            resolution = await resolve_data_scope(
                session,
                DataScopeSpec.model_validate(
                    _scope(
                        decision={
                            "start": "2024-01-05",
                            "end": WINDOW_END.isoformat(),
                        },
                        warmup_sessions=100,
                    )
                ),
            )
        assert resolution.blockers == ()
        assert any("预热不足" in item for item in resolution.warnings)
        assert resolution.window.warmup_sessions == 4

    async def test_two_strategies_referencing_same_release(
        self, seeded: AsyncEngine
    ) -> None:
        await _publish_spec(seeded)
        await _publish_spec(seeded, strategy_id="ma_cross_scope_510_peer")
        async with session_factory(seeded)() as session:
            resolution = await resolve_data_scope(
                session,
                DataScopeSpec.model_validate(_scope()),
                strategy_id=STRATEGY_ID,
                strategy_version=1,
            )
        assert resolution.blockers == ()
        names = {(item.strategy_id, item.version) for item in resolution.affected_strategies}
        assert (STRATEGY_ID, 1) in names
        assert ("ma_cross_scope_510_peer", 1) in names

    async def test_filter_mode_empty_pool_is_blocking(
        self, seeded: AsyncEngine
    ) -> None:
        # 过滤条件解析出 0 只标的必须阻塞(旧实现 requested=0/resolved=0 却
        # blockers 为空,门控会放行 0 标的提交)。
        async with session_factory(seeded)() as session:
            resolution = await resolve_data_scope(
                session,
                DataScopeSpec.model_validate(
                    _scope(universe={"instrument_types": ["etf"]})
                ),
            )
        codes = {item.code for item in resolution.blockers}
        assert "scope_universe_empty" in codes
        assert resolution.universe.resolved_count == 0

    async def test_commodity_does_not_leak_a_share_pool(
        self, seeded: AsyncEngine
    ) -> None:
        # 商品范围映射到 futures 类型;库里只有 A 股 stock → 0 只而不是
        # 「不加过滤把 6 只正股全放进来」。
        async with session_factory(seeded)() as session:
            resolution = await resolve_data_scope(
                session,
                DataScopeSpec.model_validate(
                    _scope(
                        universe={
                            "asset_classes": ["commodity"],
                            "markets": ["a_share"],
                        }
                    )
                ),
            )
        assert resolution.universe.resolved_count == 0
        assert any(
            item.code == "scope_universe_empty" for item in resolution.blockers
        )

    async def test_unmapped_asset_class_is_blocking(
        self, seeded: AsyncEngine
    ) -> None:
        async with session_factory(seeded)() as session:
            resolution = await resolve_data_scope(
                session,
                DataScopeSpec.model_validate(
                    _scope(universe={"asset_classes": ["cash"]})
                ),
            )
        assert any(
            item.code == "scope_asset_class_unmapped" for item in resolution.blockers
        )

    async def test_bars_missing_symbol_is_blocking(
        self, seeded: AsyncEngine
    ) -> None:
        # 多一只不在 bars 发布里的标的:G.H 有元数据但发布没覆盖 → bars 缺
        # 标的必须阻塞(研究发布才容忍缺标的)。
        async with session_factory(seeded)() as session:
            session.add(
                InstrumentModel(
                    code="G.SH",
                    name="G.SH",
                    market=Market.A_SHARE.value,
                    instrument_type=InstrumentType.STOCK.value,
                    exchange="SSE",
                    listing_board="main",
                    status="active",
                    list_date=date(2020, 1, 1),
                )
            )
            await session.commit()
        try:
            async with session_factory(seeded)() as session:
                resolution = await resolve_data_scope(
                    session,
                    DataScopeSpec.model_validate(
                        _scope(universe={"explicit_symbols": [*SYMBOLS, "G.SH"]})
                    ),
                )
        finally:
            async with session_factory(seeded)() as session:
                await session.execute(
                    delete(InstrumentModel).where(InstrumentModel.code == "G.SH")
                )
                await session.commit()
        codes = {item.code for item in resolution.blockers}
        assert "scope_release_no_match" in codes
        assert resolution.releases == ()

    async def test_research_release_missing_symbols_is_tolerated(
        self, seeded: AsyncEngine
    ) -> None:
        async with session_factory(seeded)() as session:
            await _publish_daily(session, "DR-daily-510-partial", symbols=SYMBOLS[:5])
        async with session_factory(seeded)() as session:
            resolution = await resolve_data_scope(
                session,
                DataScopeSpec.model_validate(
                    _scope(
                        datasets={"required": ["bars", "daily_metrics"]},
                        release_selection="locked",
                        locked_release_ids=[BARS_V1, "DR-daily-510-partial"],
                    )
                ),
            )
        daily = next(
            item for item in resolution.releases if item.dataset.value == "daily_metrics"
        )
        assert "缺 1 只标的" in daily.reason
        assert resolution.gaps.missing_in_research["daily_metrics"] == (SYMBOLS[-1],)
        assert any("研究数据发布 daily_metrics 缺 1 只标的" in item for item in resolution.warnings)
        assert not any(
            item.code == "scope_release_no_match" for item in resolution.blockers
        )

    async def test_optional_dataset_not_in_frozen_set(
        self, seeded: AsyncEngine
    ) -> None:
        # optional 只观测、不进入冻结发布集合(否则每次提交都因集合不等 409)。
        async with session_factory(seeded)() as session:
            resolution = await resolve_data_scope(
                session,
                DataScopeSpec.model_validate(
                    _scope(datasets={"required": ["bars"], "optional": ["daily_metrics"]})
                ),
            )
        assert [item.dataset.value for item in resolution.releases] == ["bars"]
        assert any(
            item.dataset.value == "daily_metrics" for item in resolution.alternatives
        )

    async def test_two_locked_bars_is_ambiguous(
        self, seeded: AsyncEngine
    ) -> None:
        async with session_factory(seeded)() as session:
            await _publish_bars(
                session, BARS_V2, published_at=datetime(2024, 2, 1, tzinfo=UTC)
            )
        async with session_factory(seeded)() as session:
            resolution = await resolve_data_scope(
                session,
                DataScopeSpec.model_validate(
                    _scope(
                        release_selection="locked",
                        locked_release_ids=[BARS_V2, BARS_V1],
                    )
                ),
            )
        assert any(
            item.code == "scope_locked_bars_ambiguous" for item in resolution.blockers
        )

    async def test_explicit_symbols_do_not_bypass_class_mapping(
        self, seeded: AsyncEngine
    ) -> None:
        # explicit_symbols 早退也不能跳过未映射大类校验(声明无效即拒绝)。
        async with session_factory(seeded)() as session:
            resolution = await resolve_data_scope(
                session,
                DataScopeSpec.model_validate(
                    _scope(
                        universe={
                            "explicit_symbols": list(SYMBOLS),
                            "asset_classes": ["cash"],
                        }
                    )
                ),
            )
        assert resolution.universe.resolved_count == 0
        assert any(
            item.code == "scope_asset_class_unmapped" for item in resolution.blockers
        )

    async def test_locked_without_bars_does_not_report_ambiguity(
        self, seeded: AsyncEngine
    ) -> None:
        async with session_factory(seeded)() as session:
            await _publish_daily(session, "DR-daily-510-only")
        async with session_factory(seeded)() as session:
            resolution = await resolve_data_scope(
                session,
                DataScopeSpec.model_validate(
                    _scope(
                        datasets={"required": ["bars"]},
                        release_selection="locked",
                        locked_release_ids=["DR-daily-510-only"],
                    )
                ),
            )
        codes = {item.code for item in resolution.blockers}
        assert "scope_locked_bars_ambiguous" not in codes
        assert "scope_locked_release_missing" in codes

    async def test_benchmark_outside_bars_is_blocking(
        self, seeded: AsyncEngine
    ) -> None:
        async with session_factory(seeded)() as session:
            resolution = await resolve_data_scope(
                session,
                DataScopeSpec.model_validate(
                    _scope(benchmarks=["000300.SH"])
                ),
            )
        codes = {item.code for item in resolution.blockers}
        assert "scope_benchmark_not_in_bars" in codes

    async def test_missing_calendar_is_named_error(
        self, engine: AsyncEngine
    ) -> None:
        scope = DataScopeSpec.model_validate(
            _scope(universe={"explicit_symbols": list(SYMBOLS), "markets": ["hk"]})
        )
        async with session_factory(engine)() as session:
            with pytest.raises(ScopeResolutionError) as excinfo:
                await resolve_data_scope(session, scope)
        assert excinfo.value.code == "scope_calendar_unavailable"


class TestSubmitGate:
    async def test_apply_merges_window_and_freezes_payload(
        self, seeded: AsyncEngine
    ) -> None:
        await _publish_spec(seeded)
        async with session_factory(seeded)() as session:
            scope = DataScopeSpec.model_validate(_scope(tail_sessions=1))
            resolution = await resolve_data_scope(
                session,
                scope,
                strategy_id=STRATEGY_ID,
                strategy_version=1,
            )
            assert resolution.blockers == ()
            parameters, payload = await apply_scope_to_parameters(
                session,
                scope,
                resolution_id=resolution.resolution_id,
                release_ids=[BARS_V1],
                strategy_id=STRATEGY_ID,
                strategy_version=1,
                parameters={"rebalance_frequency": "monthly"},
            )
        assert parameters["rebalance_frequency"] == "monthly"
        assert parameters["research_window"] == {
            "warmup_start": WINDOW_START.isoformat(),
            "decision_start": WINDOW_START.isoformat(),
            "decision_end": WINDOW_END.isoformat(),
            "valuation_end": "2024-02-01",
        }
        assert payload["resolution_id"] == resolution.resolution_id
        assert payload["releases"][0]["artifact_id"] == BARS_V1
        assert payload["scope"]["datasets"]["required"] == ["bars"]

    async def test_new_release_after_preview_is_drift(
        self, seeded: AsyncEngine
    ) -> None:
        await _publish_spec(seeded)
        async with session_factory(seeded)() as session:
            scope = DataScopeSpec.model_validate(_scope())
            preview = await resolve_data_scope(
                session, scope, strategy_id=STRATEGY_ID, strategy_version=1
            )
        # 预览之后上线更新的一份覆盖发布(auto 模式会改选它)。
        async with session_factory(seeded)() as session:
            await _publish_bars(
                session, BARS_V2, published_at=datetime(2024, 2, 1, tzinfo=UTC)
            )
        async with session_factory(seeded)() as session:
            with pytest.raises(ScopeGateError) as excinfo:
                await apply_scope_to_parameters(
                    session,
                    scope,
                    resolution_id=preview.resolution_id,
                    release_ids=[BARS_V1],
                    strategy_id=STRATEGY_ID,
                    strategy_version=1,
                    parameters={},
                )
        assert excinfo.value.code == "scope_release_drift"
        assert excinfo.value.status_code == 409
        assert excinfo.value.detail["actual_release_ids"] == [BARS_V2]

    async def test_window_conflict_rejected(self, seeded: AsyncEngine) -> None:
        await _publish_spec(seeded)
        async with session_factory(seeded)() as session:
            scope = DataScopeSpec.model_validate(_scope())
            preview = await resolve_data_scope(
                session, scope, strategy_id=STRATEGY_ID, strategy_version=1
            )
            with pytest.raises(ScopeGateError) as excinfo:
                await apply_scope_to_parameters(
                    session,
                    scope,
                    resolution_id=preview.resolution_id,
                    release_ids=[BARS_V1],
                    strategy_id=STRATEGY_ID,
                    strategy_version=1,
                    parameters={
                        "research_window": {
                            "warmup_start": "2020-01-01",
                            "decision_start": "2020-01-02",
                            "decision_end": "2020-01-03",
                            "valuation_end": "2020-01-04",
                        }
                    },
                )
        assert excinfo.value.code == "scope_window_conflict"

    async def test_calendar_change_after_preview_is_drift(
        self, seeded: AsyncEngine
    ) -> None:
        """预览后补齐更早的交易日 → 预热窗口变化 → 旧 resolution_id 必须 409。"""

        await _publish_spec(seeded)
        async with session_factory(seeded)() as session:
            scope = DataScopeSpec.model_validate(
                _scope(
                    decision={"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
                    warmup_sessions=2,
                )
            )
            preview = await resolve_data_scope(
                session, scope, strategy_id=STRATEGY_ID, strategy_version=1
            )
            assert preview.blockers == ()
        async with session_factory(seeded)() as session:
            await TradeCalRepository(session).upsert_trading_days(
                {EXTRA_CAL_DAY}, source="issue510"
            )
            await session.commit()
        async with session_factory(seeded)() as session:
            with pytest.raises(ScopeGateError) as excinfo:
                await apply_scope_to_parameters(
                    session,
                    scope,
                    resolution_id=preview.resolution_id,
                    release_ids=[BARS_V1],
                    strategy_id=STRATEGY_ID,
                    strategy_version=1,
                    parameters={},
                )
        assert excinfo.value.code == "scope_release_drift"
        assert excinfo.value.detail["actual_resolution_id"] != preview.resolution_id

    async def test_multi_period_without_tail_rejected(
        self, seeded: AsyncEngine
    ) -> None:
        """multi_period + tail=0 在入队期就拒绝,不把失败推迟到执行期。"""

        await _publish_spec(seeded)
        async with session_factory(seeded)() as session:
            scope = DataScopeSpec.model_validate(_scope())
            preview = await resolve_data_scope(
                session, scope, strategy_id=STRATEGY_ID, strategy_version=1
            )
            assert preview.window.valuation_end == preview.window.decision_end
            with pytest.raises(ScopeGateError) as excinfo:
                await apply_scope_to_parameters(
                    session,
                    scope,
                    resolution_id=preview.resolution_id,
                    release_ids=[BARS_V1],
                    strategy_id=STRATEGY_ID,
                    strategy_version=1,
                    parameters={"rebalance_frequency": "monthly"},
                )
        assert excinfo.value.code == "scope_window_tail_too_short"

    async def test_spec_release_mismatch_is_blocking(self, seeded: AsyncEngine) -> None:
        # 策略冻结的发布集合与 scope 解析出的不同 → 复用 resolver 既有守卫。
        await _publish_spec(seeded, release_ids=("DR-other-release",))
        async with session_factory(seeded)() as session:
            resolution = await resolve_data_scope(
                session,
                DataScopeSpec.model_validate(_scope()),
                strategy_id=STRATEGY_ID,
                strategy_version=1,
            )
        assert any(item.code == "spec_release_mismatch" for item in resolution.blockers)


class TestQueueRun:
    def _scope_payload(self) -> dict[str, Any]:
        return _scope(
            datasets={"required": ["bars", "daily_metrics"]},
            # multi_period 需要下一交易日撮合,尾部至少 1 天。
            tail_sessions=1,
        )

    def _queue_payload(self, resolution_id: str, key: str) -> dict[str, Any]:
        return {
            "idempotency_key": key,
            "strategy_id": STRATEGY_ID,
            "strategy_version": 1,
            "dataset_release_ids": [BARS_V1, DAILY_V1],
            "factor_snapshot_ids": [],
            "parameters": {"rebalance_frequency": "monthly"},
            "code_version": "abcdef0123456789",
            "initial_capital": "200000",
            "requested_by": "integration-test",
            "data_scope": self._scope_payload(),
            "data_scope_resolution_id": resolution_id,
        }

    async def test_queue_freezes_scope_and_research_window(
        self, seeded: AsyncEngine, client: httpx.AsyncClient
    ) -> None:
        await _publish_spec(seeded, release_ids=(BARS_V1, DAILY_V1))
        preview = await client.post(
            "/api/data/scopes/preview", json={"scope": self._scope_payload()}
        )
        assert preview.status_code == 200, preview.text
        resolution_id = preview.json()["resolution_id"]
        response = await client.post(
            "/api/research/runs",
            json=self._queue_payload(resolution_id, "scope-510-queue-1"),
        )
        assert response.status_code == 201, response.text
        run_id = response.json()["run_id"]
        async with session_factory(seeded)() as session:
            row = await ResearchRunRepository(session).get(run_id)
        assert row is not None
        manifest = manifest_from_json(row.manifest)
        assert manifest.data_scope is not None
        assert manifest.data_scope["resolution_id"] == resolution_id
        assert manifest.parameters["research_window"] == {
            "warmup_start": WINDOW_START.isoformat(),
            "decision_start": WINDOW_START.isoformat(),
            "decision_end": WINDOW_END.isoformat(),
            "valuation_end": "2024-02-01",
        }
        # 旧入口(不带 scope)仍然可用,manifest 不含 data_scope 键。
        legacy = self._queue_payload(resolution_id, "scope-510-queue-2")
        legacy.pop("data_scope")
        legacy.pop("data_scope_resolution_id")
        legacy_response = await client.post("/api/research/runs", json=legacy)
        assert legacy_response.status_code == 201, legacy_response.text
        async with session_factory(seeded)() as session:
            legacy_row = await ResearchRunRepository(session).get(
                legacy_response.json()["run_id"]
            )
        assert legacy_row is not None
        assert manifest_from_json(legacy_row.manifest).data_scope is None

    async def test_mcp_queue_uses_same_gate(
        self, seeded: AsyncEngine, client: httpx.AsyncClient
    ) -> None:
        """MCP 入队(#513 起共用同一提交服务)与 REST 同 scope 门控语义。"""

        from finboard_api.research_run_schemas import ResearchRunQueueIn
        from finboard_app.research_operation import (
            ResearchOperationContext,
            ResearchOperationError,
        )
        from finboard_app.research_run_submission import ResearchRunSubmissionService

        await _publish_spec(seeded, release_ids=(BARS_V1, DAILY_V1))
        preview = await client.post(
            "/api/data/scopes/preview", json={"scope": self._scope_payload()}
        )
        resolution_id = preview.json()["resolution_id"]
        body = ResearchRunQueueIn.model_validate(
            self._queue_payload(resolution_id, "scope-510-mcp-1")
        )
        async with session_factory(seeded)() as session:
            report = await ResearchRunSubmissionService(session).preflight(
                body, ResearchOperationContext.for_mcp()
            )
        manifest = report.manifest
        assert manifest.data_scope is not None
        assert manifest.data_scope["resolution_id"] == resolution_id
        research_window = manifest.parameters["research_window"]
        assert isinstance(research_window, dict)
        assert research_window["decision_start"] == WINDOW_START.isoformat()
        # 漂移:旧 resolution_id + 新发布 → 同一门控的 conflict 语义。
        async with session_factory(seeded)() as session:
            await _publish_bars(
                session, BARS_V2, published_at=datetime(2024, 2, 1, tzinfo=UTC)
            )
        async with session_factory(seeded)() as session:
            with pytest.raises(ResearchOperationError) as excinfo:
                await ResearchRunSubmissionService(session).preflight(
                    body, ResearchOperationContext.for_mcp()
                )
        assert excinfo.value.mcp_kind == "conflict"
        assert excinfo.value.code == "scope_release_drift"

    async def test_stale_resolution_id_is_409(
        self, seeded: AsyncEngine, client: httpx.AsyncClient
    ) -> None:
        await _publish_spec(seeded, release_ids=(BARS_V1, DAILY_V1))
        preview = await client.post(
            "/api/data/scopes/preview", json={"scope": self._scope_payload()}
        )
        resolution_id = preview.json()["resolution_id"]
        async with session_factory(seeded)() as session:
            await _publish_bars(
                session, BARS_V2, published_at=datetime(2024, 2, 1, tzinfo=UTC)
            )
        response = await client.post(
            "/api/research/runs",
            json=self._queue_payload(resolution_id, "scope-510-queue-3"),
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "scope_release_drift"

    async def test_missing_resolution_id_rejected(
        self, seeded: AsyncEngine, client: httpx.AsyncClient
    ) -> None:
        await _publish_spec(seeded, release_ids=(BARS_V1, DAILY_V1))
        payload = self._queue_payload("SC-placeholder", "scope-510-queue-4")
        payload["data_scope_resolution_id"] = None
        response = await client.post("/api/research/runs", json=payload)
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "scope_resolution_required"


class TestRest:
    async def test_preview_endpoint(self, seeded: AsyncEngine, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/api/data/scopes/preview", json={"scope": _scope()}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["resolution_id"].startswith("SC-")
        assert body["window"]["decision_start"] == WINDOW_START.isoformat()
        assert body["releases"][0]["artifact_id"] == BARS_V1
        assert body["blockers"] == []
        assert body["selection_mode"] == "auto"

    async def test_validate_reports_valid_then_drift(
        self, seeded: AsyncEngine, client: httpx.AsyncClient
    ) -> None:
        preview = await client.post(
            "/api/data/scopes/preview", json={"scope": _scope()}
        )
        resolution_id = preview.json()["resolution_id"]
        ok = await client.post(
            "/api/data/scopes/validate",
            json={
                "scope": _scope(),
                "resolution_id": resolution_id,
                "dataset_release_ids": [BARS_V1],
            },
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["valid"] is True
        assert ok.json()["drift"] is None
        async with session_factory(seeded)() as session:
            await _publish_bars(
                session, BARS_V2, published_at=datetime(2024, 2, 1, tzinfo=UTC)
            )
        drifted = await client.post(
            "/api/data/scopes/validate",
            json={
                "scope": _scope(),
                "resolution_id": resolution_id,
                "dataset_release_ids": [BARS_V1],
            },
        )
        assert drifted.status_code == 200, drifted.text
        payload = drifted.json()
        assert payload["valid"] is False
        assert payload["drift"]["actual_release_ids"] == [BARS_V2]

    async def test_existing_spec_mode_requires_ids(
        self, seeded: AsyncEngine, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/api/data/scopes/preview",
            json={"scope": _scope(), "mode": "existing_spec"},
        )
        assert response.status_code == 422

    async def test_get_by_resolution_id_is_404(
        self, seeded: AsyncEngine, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/api/data/scopes/SC-0123456789abcdef")
        assert response.status_code == 404
        assert "重新" in response.json()["detail"]
