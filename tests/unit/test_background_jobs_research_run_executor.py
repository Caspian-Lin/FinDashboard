"""research_run 执行器单元测试(issue #143)。

只覆盖不依赖 PostgreSQL 的纯逻辑:payload 校验、``ResearchRunStatus`` →
``JobResult.status`` 映射。依赖 PG 的端到端队列消费 / 双写一致性 / 崩溃恢复由
``tests/integration/test_research_run_worker.py`` 覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobRecord,
)
from finboard_backtest.background_jobs.executors.research_run import (
    _extract_run_id,
    _record_to_result,
)
from finboard_backtest.research_run import (
    ResearchRunManifest,
    ResearchRunStatus,
    stable_checksum,
)
from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchRunRecord,
)
from finboard_backtest.strategy_spec import build_strategy_template


def _make_manifest(run_id: str = "RR-test0000000001") -> ResearchRunManifest:
    spec = build_strategy_template(
        "ma_cross",
        strategy_id="ma_cross_test",
        dataset_release_ids=("release-v1",),
    )
    return ResearchRunManifest(
        run_id=run_id,
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
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
    )


def _make_record(
    status: ResearchRunStatus,
    *,
    run_id: str = "RR-test0000000001",
    error_code: str | None = None,
    error_summary: str | None = None,
) -> ResearchRunRecord:
    return ResearchRunRecord(
        manifest=_make_manifest(run_id=run_id),
        status=status,
        error_code=error_code,
        error_summary=error_summary,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


class TestExtractRunId:
    def test_valid_run_id(self) -> None:
        job = _make_job({"run_id": "RR-abc123"})
        assert _extract_run_id(job) == "RR-abc123"

    def test_missing_run_id_raises(self) -> None:
        job = _make_job({})
        with pytest.raises(ExecutorError) as exc_info:
            _extract_run_id(job)
        assert exc_info.value.retryable is False
        assert exc_info.value.code == "invalid_payload"

    def test_non_string_run_id_raises(self) -> None:
        job = _make_job({"run_id": 12345})
        with pytest.raises(ExecutorError):
            _extract_run_id(job)

    def test_wrong_prefix_raises(self) -> None:
        job = _make_job({"run_id": "BJ-notarrunid"})
        with pytest.raises(ExecutorError) as exc_info:
            _extract_run_id(job)
        assert exc_info.value.code == "invalid_payload"


class TestRecordToResult:
    @pytest.mark.parametrize(
        ("status", "expected_job_status"),
        [
            (ResearchRunStatus.COMPLETED, "succeeded"),
            (ResearchRunStatus.CANCELLED, "cancelled"),
            (ResearchRunStatus.INTERRUPTED, "retry_waiting"),
            (ResearchRunStatus.FAILED, "failed"),
            (ResearchRunStatus.REJECTED, "failed"),
        ],
    )
    def test_status_mapping(
        self,
        status: ResearchRunStatus,
        expected_job_status: str,
    ) -> None:
        record = _make_record(status)
        result = _record_to_result(record)
        assert result.status == expected_job_status
        assert result.result_ref == record.manifest.run_id

    def test_failed_carries_error_code_and_summary(self) -> None:
        record = _make_record(
            ResearchRunStatus.FAILED,
            error_code="non_deterministic_replay",
            error_summary="重放结果不一致",
        )
        result = _record_to_result(record)
        assert result.status == "failed"
        assert result.error_code == "non_deterministic_replay"
        assert result.error_summary == "重放结果不一致"

    def test_interrupted_carries_error_summary(self) -> None:
        record = _make_record(
            ResearchRunStatus.INTERRUPTED,
            error_summary="运行状态变为 interrupted",
        )
        result = _record_to_result(record)
        assert result.status == "retry_waiting"
        assert result.error_code == "interrupted"

    def test_succeeded_has_no_error(self) -> None:
        record = _make_record(ResearchRunStatus.COMPLETED)
        result = _record_to_result(record)
        assert result.error_code is None
        assert result.error_summary is None


# ---- helpers ----------------------------------------------------------------


def _make_job(payload: dict[str, object]) -> JobRecord:
    return JobRecord(
        job_id="BJ-TEST0000000001",
        kind="research_run",
        queue="research",
        payload=payload,
        attempt=1,
        max_attempts=3,
        requested_by="unit-test",
    )
