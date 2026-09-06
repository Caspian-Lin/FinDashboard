"""ResearchCodeRunExecutor —— payload 解析 / 失败分类 / 输出归档(issue #216)。

执行器全链路(代码解析→挂载→容器→登记)依赖 DB + Docker,由集成与 E2E
门控测试覆盖;此处覆盖纯函数面:_parse_payload / _classify / _read_outputs /
_stage_write_code。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobRecord,
)
from finboard_backtest.research_sandbox.errors import (
    OOM_KILLED,
    OUTPUT_CONTRACT_VIOLATION,
    RUNTIME_ERROR,
    SANDBOX_UNAVAILABLE,
    TIMEOUT,
)
from finboard_backtest.research_sandbox.executor import (
    _classify,
    _parse_payload,
    _read_outputs,
    _stage_write_code,
)
from finboard_backtest.research_sandbox.runner import SandboxRunResult


def _job(payload: Mapping[str, object]) -> JobRecord:
    return JobRecord(
        job_id="BJ-1",
        kind="research_code_run",
        queue="default",
        payload=dict(payload),
        attempt=1,
        max_attempts=1,
        requested_by="agent:mcp",
    )


_VALID = {
    "kind": "factor",
    "name": "mom20",
    "dataset_release_ids": ["DR-1"],
    "decision_at": "2024-06-03T15:00:00+08:00",
}


class TestParsePayload:
    def test_valid(self) -> None:
        payload = _parse_payload(_job(_VALID))
        assert payload.kind == "factor"
        assert payload.decision_at is not None
        assert payload.decision_at.tzinfo is not None

    def test_missing_fields(self) -> None:
        for key in ("kind", "name", "dataset_release_ids", "decision_at"):
            broken = {k: v for k, v in _VALID.items() if k != key}
            with pytest.raises(ExecutorError) as exc_info:
                _parse_payload(_job(broken))
            assert exc_info.value.code == "invalid_payload"

    def test_naive_decision_at_rejected(self) -> None:
        with pytest.raises(ExecutorError) as exc_info:
            _parse_payload(_job({**_VALID, "decision_at": "2024-06-03T15:00:00"}))
        assert exc_info.value.code == "invalid_payload"

    def test_empty_releases_rejected(self) -> None:
        with pytest.raises(ExecutorError):
            _parse_payload(_job({**_VALID, "dataset_release_ids": []}))

    def test_digest_stable(self) -> None:
        a = _parse_payload(_job(_VALID)).digest()
        b = _parse_payload(_job(dict(_VALID))).digest()
        assert a == b
        assert len(a) == 16


def _run_result(
    *,
    exit_code: int | None = 0,
    timed_out: bool = False,
    oom: bool = False,
    usage: dict[str, Any] | None = None,
) -> SandboxRunResult:
    return SandboxRunResult(
        container_id="cid",
        image_digest="sha256:x",
        exit_code=exit_code,
        stdout="",
        stderr="boom",
        timed_out=timed_out,
        oom_killed=oom,
        duration_seconds=1.2,
        usage=usage or {"max_mem_mb": 512.0, "max_cpu_percent": 40.0},
    )


def _outputs(
    *, error: dict[str, Any] | None = None, empty: bool = False
):
    from finboard_backtest.research_sandbox.executor import _Outputs

    if empty:
        return _Outputs(problems=["缺少 scores.parquet", "缺少 metrics.json"])
    return _Outputs(
        scores_path=Path("x"),
        scores_checksum="ab" * 32,
        metrics={"coverage": 1.0},
        error=error,
    )


_Outputs_ok = _outputs()


class TestClassify:
    def test_timeout(self) -> None:
        ok, code, summary = _classify(_run_result(timed_out=True), _Outputs_ok)
        assert ok is False
        assert code == TIMEOUT
        assert "峰值内存" in (summary or "")

    def test_oom(self) -> None:
        ok, code, _ = _classify(_run_result(oom=True, exit_code=137), _Outputs_ok)
        assert ok is False
        assert code == OOM_KILLED
        ok, code, _ = _classify(_run_result(exit_code=137), _Outputs_ok)
        assert ok is False
        assert code == OOM_KILLED

    def test_contract_exit_3(self) -> None:
        ok, code, summary = _classify(
            _run_result(exit_code=3), _outputs(error={"message": "scores 为空"})
        )
        assert ok is False
        assert code == OUTPUT_CONTRACT_VIOLATION
        assert "scores 为空" in (summary or "")

    def test_runtime_exit_4(self) -> None:
        ok, code, _ = _classify(
            _run_result(exit_code=4), _outputs(error={"message": "ValueError: x"})
        )
        assert ok is False
        assert code == RUNTIME_ERROR

    def test_exit0_missing_outputs(self) -> None:
        ok, code, summary = _classify(_run_result(exit_code=0), _outputs(empty=True))
        assert ok is False
        assert code == OUTPUT_CONTRACT_VIOLATION
        assert "scores.parquet" in (summary or "")

    def test_docker_exit_codes_retryable(self) -> None:
        for exit_code in (125, 126, 127):
            ok, code, _ = _classify(_run_result(exit_code=exit_code), _Outputs_ok)
            assert ok is False
            assert code == SANDBOX_UNAVAILABLE


class TestReadOutputs:
    def test_valid_outputs(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        pq.write_table(
            pa.table({"symbol": ["A"], "score": [1.0]}), out / "scores.parquet"
        )
        (out / "metrics.json").write_text(
            json.dumps({"coverage": 1.0}), encoding="utf-8"
        )
        outputs = _read_outputs(out)
        assert outputs.scores_checksum is not None
        assert len(outputs.scores_checksum) == 64
        assert outputs.metrics == {"coverage": 1.0}
        assert outputs.problems == []

    def test_missing_dir_and_files(self, tmp_path: Path) -> None:
        outputs = _read_outputs(tmp_path / "missing")
        assert outputs.scores_checksum is None
        assert any("输出目录不存在" in p for p in outputs.problems)

    def test_corrupt_scores(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "scores.parquet").write_bytes(b"not parquet")
        outputs = _read_outputs(out)
        assert any("不可解析" in p for p in outputs.problems)


class TestStageWriteCode:
    def test_writes_files_and_params(self, tmp_path: Path) -> None:
        _stage_write_code(
            tmp_path, {"factor.py": "x = 1\n", "manifest.toml": "[manifest]\n"},
            {"window": 5},
        )
        assert (tmp_path / "code" / "factor.py").read_text() == "x = 1\n"
        params = json.loads(
            (tmp_path / "code" / "params.json").read_text(encoding="utf-8")
        )
        assert params == {"window": 5}

    def test_no_params_no_file(self, tmp_path: Path) -> None:
        _stage_write_code(
            tmp_path, {"factor.py": "x = 1\n"}, None
        )
        assert not (tmp_path / "code" / "params.json").exists()
