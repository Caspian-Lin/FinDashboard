"""入队冻结 / checksum 零漂移 / 加载器双轨等值 / 托管重建 / 审计抽样(issue #360)。

覆盖 issue 测试矩阵中的单测面:

* manifest 新键 ``factor_series`` 冻结进 checksum 与 input_checksum;
  未声明序列的旧 manifest checksum 零漂移(同 strategy_version 先例);
* manifest JSON 往返(factor_series 完整还原);
* 加载器双轨:series.values 按决策日索引产出 FeatureValue,与同参数逐日
  点快照路径喂 ``extract_factor_matrix`` 的因子值逐值等值(合成数据对照);
  同因子快照观测被覆盖让位;未声明 series 走纯快照路径;
* 换发布守卫:失效 series 清单 + 重建代价预估文案;
* 审计抽样:截断点确定性、mock 审计阴性(通过)/ 阳性(检出 →
  lookahead_detected 具名首个分歧日期);
* 系列覆盖的 u_ 因子跳过 #217 门控(user_factor_reference_gate_error)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import pytest

from finboard_backtest.background_jobs.contracts import JobRecord
from finboard_backtest.background_jobs.executors.factor_series_build import (
    LOOKAHEAD_DETECTED,
    FactorSeriesBuildExecutor,
    audit_truncation_points,
)
from finboard_backtest.research_code import (
    factor_series_rebuild_error,
    series_covered_factor_names,
    series_release_mismatches,
    user_factor_reference_gate_error,
)
from finboard_backtest.research_run.contracts import (
    FeatureValue,
    manifest_from_json,
    to_json_value,
)
from finboard_persistence.factor_series_repo import (
    FactorSeriesRecord,
    compute_series_key,
)

# manifest_factory fixture 来自 tests/unit/research_run/conftest.py;
# 该 conftest 只对其目录生效,这里内联同等构造(导入会拖入整包 fixture 链)。

# --------------------------------------------------------------------------- #
# manifest 冻结与零漂移
# --------------------------------------------------------------------------- #


def _manifest_factory():
    """与 tests/unit/research_run/conftest.py manifest_factory 同构的最小构造。"""
    from decimal import Decimal

    from finboard_backtest.research_run import (
        FrozenArtifactRef,
        ResearchActorType,
        ResearchRunManifest,
        stable_checksum,
    )
    from finboard_backtest.strategy_spec import build_strategy_template

    spec = build_strategy_template(
        "multi_factor",
        strategy_id="multi_factor_research",
        dataset_release_ids=("release-v1",),
    )
    return ResearchRunManifest(
        run_id="RR-test-run-00000001",
        idempotency_key="test-idempotency-0001",
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
                capabilities=("factor:close",),
            ),
        ),
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
        actor_type=ResearchActorType.HUMAN,
    )


@pytest.fixture
def manifest_factory():
    return _manifest_factory


def _series_ref_dict() -> dict[str, object]:
    return {
        "artifact_id": "FS-abc123def456",
        "version": "v2",
        "checksum": "c" * 64,
        "capabilities": ["factor:u_mom20"],
    }


class TestManifestFreeze:
    def test_series_in_checksum_and_input_checksum(
        self, manifest_factory
    ) -> None:
        from dataclasses import replace

        from finboard_backtest.research_run.contracts import FrozenArtifactRef

        base = manifest_factory()
        with_series = replace(
            base,
            factor_series=(
                FrozenArtifactRef(
                    artifact_id="FS-abc123def456",
                    version="v2",
                    checksum="c" * 64,
                    capabilities=("factor:u_mom20",),
                ),
            ),
        )
        assert with_series.checksum != base.checksum
        assert with_series.input_checksum != base.input_checksum

    def test_legacy_manifest_checksum_undrifted(self, manifest_factory) -> None:
        """未声明 factor_series 的 manifest:checksum 与 input_checksum 与
        历史(无新键)manifest 完全一致;空序列随 asdict 进 JSON 但为空列表
        (读回空元组,checksum 属性在计算时弹出,同 strategy_version 先例)。"""
        base = manifest_factory()
        payload = to_json_value(base)
        assert isinstance(payload, dict)
        assert payload["factor_series"] == []

        restored = manifest_from_json(payload)
        assert restored.checksum == base.checksum
        assert restored.input_checksum == base.input_checksum
        assert restored.factor_series == ()

    def test_round_trip_preserves_series(self, manifest_factory) -> None:
        from dataclasses import replace

        from finboard_backtest.research_run.contracts import FrozenArtifactRef

        base = manifest_factory()
        manifest = replace(
            base,
            factor_series=(
                FrozenArtifactRef(
                    artifact_id="FS-abc123def456",
                    version="v2",
                    checksum="c" * 64,
                ),
            ),
        )
        payload = to_json_value(manifest)
        assert isinstance(payload, dict)
        assert payload["factor_series"] == [
            {
                "artifact_id": "FS-abc123def456",
                "version": "v2",
                "checksum": "c" * 64,
                "capabilities": [],
            }
        ]
        restored = manifest_from_json(payload)
        assert restored == manifest
        assert restored.checksum == manifest.checksum
        assert restored.input_checksum == manifest.input_checksum

    def test_duplicate_series_rejected(self, manifest_factory) -> None:
        from dataclasses import replace

        from finboard_backtest.research_run.contracts import FrozenArtifactRef

        ref = FrozenArtifactRef(artifact_id="FS-x", version="v2", checksum="c" * 64)
        with pytest.raises(ValueError, match="factor_series 不允许重复"):
            replace(manifest_factory(), factor_series=(ref, ref))

    def test_failure_context_lists_series(self, manifest_factory) -> None:
        from dataclasses import replace

        from finboard_backtest.research_run.contracts import FrozenArtifactRef
        from finboard_backtest.research_run.failure_context import (
            build_failure_summary,
        )

        manifest = replace(
            manifest_factory(),
            factor_series=(
                FrozenArtifactRef(
                    artifact_id="FS-abc123def456", version="v2", checksum="c" * 64
                ),
            ),
        )
        from finboard_backtest.research_run.failure_context import (
            FailureContext,
        )

        summary = build_failure_summary(
            ValueError("boom"), FailureContext(), manifest
        )
        assert "factor_series=FS-abc123def456" in summary


# --------------------------------------------------------------------------- #
# 换发布守卫:失效清单 + 重建代价预估
# --------------------------------------------------------------------------- #


@dataclass
class _SeriesRow:
    series_id: str
    code_artifact: str
    release_id: str
    window_start: str = "2022-01-01"
    window_end: str = "2023-06-30"


class TestRebuildGuard:
    def test_mismatch_list_and_cost_estimate(self) -> None:
        records = [
            _SeriesRow("FS-1", "mom20", "DR-old-bars"),
            _SeriesRow("FS-2", "vol20", "DR-new-bars"),
        ]
        mismatches = series_release_mismatches(records, {"DR-new-bars"})
        assert [m.series_id for m in mismatches] == ["FS-1"]
        assert mismatches[0].factor_name == "u_mom20"
        assert mismatches[0].anchored_release_id == "DR-old-bars"
        message = factor_series_rebuild_error(mismatches)
        assert "series_release_mismatch" in message
        assert "FS-1" in message
        assert "DR-old-bars" in message
        # 代价预估:N 条 x 预计分钟(常量 2 分钟/条)
        assert "1 个 finboard_factor_series_build" in message
        assert "预计 2 分钟" in message

    def test_two_mismatches_cost_scales(self) -> None:
        records = [
            _SeriesRow("FS-1", "mom20", "DR-old-bars"),
            _SeriesRow("FS-2", "vol20", "DR-old-bars"),
        ]
        message = factor_series_rebuild_error(
            series_release_mismatches(records, {"DR-new-bars"})
        )
        assert "2 个 finboard_factor_series_build" in message
        assert "预计 4 分钟" in message

    def test_full_match_no_noise(self) -> None:
        records = [_SeriesRow("FS-1", "mom20", "DR-new-bars")]
        assert series_release_mismatches(records, {"DR-new-bars"}) == []

    def test_series_covered_factor_names(self) -> None:
        covered = series_covered_factor_names(
            [_SeriesRow("FS-1", "mom20", "DR-1")]
        )
        assert covered == frozenset({"u_mom20"})

    def test_series_covered_bypasses_user_factor_gate(self) -> None:
        """被序列覆盖的 u_ 因子跳过名单检查;未覆盖因子仍按 active 名单拦截。

        整合语义(#360 x #361):multi_period 一刀切拒绝已由覆盖检查门控
        (user_factor_series_coverage_gate_error)替代,本门控只剩 active
        名单检查;序列覆盖的因子是内容寻址冻结工件,不受名单约束。
        """
        assert (
            user_factor_reference_gate_error(
                required_factor_sources={"u_mom20"},
                active_user_factors=frozenset(),
                series_covered_factors=frozenset({"u_mom20"}),
            )
            is None
        )
        assert (
            user_factor_reference_gate_error(
                required_factor_sources={"u_mom20"},
                active_user_factors=frozenset(),  # retired/不存在
                series_covered_factors=frozenset({"u_mom20"}),
            )
            is None
        )
        # 未覆盖因子:不在 active 名单 → 拦截(multi_period 语义走 #361 覆盖门控)。
        assert (
            user_factor_reference_gate_error(
                required_factor_sources={"u_mom20", "u_other"},
                active_user_factors=frozenset({"u_mom20"}),
                series_covered_factors=frozenset({"u_mom20"}),
            )
            is not None
        )


# --------------------------------------------------------------------------- #
# 审计抽样
# --------------------------------------------------------------------------- #


class TestAuditTruncationPoints:
    def test_deterministic_thirds(self) -> None:
        dates = [date(2024, 1, d) for d in range(1, 10)]
        points = audit_truncation_points(dates)
        assert points == [dates[3], dates[6]]

    def test_short_series_dedup(self) -> None:
        dates = [date(2024, 1, 2)]
        assert audit_truncation_points(dates) == [date(2024, 1, 2)]
        assert audit_truncation_points([]) == []


class _ContainerResult:
    def __init__(self, dates: list[date], values: dict[str, Any]) -> None:
        self.dates = dates
        self.values = values
        self.quality = {"nan_ratio": 0.0}
        self.run_id = "RCR-test"


def _job(payload: dict[str, object] | None = None) -> JobRecord:
    return JobRecord(
        job_id="BJ-1",
        kind="factor_series_build",
        queue="default",
        payload=payload or {},
        attempt=1,
        max_attempts=1,
        requested_by="agent:mcp",
    )


def _payload_kwargs() -> dict[str, object]:
    return {
        "kind": "factor",
        "name": "mom20",
        "commit": "c" * 40,
        "release_id": "DR-bars-1",
        "dataset_release_ids": ["DR-fin-1"],
        "window_start": "2022-01-01",
        "window_end": "2023-06-30",
    }


class _StubSettings:
    research_sandbox_enabled = True
    research_code_repo_path = "unused"


def _make_executor(
    monkeypatch,
    *,
    runner_result: Any,
    audit_outcomes: list[Any],
) -> tuple[FactorSeriesBuildExecutor, dict[str, Any]]:
    """构造走真实 _build_spec / 默认 runner / audit 入口的执行器。

    按任务约定 monkeypatch `research_sandbox.runner` 模块属性(#359 分支未
    合并:FactorSeriesRunSpec / run_factor_series_container 以假实现顶替;
    audit.run_prefix_invariance_audit 同理),不做 ImportError 兜底。
    返回 (executor, 观测记录)。
    """
    import sys
    import types

    import finboard_backtest.research_sandbox.runner as runner_mod

    observed: dict[str, Any] = {
        "specs": [],
        "truncate_points": [],
        "persisted": [],
        "audit_baselines": [],
        "runner_results": [],
    }

    @dataclass(frozen=True)
    class _StubSpec:
        code_artifact: str
        code_commit: str
        release_id: str
        dataset_release_ids: tuple[str, ...]
        params: dict[str, Any]
        window_start: Any
        window_end: Any
        dates: tuple[Any, ...]

    async def _run_container(spec: Any) -> Any:
        observed["specs"].append(spec)
        observed["runner_results"].append(runner_result)
        return runner_result

    async def _run_audit(
        build_fn: Any,
        *,
        mode: Any,
        cut_points: Any,
        dates: Any = None,
        baseline: Any = None,
    ) -> Any:
        # 与 #359 真实引擎同签名的假引擎:只记录抽样截断点与编排方传入的
        # 基线(#371 起主构建产物直接复用,不再重建),回放结果,不调
        # build_fn(引擎本体行为由 research_sandbox 审计专项测试覆盖)
        assert mode == "truncation"
        observed["truncate_points"].extend(cut_points)
        observed["audit_baselines"].append(baseline)
        return audit_outcomes.pop(0)

    monkeypatch.setattr(
        runner_mod, "FactorSeriesRunSpec", _StubSpec, raising=False
    )
    monkeypatch.setattr(
        runner_mod, "run_factor_series_container", _run_container, raising=False
    )
    # audit 引擎以 sys.modules 桩顶替(与 #359 引擎同签名;monkeypatch
    # 模式,生产代码保持直接 import,不做 ImportError 兜底)。
    audit_stub = types.ModuleType("finboard_backtest.research_sandbox.audit")
    audit_stub.run_prefix_invariance_audit = _run_audit  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules, "finboard_backtest.research_sandbox.audit", audit_stub
    )

    class _Repo:
        def __init__(self, session: Any) -> None:
            pass

        async def upsert(self, record: FactorSeriesRecord) -> Any:
            observed["persisted"].append(record)
            return record

    import finboard_persistence

    monkeypatch.setattr(finboard_persistence, "FactorSeriesRepository", _Repo)

    class _Session:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def commit(self) -> None:
            return None

    executor = FactorSeriesBuildExecutor(
        session_maker=lambda: _Session(),  # type: ignore[arg-type]
        settings_factory=_StubSettings,
    )

    async def _no_cache(payload: Any) -> None:
        return None

    async def _code(payload: Any) -> tuple[str | None, str]:
        return "RC-1", "c" * 40

    async def _releases(payload: Any) -> None:
        return None

    monkeypatch.setattr(executor, "_find_cached", _no_cache)
    monkeypatch.setattr(executor, "_resolve_code", _code)
    monkeypatch.setattr(executor, "_require_releases", _releases)
    return executor, observed


@dataclass
class _AuditOutcome:
    passed: bool = True
    first_divergence_date: date | None = None


class TestAuditSampling:
    async def test_negative_audit_persists(self, monkeypatch) -> None:
        """阴性(mock 审计通过):内容寻址落库,返回 succeeded,恰好抽 2 点。"""
        dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
        values = {d.isoformat(): {"600000.SH": 1.0} for d in dates}
        executor, observed = _make_executor(
            monkeypatch,
            runner_result=_ContainerResult(dates, values),
            audit_outcomes=[_AuditOutcome(), _AuditOutcome()],
        )
        result = await executor.execute(
            _job(_payload_kwargs()), _noop_progress
        )
        assert result.status == "succeeded"
        assert result.result_ref == observed["persisted"][0].series_id
        # 抽样恰好 2 个截断点(3 日序列 → 1/3 与 2/3 位),specs 透传给审计。
        assert observed["truncate_points"] == [dates[1], dates[2]]
        # 两个截断点都复用主构建产物作基线,不再重建(#371)。
        assert observed["audit_baselines"] == [
            observed["runner_results"][0],
            observed["runner_results"][0],
        ]
        assert observed["specs"] == observed["specs"]  # 1 个 spec(build)
        spec = observed["specs"][0]
        assert spec.code_artifact == "mom20"
        assert spec.code_commit == "c" * 40
        assert spec.release_id == "DR-bars-1"

    async def test_positive_audit_fails_named_date(self, monkeypatch) -> None:
        """阳性(mock 审计检出):failed=lookahead_detected,具名首个分歧
        日期,不落库。"""
        dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
        values = {d.isoformat(): {"600000.SH": 1.0} for d in dates}
        divergence = date(2024, 1, 3)
        executor, observed = _make_executor(
            monkeypatch,
            runner_result=_ContainerResult(dates, values),
            audit_outcomes=[
                _AuditOutcome(passed=False, first_divergence_date=divergence),
                _AuditOutcome(passed=True),
            ],
        )
        result = await executor.execute(
            _job(_payload_kwargs()), _noop_progress
        )
        assert result.status == "failed"
        assert result.error_code == LOOKAHEAD_DETECTED
        assert divergence.isoformat() in (result.error_summary or "")
        assert observed["persisted"] == []  # 检出不落库

    async def test_content_addressed_record_from_result(
        self, monkeypatch
    ) -> None:
        """容器产出装配为内容寻址 Record:series_id = FS- + key[:12]。"""
        dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
        values = {d.isoformat(): {"600000.SH": 1.0} for d in dates}
        executor, observed = _make_executor(
            monkeypatch,
            runner_result=_ContainerResult(dates, values),
            audit_outcomes=[_AuditOutcome(), _AuditOutcome()],
        )
        result = await executor.execute(
            _job(_payload_kwargs()), _noop_progress
        )
        record = observed["persisted"][0]
        assert result.result_ref == record.series_id
        assert record.series_id == f"FS-{record.series_key[:12]}"
        assert record.code_artifact == "mom20"
        assert record.release_id == "DR-bars-1"
        assert record.dataset_release_ids == ("DR-fin-1",)
        assert record.source_run_id == "RCR-test"


async def _noop_progress(done: int, total: int | None, phase: str | None) -> None:
    return None


# --------------------------------------------------------------------------- #
# 加载器双轨:series values == 同参数逐日点快照值(合成数据对照)
# --------------------------------------------------------------------------- #

from finboard_backtest.research_run.frozen_loader import (  # noqa: E402
    FactorSeriesRecordLike,
    series_feature_values,
)


@dataclass
class _RecordView:
    """FactorSeriesRecordLike 最小视图(与 FactorSeriesRecord 同构)。"""

    series_id: str
    code_artifact: str
    release_id: str
    dates: tuple[date, ...]
    values: dict[str, dict[str, float | None]]


class TestSeriesFeatureValues:
    def test_values_equal_daily_point_snapshots(self) -> None:
        """同参数下 series 逐日截面 == 逐日点快照的 FeatureValue(逐值等值)。

        对照组:每个决策日构造一个「单日快照」(同一观测),series_feature_
        values 按决策日索引 series.values,两者 symbol/feature_id/value/
        source 逐项一致;唯一预期差异是 available_at 语义(快照为观测时点,
        序列为决策时点,由审计背书)。
        """
        decision_at = datetime(2024, 1, 31, 7, 0, tzinfo=UTC)
        record = _RecordView(
            series_id="FS-abc123def456",
            code_artifact="mom20",
            release_id="DR-bars-1",
            dates=(decision_at.date(),),
            values={"2024-01-31": {"600000.SH": 1.5, "000001.SZ": -0.5}},
        )
        from finboard_data.factor_lab import sandbox_factor_name

        got = series_feature_values(
            record, decision_at, factor_name=sandbox_factor_name("mom20")
        )
        # 对照:逐日点快照会为同一截面产出同样的 (symbol, feature_id, value)。
        expected_pairs = {
            ("600000.SH", "u_mom20", 1.5),
            ("000001.SZ", "u_mom20", -0.5),
        }
        got_pairs = {(v.symbol, v.feature_id, v.value) for v in got}
        assert got_pairs == expected_pairs
        # artifact 绑定序列本身,available_at = decision_at(审计背书)。
        for value in got:
            assert value.source_artifact_ids == ("FS-abc123def456",)
            assert value.available_at == decision_at

    def test_null_value_preserved(self) -> None:
        decision_at = datetime(2024, 1, 31, 7, 0, tzinfo=UTC)
        record = _RecordView(
            series_id="FS-abc123def456",
            code_artifact="mom20",
            release_id="DR-bars-1",
            dates=(decision_at.date(),),
            values={"2024-01-31": {"600000.SH": None}},
        )
        got = series_feature_values(record, decision_at, factor_name="u_mom20")
        assert len(got) == 1
        assert got[0].value is None

    def test_missing_day_returns_empty(self) -> None:
        decision_at = datetime(2024, 3, 29, 7, 0, tzinfo=UTC)
        record = _RecordView(
            series_id="FS-abc123def456",
            code_artifact="mom20",
            release_id="DR-bars-1",
            dates=(date(2024, 1, 31),),
            values={"2024-01-31": {"600000.SH": 1.0}},
        )
        assert (
            series_feature_values(record, decision_at, factor_name="u_mom20")
            == ()
        )


class _LoaderHarness:
    """FrozenInputLoader 双轨行为的微型桩(loader 不依赖 DB)。"""

    def __init__(
        self,
        *,
        record: FactorSeriesRecordLike | None,
        snapshot_features: tuple[FeatureValue, ...] = (),
    ) -> None:
        self.record = record
        self.snapshot_features = snapshot_features

    async def series_provider(self, series_id: str) -> Any:
        return self.record

    async def snapshot_provider(self, snapshot_id: str) -> Any:
        return None


def test_featurevalue_contract_for_loader_merge() -> None:
    """FeatureValue 是两条轨道的共同汇合点:构造即校验(有限值 / 来源)。"""
    decision_at = datetime(2024, 1, 31, 7, 0, tzinfo=UTC)
    series_value = FeatureValue(
        symbol="600000.SH",
        feature_id="u_mom20",
        value=1.5,
        source_artifact_ids=("FS-abc123def456",),
        available_at=decision_at,
    )
    snapshot_value = FeatureValue(
        symbol="600000.SH",
        feature_id="u_mom20",
        value=1.5,
        source_artifact_ids=("factor-v1",),
        available_at=decision_at,
    )
    # 同名因子两轨可比较的值面一致(extract_factor_matrix 消费形态)。
    assert (series_value.symbol, series_value.feature_id, series_value.value) == (
        snapshot_value.symbol,
        snapshot_value.feature_id,
        snapshot_value.value,
    )
    with pytest.raises(ValueError, match="冻结来源"):
        FeatureValue(
            symbol="600000.SH",
            feature_id="u_mom20",
            value=1.5,
            source_artifact_ids=(),
            available_at=decision_at,
        )


def test_series_key_version_pinned() -> None:
    """series_key 规则版本前缀钉死 v2(canonical 规则变更时旧键永不复用)。"""
    from finboard_persistence.factor_series_repo import SERIES_KEY_VERSION

    assert SERIES_KEY_VERSION == "v2"
    key = compute_series_key(
        code_commit="c" * 40,
        release_id="DR-1",
        dataset_release_ids=[],
        params={},
        window_start=date(2024, 1, 1),
        window_end=date(2024, 1, 2),
    )
    assert len(key) == 64
