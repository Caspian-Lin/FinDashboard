"""ResearchRun manifest 黄金样本(issue #513)。

#513 把 REST(``routes/research_runs.py``)与 MCP(``tools/runs.py``)两份
「预检 → 冻结 → manifest」编排收敛为 ``ResearchRunSubmissionService``。本文件
锁定重构后的字节契约:

* ``RR-`` 由幂等键确定性派生(逐字节,与历史实现一致);
* 7 组代表性 payload(最小 / 全参数 / data_scope / user_code / factor_series /
  factor_snapshot / replay)的 ``manifest_checksum`` 黄金值 —— ``ResearchRunManifest`` 的字段顺序、
  取值与 None 省略规则(``contracts.py``)任何漂移都会立即失败;
* REST 上下文(显式 ``actor_type=agent``)与 MCP 上下文对同一 payload 产出
  **完全相同**的 manifest —— 两条适配层不再有第二份编排。

黄金值取自 #513 重构后的实现;重构前后 manifest 构造逐字段一致(纯搬移,未改
``contracts.py``),因此同一 payload 的 checksum 与历史值相同 —— 后续任何字段
增删/顺序变化都会让本文件失败,必须显式申报 ``schema_version`` 变更。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import finboard_app.research_run_submission as submission
from finboard_app.research_operation import ResearchOperationContext
from finboard_app.research_run_submission import (
    ResearchRunReplayInput,
    ResearchRunSubmissionService,
    ResearchRunSubmitInput,
    research_run_id_for,
)
from finboard_backtest.research_run import (
    ResearchActorType,
    ResearchRunRecord,
    ResearchRunStatus,
    stable_checksum,
    to_json_value,
)
from finboard_backtest.strategy_spec import (
    FeatureGraph,
    ResearchStrategySpec,
    SignalRules,
    build_strategy_template,
)
from finboard_backtest.strategy_spec.contracts import StrategyCodeArtifactRef
from finboard_data import AssetCapability, CapabilityStatus, ResearchDatasetRelease
from finboard_data.factor_lab import FeatureObservation, FeatureSnapshot
from finboard_data.releases import (
    RELEASE_FIELDS,
    ReleasedInstrument,
    default_execution_metadata,
)
from finboard_persistence.factor_series_repo import FactorSeriesRecord
from finboard_shared.types import (
    AssetClass,
    BarPeriod,
    DatasetQualityStatus,
    InstrumentType,
    Market,
)

RELEASE_ID = "golden-release-513"
RELEASE_CHECKSUM = "b" * 64
SPEC_ID = "golden_strategy_513"
SPEC_CHECKSUM = "d" * 64
USER_CODE_NAME = "golden_user_code_513"
USER_CODE_COMMIT = "abcdef0123456789"
SERIES_ID = "FS-0123456789abcdef"
SNAPSHOT_ID = "FSNAP-0123456789abcdef"


# --------------------------------------------------------------------------- #
# 固定输入
# --------------------------------------------------------------------------- #
def _instrument(code: str) -> ReleasedInstrument:
    return ReleasedInstrument(
        code=code,
        name=code,
        name_history=(),
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2024, 1, 1, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        artifact_path=f"bars/{code}_D1_qfq.parquet",
        artifact_checksum="a" * 64,
        artifact_size=1024,
        row_count=240,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        expected_sessions=240,
        missing_sessions=0,
        suspended_sessions=0,
        anomaly_count=0,
        coverage_pct=Decimal("1.0"),
        category="stock",
        ready=True,
        list_date=date(2020, 1, 1),
        delist_date=None,
        industry=None,
    )


def _release() -> ResearchDatasetRelease:
    instruments = tuple(_instrument(code) for code in ("600001.SH", "600002.SH", "600003.SH"))
    return ResearchDatasetRelease(
        release_id=RELEASE_ID,
        dataset_name="golden_bars_513",
        source="golden-test",
        version="v1",
        schema_version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        period=BarPeriod.D1,
        adjustment="qfq",
        fields=RELEASE_FIELDS,
        availability_rules=(),
        code_version="abcdef0123456789",
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
        instruments=instruments,
        capabilities=(
            AssetCapability(
                key="stock",
                status=CapabilityStatus.READY,
                symbol_count=len(instruments),
                ready_count=len(instruments),
            ),
        ),
        quality_status=DatasetQualityStatus.PASSED,
        quality_report={"release_coverage": "1.0"},
        storage_uri=RELEASE_ID,
        release_checksum=RELEASE_CHECKSUM,
    )


def _ma_cross_spec() -> ResearchStrategySpec:
    spec = build_strategy_template(
        "ma_cross",
        strategy_id=SPEC_ID,
        dataset_release_ids=(RELEASE_ID,),
    )
    # 桩发布不携带成交额元数据:放宽 universe 让候选池非空(与 #312 同法)。
    return spec.model_copy(
        update={
            "universe": spec.universe.model_copy(
                update={
                    "min_average_amount": None,
                    "required_data_fields": ("price",),
                    "ranking": None,
                }
            )
        }
    )


def _user_code_spec() -> ResearchStrategySpec:
    """multi_factor 模板改造为 user_code:清空 graph/rules + 挂 code_artifact。"""

    template = build_strategy_template(
        "multi_factor",
        strategy_id=f"{SPEC_ID}_user_code",
        dataset_release_ids=(RELEASE_ID,),
    )
    spec = template.model_copy(
        update={
            "strategy_kind": "user_code",
            "feature_graph": FeatureGraph(nodes=(), outputs=()),
            "signal_rules": SignalRules(rules=()),
            "code_artifact": StrategyCodeArtifactRef(name=USER_CODE_NAME),
            "universe": template.universe.model_copy(
                update={
                    "min_average_amount": None,
                    "required_data_fields": ("price",),
                    "ranking": None,
                }
            ),
        }
    )
    return spec


def _series_record() -> FactorSeriesRecord:
    return FactorSeriesRecord.build(
        code_artifact=USER_CODE_NAME,
        code_commit=USER_CODE_COMMIT,
        kind="strategy",
        release_id=RELEASE_ID,
        dataset_release_ids=(RELEASE_ID,),
        params={},
        window_start=date(2024, 1, 1),
        window_end=date(2024, 12, 31),
        dates=[date(2024, 1, 2)],
        values={"2024-01-02": {"600001.SH": 0.01}},
    )


def _snapshot() -> FeatureSnapshot:
    """最小可入队特征快照:锚定本次 bars 发布,单标的单因子观测。

    ``factor_snapshots`` 的 ``FrozenArtifactRef`` 分支(snapshot_id /
    framework_version / checksum / 排序后的 ``factor:`` 能力集)由此锁定。
    """

    decision_at = datetime(2024, 12, 31, tzinfo=UTC)
    return FeatureSnapshot(
        snapshot_id=SNAPSHOT_ID,
        dataset_release_id=RELEASE_ID,
        dataset_release_checksum=RELEASE_CHECKSUM,
        decision_at=decision_at,
        published_at=datetime(2024, 12, 31, tzinfo=UTC),
        framework_version="factor-framework-v1",
        calculation_windows={},
        transformations={},
        neutralization={},
        code_version=USER_CODE_COMMIT,
        observations=(
            FeatureObservation(
                symbol="600001.SH",
                feature_name="u_golden_factor",
                value=0.25,
                observed_at=decision_at,
                available_at=datetime(2024, 12, 30, tzinfo=UTC),
                source="golden-test",
                source_version="v1",
            ),
        ),
        checksum="e" * 64,
    )


def _payload(key: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "idempotency_key": key,
        "strategy_id": SPEC_ID,
        "strategy_version": 1,
        "dataset_release_ids": [RELEASE_ID],
        "code_version": "abcdef0123456789",
        "initial_capital": "100000",
        "requested_by": "user:513",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# 假仓储(不连 DB;仅替换服务内引用,门控函数全部走真实实现)
# --------------------------------------------------------------------------- #
class _FakeSpecRepo:
    def __init__(self, session: object) -> None:
        del session

    async def get_version(self, strategy_id: str, version: int) -> object:
        spec = _current_spec()
        return SimpleNamespace(
            status="published",
            payload=spec.canonical_payload(),
            checksum=stable_checksum(spec.canonical_payload()),
        )


class _FakeReleaseRepo:
    def __init__(self, session: object) -> None:
        del session

    async def require_usable(self, release_id: str) -> ResearchDatasetRelease:
        return _release()


class _FakeFeatureRepo:
    def __init__(self, session: object) -> None:
        del session

    async def get(self, snapshot_id: str) -> object:
        if snapshot_id == SNAPSHOT_ID:
            return _snapshot()
        raise AssertionError(f"未声明的快照: {snapshot_id}")


class _FakeSeriesRepo:
    def __init__(self, session: object) -> None:
        del session

    async def get(self, series_id: str) -> object:
        return _series_record()


class _FakeRunRepo:
    def __init__(self, session: object) -> None:
        del session

    async def create_or_get(self, **kwargs: object) -> tuple[object, bool]:
        del kwargs
        return SimpleNamespace(job_id="BJ-golden"), True

    async def get(self, run_id: str) -> object:
        del run_id
        return SimpleNamespace(job_id="BJ-golden")


class _FakeStore:
    def __init__(self, repository: object) -> None:
        del repository

    async def get(self, run_id: str) -> ResearchRunRecord | None:
        del run_id
        return ResearchRunRecord(
            manifest=_replay_source(), status=ResearchRunStatus.COMPLETED
        )

    async def create_or_get(
        self, manifest: Any
    ) -> tuple[ResearchRunRecord, bool]:
        return ResearchRunRecord(manifest=manifest), True


_CURRENT_SPEC: ResearchStrategySpec | None = None
_REPLAY_SOURCE_MANIFEST: Any = None


def _current_spec() -> ResearchStrategySpec:
    assert _CURRENT_SPEC is not None
    return _CURRENT_SPEC


def _replay_source() -> Any:
    assert _REPLAY_SOURCE_MANIFEST is not None
    return _REPLAY_SOURCE_MANIFEST


def _patch(monkeypatch: pytest.MonkeyPatch, spec: ResearchStrategySpec) -> None:
    global _CURRENT_SPEC
    _CURRENT_SPEC = spec
    monkeypatch.setattr(submission, "ResearchStrategySpecRepository", _FakeSpecRepo)
    monkeypatch.setattr(
        submission, "ResearchDatasetReleaseRepository", _FakeReleaseRepo
    )
    monkeypatch.setattr(submission, "FeatureSnapshotRepository", _FakeFeatureRepo)
    monkeypatch.setattr(submission, "FactorSeriesRepository", _FakeSeriesRepo)
    monkeypatch.setattr(submission, "ResearchRunRepository", _FakeRunRepo)
    monkeypatch.setattr(submission, "SqlAlchemyResearchRunStore", _FakeStore)

    async def _no_bindings(session: object, *, spec: object) -> tuple[None, None]:
        del session, spec
        return None, None

    async def _no_user_factors(session: object) -> frozenset[str]:
        del session
        return frozenset()

    async def _no_strategies(session: object) -> dict[str, str]:
        del session
        return {USER_CODE_NAME: USER_CODE_COMMIT}

    monkeypatch.setattr(submission, "resolve_screen_bindings", _no_bindings)
    monkeypatch.setattr(submission, "active_user_factor_names", _no_user_factors)
    monkeypatch.setattr(
        submission, "active_user_strategy_commits", _no_strategies
    )


def _service(monkeypatch: pytest.MonkeyPatch, spec: ResearchStrategySpec) -> Any:
    _patch(monkeypatch, spec)
    return ResearchRunSubmissionService(
        cast(Any, AsyncMock()), sandbox_enabled=True, release_root=None
    )


REST_AGENT = ResearchOperationContext.for_web(
    "req-golden-513", actor_type=ResearchActorType.AGENT
)
MCP = ResearchOperationContext.for_mcp()


# --------------------------------------------------------------------------- #
# 1) run_id 确定性派生
# --------------------------------------------------------------------------- #
def test_run_id_matches_sha256_prefix() -> None:
    assert research_run_id_for("golden-minimal-key") == GOLDEN_RUN_IDS["minimal"]
    assert research_run_id_for("golden-full-key") == GOLDEN_RUN_IDS["full"]
    # 幂等键长度/内容不同 → run_id 不同(防碰撞假设与历史实现一致)。
    assert research_run_id_for("a" * 8) != research_run_id_for("a" * 9)


# --------------------------------------------------------------------------- #
# 2) 7 组 payload 的 checksum 黄金值 + REST/MCP 等值
# --------------------------------------------------------------------------- #
GOLDEN_RUN_IDS = {
    "minimal": "RR-26883230ba131bf446fdbb94",
    "full": "RR-0e10b8581f11eca6db1809bd",
    "data_scope": "RR-1909a209c7eec3b6c13a38b3",
    "user_code": "RR-c1c755cf79c12ffeca1d92b6",
    "factor_series": "RR-9a000460c864a398895f23e2",
    "replay": "RR-53f852dbc36aae42d0ad1450",
    "factor_snapshot": "RR-dc172d85b11fc902ad401e95",
}

GOLDEN_CHECKSUMS = {
    "minimal": "16e6498770aaef1d072337ef876ac05cfa9a6615ca64cf570bbf412ece69f5c1",
    "full": "41f8d110f371caa3d13ce6ecda31d88a93a4d50c1c61fd22914ba7bab27d4150",
    "data_scope": "33d571aff08c344e10fddaa602bff181055f517fe69a8b5e1a5c1cdd2425dd15",
    "user_code": "aa0437ee20b261dfafd5ea83ab0a5e33e87fe6a08383c635857f1265187c5fcd",
    "factor_series": "f411dc5ba3bb5368a44c891fc72bf09cd685379f8d3709e7171b4df6bb9b95ad",
    "replay": "6b347b5528c2111bebcae5a0341bc85c72f30b36a0db638c2c10c569a085f8cf",
    "factor_snapshot": "670702fae456394902d92d05c5fb308802baeea2b8d2b3a95cfd4fc9a9933041",
}


@pytest.mark.asyncio
async def test_golden_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch, _ma_cross_spec())
    body = ResearchRunSubmitInput.model_validate(_payload("golden-minimal-key"))
    report = await service.preflight(body, REST_AGENT)
    assert report.run_id == GOLDEN_RUN_IDS["minimal"]
    assert report.manifest_checksum == GOLDEN_CHECKSUMS["minimal"]


@pytest.mark.asyncio
async def test_golden_full(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch, _ma_cross_spec())
    body = ResearchRunSubmitInput.model_validate(
        _payload(
            "golden-full-key",
            initial_capital="500000",
            actor_type="agent",
            portfolio_config={"max_risk_contribution": 0.5},
            risk_config={
                "rules": [
                    {
                        "rule_type": "price_stop_loss",
                        "enabled": True,
                        "threshold": 0.08,
                    }
                ]
            },
            fee_config={
                "commission_rate": 0.0006,
                "minimum_commission": 10,
                "sell_tax_rate": 0.001,
                "slippage_bps": 10,
            },
            benchmark_config={"symbol": "000300.SH"},
        )
    )
    report = await service.preflight(body, REST_AGENT)
    assert report.run_id == GOLDEN_RUN_IDS["full"]
    assert report.manifest_checksum == GOLDEN_CHECKSUMS["full"]
    # 覆盖只进 manifest 的对应分区,不串区。
    assert report.manifest.fee_config["overrides"]["commission_rate"] == 0.0006
    assert report.manifest.portfolio_config["overrides"] == {
        "max_risk_contribution": 0.5
    }


@pytest.mark.asyncio
async def test_golden_data_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch, _ma_cross_spec())

    async def _fake_scope(
        session: object, scope: object, **kwargs: object
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        del session, scope, kwargs
        return (
            {
                "research_window": {
                    "warmup_start": "2024-01-01",
                    "decision_start": "2024-01-01",
                    "decision_end": "2024-12-31",
                    "valuation_end": "2024-12-31",
                }
            },
            {
                "resolution_id": "SC-0123456789abcdef",
                "release_ids": [RELEASE_ID],
                "scope": {"universe": "all"},
            },
        )

    monkeypatch.setattr(submission, "apply_scope_to_parameters", _fake_scope)
    body = ResearchRunSubmitInput.model_validate(
        _payload(
            "golden-scope-key",
            data_scope={
                "universe": {
                    "explicit_symbols": ["600001.SH", "600002.SH", "600003.SH"],
                    "filters": {"min_listing_days": 60},
                },
                "decision": {"start": "2024-01-01", "end": "2024-12-31"},
                "warmup_sessions": 0,
                "tail_sessions": 1,
                "datasets": {"required": ["bars"]},
                "bars_adjustment": "qfq",
            },
            data_scope_resolution_id="SC-0123456789abcdef",
        )
    )
    report = await service.preflight(body, REST_AGENT)
    assert report.run_id == GOLDEN_RUN_IDS["data_scope"]
    assert report.manifest_checksum == GOLDEN_CHECKSUMS["data_scope"]
    assert report.manifest.data_scope is not None
    assert report.manifest.data_scope["resolution_id"] == "SC-0123456789abcdef"
    # 窗口只进 manifest parameters,不改写 payload 原参数(#361 series_key)。
    assert "research_window" in report.manifest.parameters
    assert "research_window" not in body.parameters


@pytest.mark.asyncio
async def test_golden_user_code(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _user_code_spec()
    service = _service(monkeypatch, spec)
    body = ResearchRunSubmitInput.model_validate(
        _payload(
            "golden-user-code-key",
            strategy_id=spec.strategy_id,
            # user_code 单时点决策必须来自冻结快照;声明决策日历走多期回放
            # (与 #218 放行路径一致),空特征图无 u_ 因子覆盖要求。
            parameters={"decision_schedule": {"kind": "monthly"}},
        )
    )
    report = await service.preflight(body, REST_AGENT)
    assert report.run_id == GOLDEN_RUN_IDS["user_code"]
    assert report.manifest_checksum == GOLDEN_CHECKSUMS["user_code"]
    # #218:active commit 冻结进 manifest 内的 spec,checksum 随之覆盖代码版本。
    assert report.manifest.strategy_spec.code_artifact is not None
    assert report.manifest.strategy_spec.code_artifact.commit == USER_CODE_COMMIT
    assert report.manifest.strategy_spec_checksum != stable_checksum(
        spec.canonical_payload()
    )


@pytest.mark.asyncio
async def test_golden_factor_series(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch, _ma_cross_spec())
    record = _series_record()
    body = ResearchRunSubmitInput.model_validate(
        _payload("golden-series-key", factor_series_ids=[record.series_id])
    )
    report = await service.preflight(body, REST_AGENT)
    assert report.run_id == GOLDEN_RUN_IDS["factor_series"]
    assert report.manifest_checksum == GOLDEN_CHECKSUMS["factor_series"]
    # #360:序列冻结引用 = {series_id, content_checksum};空集合不入 checksum。
    assert len(report.manifest.factor_series) == 1
    assert report.manifest.factor_series[0].artifact_id == record.series_id
    assert report.manifest.factor_series[0].checksum == record.content_checksum


@pytest.mark.asyncio
async def test_golden_factor_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """``factor_snapshots`` 的 FrozenArtifactRef 分支(其余 6 组均为空)。"""

    service = _service(monkeypatch, _ma_cross_spec())
    body = ResearchRunSubmitInput.model_validate(
        _payload("golden-snapshot-key", factor_snapshot_ids=[SNAPSHOT_ID])
    )
    report = await service.preflight(body, REST_AGENT)
    assert report.run_id == GOLDEN_RUN_IDS["factor_snapshot"]
    assert report.manifest_checksum == GOLDEN_CHECKSUMS["factor_snapshot"]
    assert len(report.manifest.factor_snapshots) == 1
    ref = report.manifest.factor_snapshots[0]
    assert ref.artifact_id == SNAPSHOT_ID
    assert ref.version == "factor-framework-v1"
    assert ref.checksum == "e" * 64
    # 能力集由 observations 的因子名排序去重派生(单因子 → 单元素)。
    assert ref.capabilities == ("factor:u_golden_factor",)


@pytest.mark.asyncio
async def test_replay_domain_conflict_is_mapped_to_operation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``store.create_or_get`` 抛领域冲突时业务层只漏 ``ResearchOperationError``。

    ``SqlAlchemyResearchRunStore`` 会把仓储层的持久化冲突包装成
    ``ResearchRunConflictError``(#513 复核对齐),若不显式收口就会绕过统一错误
    分类,让 REST/MCP 适配层拿到未映射异常。
    """

    from finboard_app.research_operation import ResearchOperationError
    from finboard_backtest.research_run import ResearchRunConflictError

    service = _service(monkeypatch, _ma_cross_spec())
    source = await service.preflight(
        ResearchRunSubmitInput.model_validate(_payload("golden-replay-source-key")),
        REST_AGENT,
    )

    class _ConflictStore(_FakeStore):
        async def get(self, run_id: str) -> ResearchRunRecord | None:
            del run_id
            return ResearchRunRecord(
                manifest=source.manifest, status=ResearchRunStatus.COMPLETED
            )

        async def create_or_get(self, manifest: Any) -> Any:
            del manifest
            raise ResearchRunConflictError("相同 idempotency_key 对应不同 manifest")

    monkeypatch.setattr(submission, "SqlAlchemyResearchRunStore", _ConflictStore)

    with pytest.raises(ResearchOperationError) as excinfo:
        await service.replay(
            "RR-replay-source",
            ResearchRunReplayInput(
                idempotency_key="golden-replay-conflict", requested_by="user:513"
            ),
            MCP,
        )
    assert excinfo.value.category == "conflict"
    assert excinfo.value.http_status == 409
    assert excinfo.value.mcp_kind == "conflict"
    assert excinfo.value.code == "run_conflict"


@pytest.mark.asyncio
async def test_golden_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    global _REPLAY_SOURCE_MANIFEST
    service = _service(monkeypatch, _ma_cross_spec())
    # 源 run 的冻结清单来自同一套预检(与真实 completed run 同形)。
    source = await service.preflight(
        ResearchRunSubmitInput.model_validate(_payload("golden-replay-source-key")),
        REST_AGENT,
    )
    _REPLAY_SOURCE_MANIFEST = source.manifest
    outcome = await service.replay(
        "RR-replay-source",
        ResearchRunReplayInput(
            idempotency_key="golden-replay-key", requested_by="user:513"
        ),
        MCP,
    )
    assert outcome.manifest.run_id == GOLDEN_RUN_IDS["replay"]
    assert outcome.manifest.checksum == GOLDEN_CHECKSUMS["replay"]
    # #305:血缘字段来自源 run,actor_type 来自服务端上下文(MCP 恒 agent)。
    assert outcome.manifest.replay_of_run_id == "RR-replay-source"
    assert outcome.manifest.replay_source_status == "completed"
    assert outcome.manifest.actor_type is ResearchActorType.AGENT


@pytest.mark.asyncio
async def test_rest_and_mcp_contexts_produce_identical_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 payload + 同一 actor_type:REST 与 MCP 上下文产出同一 manifest。"""

    service = _service(monkeypatch, _ma_cross_spec())
    body = ResearchRunSubmitInput.model_validate(
        _payload("golden-equivalence-key", actor_type="agent")
    )
    rest_report = await service.preflight(body, REST_AGENT)
    mcp_report = await service.preflight(body, MCP)
    assert rest_report.run_id == mcp_report.run_id
    assert rest_report.manifest_checksum == mcp_report.manifest_checksum
    assert to_json_value(rest_report.manifest) == to_json_value(mcp_report.manifest)
    # 默认 human 的 REST 上下文与 MCP 恒 agent 不同 —— 通道身份不被 payload 覆盖。
    human_report = await service.preflight(body, ResearchOperationContext.for_web("r"))
    assert human_report.manifest.checksum != rest_report.manifest_checksum
