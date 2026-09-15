"""``user_code`` 策略沙箱接入 research run 的集成测试(issue #218)。

fake 沙箱调用方注入(替换 ``StrategySandboxCaller``),验证:

* multi_period 全链路:queue(双写)→ worker → UserCodeStrategyAdapter
  (stub 冻结发布 + fake decide)→ Coordinator 13 stage x N 决策 + report;
* 决策输入含**当前权重回显**(第 2 决策起 decide 看到上一决策成交后的
  实际持仓权重;第 1 决策为空);
* 越权处理:decide 输出候选池外标的被丢弃(run 不失败);
* report 归档 ``sandbox_provenance``(code commit + 镜像 digest + 逐决策
  targets checksum);
* 同期 multi_factor 规格同屏可比(决策数 / 曲线形状 / 指标字段对齐);
* decide 失败 → run FAILED 且原因可查询。

真实容器 E2E(布林带式均值回归样例)见
``test_research_sandbox_docker_e2e.py``(FINBOARD_SANDBOX_E2E=1 门控)。
依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。不连 broker / 不下实盘单。
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_backtest.research_run import (
    FrozenArtifactRef,
    ResearchRunManifest,
    ResearchRunStatus,
    stable_checksum,
)
from finboard_backtest.research_run.user_code_engine import UserCodeStrategyAdapter
from finboard_backtest.research_sandbox.strategy_exec import (
    StrategyDecisionOutcome,
    UserCodeExecutionError,
)
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_backtest.strategy_spec.contracts import (
    FeatureGraph,
    SignalRules,
    StrategyCodeArtifactRef,
)
from finboard_persistence import (
    BackgroundJobRepository,
    ResearchRunRepository,
    create_async_engine,
    session_factory,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
)
from tests.integration.test_research_run_signal_engine_worker import (
    SYMBOLS,
    _build_worker,
    _drain_worker,
    _multi_period_factory,
    _multi_period_manifest,
    _multi_period_provider,
    _queue_double_write,
)

pytestmark = pytest.mark.asyncio

CODE_COMMIT = "f" * 40
IMAGE_DIGEST = "sha256:" + "9" * 64


@pytest_asyncio.fixture(scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    from finboard_persistence import Base
    from tests.integration.conftest import TEST_DB_URL, ensure_test_db

    await ensure_test_db(TEST_DB_URL)
    eng = create_async_engine(TEST_DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM research_run_artifacts"))
        await conn.execute(text("DELETE FROM research_runs"))
        await conn.execute(text("DELETE FROM background_jobs"))


# ---- fake 沙箱调用方(替换 StrategySandboxCaller,行为面一致) -----------------


@dataclass
class FakeSandboxCaller:
    """记录调用并按脚本产出目标权重的假沙箱(decide 语义一致)。"""

    weights_fn: Callable[[int, tuple[str, ...], dict[str, float]], dict[str, float]]
    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_on: int | None = None
    image: str = "finboard-research-sandbox:test"
    image_digest: str = IMAGE_DIGEST
    commit: str = CODE_COMMIT

    @classmethod
    def create(
        cls,
        *,
        settings: Any,
        artifact_name: str,
        commit: str | None,
        release_provider_factory: Callable[[str], Any],
        dataset_release_ids: Any,
        run_id: str,
        params: Any = None,
    ) -> FakeSandboxCaller:
        raise AssertionError("由 monkeypatch 工厂直接构造实例,不应走 create")

    async def decide(
        self,
        *,
        decision_index: int,
        decision_at: datetime,
        symbols: tuple[str, ...],
        current_weights: Any,
        strategy_constraints: Any,
    ) -> StrategyDecisionOutcome:
        self.calls.append(
            {
                "index": decision_index,
                "decision_at": decision_at,
                "symbols": tuple(symbols),
                "current_weights": dict(current_weights),
                "strategy_constraints": dict(strategy_constraints),
            }
        )
        if self.fail_on is not None and decision_index == self.fail_on:
            raise UserCodeExecutionError("runtime_error", "策略代码运行时异常: boom")
        weights = self.weights_fn(decision_index, symbols, dict(current_weights))
        record = {
            "index": decision_index,
            "decision_at": decision_at.isoformat(),
            "mount_manifest_checksum": "m" * 64,
            "targets_checksum": "t" * 64,
            "n_targets": len(weights),
        }
        return StrategyDecisionOutcome(weights=weights, record=record)

    def provenance_header(self, *, mode: str, decision_count: int) -> dict[str, Any]:
        return {
            "artifact_name": "mean_reversion_zscore",
            "kind": "strategy",
            "commit": self.commit,
            "code_checksum": "c" * 16,
            "image": self.image,
            "image_digest": self.image_digest,
            "execution_mode": mode,
            "decision_count": decision_count,
        }


def _patch_caller(monkeypatch: pytest.MonkeyPatch, caller: FakeSandboxCaller) -> None:
    import finboard_backtest.research_run.user_code_engine as module

    monkeypatch.setattr(module, "StrategySandboxCaller", SimpleNamespace(
        create=FakeSandboxCaller.create
    ))
    # 工厂侧直接构造适配器并注入 caller(_ensure_loaded 会跳过 create)。
    async def _ensure(self: UserCodeStrategyAdapter) -> tuple[Any, Any]:
        if self._context_stream is None:
            from finboard_backtest.research_run.signal_engine import (
                iter_decision_load_contexts,
            )

            self._context_stream = iter_decision_load_contexts(
                self._manifest,
                release_provider_factory=self._release_provider_factory,
                snapshot_provider=self._snapshot_provider,
            )
            self._sandbox = caller  # type: ignore[assignment]
        return self._context_stream, self._sandbox

    monkeypatch.setattr(UserCodeStrategyAdapter, "_ensure_loaded", _ensure)


def _user_code_factory(
    provider: Any, caller: FakeSandboxCaller, monkeypatch: pytest.MonkeyPatch
) -> Callable[[ResearchRunManifest], UserCodeStrategyAdapter]:
    _patch_caller(monkeypatch, caller)

    def factory(manifest: ResearchRunManifest) -> UserCodeStrategyAdapter:
        def release_factory(release_id: str) -> Any:
            assert release_id == "frozen-release-multi"
            return provider

        async def snapshot_provider(snapshot_id: str) -> None:
            return None

        return UserCodeStrategyAdapter(
            manifest=manifest,
            release_provider_factory=release_factory,
            snapshot_provider=snapshot_provider,
            settings_factory=lambda: SimpleNamespace(
                research_sandbox_enabled=True,
                research_sandbox_image="finboard-research-sandbox:test",
                research_sandbox_repo_path="unused",
            ),
        )

    return factory


def _user_code_manifest(suffix: str) -> ResearchRunManifest:
    spec = build_strategy_template(
        "multi_factor",
        strategy_id=f"user_code_{suffix}",
        dataset_release_ids=("frozen-release-multi",),
    ).model_copy(
        update={
            "strategy_kind": "user_code",
            "feature_graph": FeatureGraph(nodes=(), outputs=()),
            "signal_rules": SignalRules(rules=()),
            "code_artifact": StrategyCodeArtifactRef(
                name="mean_reversion_zscore", commit=CODE_COMMIT
            ),
        }
    )
    return ResearchRunManifest(
        run_id=_run_id(f"user-code-{suffix}"),
        idempotency_key=f"user-code-{suffix}",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id="frozen-release-multi",
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        parameters={"rebalance_frequency": "monthly"},
        code_version="abcdef0123456789",
        initial_capital=Decimal("200000"),
        requested_by="worker-test",
    )


def _run_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"RR-{digest}"


class TestUserCodeMultiPeriodEndToEnd:
    async def test_user_code_run_completes_with_provenance_and_echo(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """全链路:3 决策(建仓→轮动→清仓)+ 权重回显 + provenance 归档。"""

        def weights_fn(
            index: int, symbols: tuple[str, ...], current: dict[str, float]
        ) -> dict[str, float]:
            if index == 0:
                return dict.fromkeys(SYMBOLS[:3], 0.15)
            if index == 1:
                return dict.fromkeys(SYMBOLS[3:], 0.2)
            return {}  # 第 3 期全现金

        caller = FakeSandboxCaller(weights_fn=weights_fn)
        manifest = _user_code_manifest("rotate")
        run_id = await _queue_double_write(engine, manifest)
        worker = _build_worker(
            engine, _user_code_factory(_multi_period_provider(), caller, monkeypatch)
        )
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.COMPLETED.value, (
                f"status={run_row.status} error={run_row.error_code}"
                f"/{run_row.error_summary}"
            )
            result = run_row.result
            assert result is not None
            assert result["execution_mode"] == "multi_period"
            assert result["decision_count"] == 3
            assert result["equity_curve"]
            assert result["sandbox_provenance"] is not None

            # 验收:run / report 归档 code commit 与镜像 digest。
            provenance = cast(dict[str, Any], result["sandbox_provenance"])
            assert provenance["commit"] == CODE_COMMIT
            assert provenance["image_digest"] == IMAGE_DIGEST
            assert provenance["artifact_name"] == "mean_reversion_zscore"
            assert len(provenance["decisions"]) == 3
            assert all(
                item["targets_checksum"] == "t" * 64
                for item in provenance["decisions"]
            )

            artifacts = await ResearchRunRepository(session).list_artifacts(run_id)
        # 3 决策 x 13 stage + report = 40 artifacts。
        assert len(artifacts) == 40

        # 验收:决策输入含当前权重回显 —— 第 1 决策空仓,第 2 决策看到
        # 第 1 决策成交后的持仓权重,第 3 决策看到第 2 期持仓。
        calls = caller.calls
        assert len(calls) == 3
        assert calls[0]["current_weights"] == {}
        second = calls[1]["current_weights"]
        assert set(second) == set(SYMBOLS[:3])
        assert all(0 < weight < 0.25 for weight in second.values())
        third = calls[2]["current_weights"]
        assert set(third) == set(SYMBOLS[3:])

        # 挂载约束视图回显(decide 可见 portfolio_policy 约束)。
        assert calls[0]["strategy_constraints"]["long_only"] is True
        assert calls[0]["strategy_constraints"]["max_weight_per_asset"] > 0

        # 第 3 期全现金 → 期末账本持仓清空(权益 = 现金)。
        assert float(str(result["final_cash"])) == pytest.approx(
            float(str(result["final_equity"])), rel=1e-6
        )

    async def test_multi_factor_same_period_comparable(
        self, engine: AsyncEngine
    ) -> None:
        """验收:同一策略区间 multi_factor 与 user_code 报告同屏可比。"""
        manifest = _multi_period_manifest("uc-compare-mf")
        run_id = await _queue_double_write(engine, manifest)
        worker = _build_worker(
            engine, _multi_period_factory(_multi_period_provider())
        )
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.COMPLETED.value
            result = run_row.result
            assert result is not None
            result = cast(dict[str, Any], result)
            # 与 user_code run 相同的决策数 / 曲线长度 / 指标字段集。
            assert result["decision_count"] == 3
            assert result["execution_mode"] == "multi_period"
            curve = cast(list[dict[str, Any]], result["equity_curve"])
            assert len(curve) == len(
                _multi_period_calendar_days()
            )
            for key in (
                "strategy_return",
                "sharpe_ratio",
                "max_drawdown",
                "annualized_return",
                "final_equity",
            ):
                assert key in result
            # 非 user_code run 无 sandbox_provenance 段。
            assert result.get("sandbox_provenance") is None

    async def test_out_of_universe_targets_dropped_not_failed(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验收:decide 输出候选池外标的 → 丢弃 + warning,run 不失败。"""

        def weights_fn(
            index: int, symbols: tuple[str, ...], current: dict[str, float]
        ) -> dict[str, float]:
            # 4 个正权重保证风险贡献上限可行(1/4 < 0.35,#91 数学约束);
            # 池外标的应被丢弃,负权重由管线 long_only 截断审计。
            weights = dict.fromkeys(SYMBOLS[:4], 0.15)
            weights["ZZZ.SH"] = 0.5  # 池外标的(应被丢弃)
            weights[SYMBOLS[0]] = -0.05  # 负权重
            return weights

        caller = FakeSandboxCaller(weights_fn=weights_fn)
        manifest = _user_code_manifest("outside")
        run_id = await _queue_double_write(engine, manifest)
        worker = _build_worker(
            engine, _user_code_factory(_multi_period_provider(), caller, monkeypatch)
        )
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.COMPLETED.value, (
                f"{run_row.error_code}/{run_row.error_summary}"
            )
            signals_artifacts = [
                item
                for item in await ResearchRunRepository(session).list_artifacts(run_id)
                if item.stage == "signals"
            ]
        symbols_seen: set[str] = set()
        for artifact in signals_artifacts:
            payload = cast(dict[str, Any], artifact.payload)
            signals = cast(list[dict[str, Any]], payload.get("signals", []))
            for signal in signals:
                symbols_seen.add(signal["symbol"])
        assert "ZZZ.SH" not in symbols_seen
        assert symbols_seen <= set(SYMBOLS)

    async def test_decide_failure_fails_run_visibly(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """decide 运行时失败 → run FAILED 且错误可查询(job 同步 FAILED)。"""
        caller = FakeSandboxCaller(weights_fn=lambda i, s, c: {}, fail_on=1)
        manifest = _user_code_manifest("boom")
        run_id = await _queue_double_write(engine, manifest)
        worker = _build_worker(
            engine, _user_code_factory(_multi_period_provider(), caller, monkeypatch)
        )
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.FAILED.value
            assert "boom" in (run_row.error_summary or "")
            assert run_row.job_id is not None
            job_row = await BackgroundJobRepository(session).get(run_row.job_id)
            assert job_row is not None
            assert job_row.status == BackgroundJobStatus.FAILED.value


def _multi_period_calendar_days() -> list[date]:
    days: list[date] = []
    current = date(2024, 1, 1)
    while current <= date(2024, 4, 30):
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days
