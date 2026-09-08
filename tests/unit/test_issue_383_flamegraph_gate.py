"""issue #383:``job-flamegraph`` 门控纯函数 / scratch release_root 助手 /
共享执行器注册表 ``build_executor_registry``。
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

from finboard_app.cli import (
    _FLAMEGRAPH_REJECTED_KINDS,
    _FLAMEGRAPH_REPLAYABLE_KINDS,
    _flamegraph_gate_error,
    _make_scratch_release_root,
    build_executor_registry,
)

#: worker 装配的全部 kind(cli.build_executor_registry 单一事实源)。
_ALL_WORKER_KINDS = frozenset(
    {
        "echo",
        "research_run",
        "bulk_download",
        "feature_snapshot",
        "dataset_publish",
        "backtest_run",
        "data_sync",
        "fetch_all",
        "quality_repair",
        "research_data_sync",
        "research_code_run",
        "factor_series_build",
        "validation_experiment",
    }
)


class TestFlamegraphGate:
    def test_all_replayable_kinds_pass_when_succeeded(self) -> None:
        for kind in _FLAMEGRAPH_REPLAYABLE_KINDS:
            assert _flamegraph_gate_error(kind, "succeeded") is None, kind

    def test_failed_source_allowed_except_dataset_publish(self) -> None:
        for kind in _FLAMEGRAPH_REPLAYABLE_KINDS:
            error = _flamegraph_gate_error(kind, "failed")
            if kind == "dataset_publish":
                assert error is not None, kind
            else:
                assert error is None, kind

    def test_non_terminal_rejected(self) -> None:
        for status in (
            "queued",
            "running",
            "retry_waiting",
            "interrupted",
            "cancel_requested",
        ):
            error = _flamegraph_gate_error("echo", status)
            assert error is not None
            assert "终态" in error

    def test_rejected_kinds_have_named_reason(self) -> None:
        for kind in _FLAMEGRAPH_REJECTED_KINDS:
            error = _flamegraph_gate_error(kind, "succeeded")
            assert error is not None
            assert "不支持诊断重放" in error

    def test_unknown_kind_rejected(self) -> None:
        error = _flamegraph_gate_error("definitely_not_a_kind", "succeeded")
        assert error is not None
        assert "未知 job kind" in error

    def test_dataset_publish_failed_source_rejected(self) -> None:
        error = _flamegraph_gate_error("dataset_publish", "failed")
        assert error is not None
        assert "succeeded" in error

    def test_dataset_publish_cancelled_source_rejected(self) -> None:
        assert _flamegraph_gate_error("dataset_publish", "cancelled") is not None

    def test_gate_covers_every_worker_kind(self) -> None:
        """放行 + 拒绝两个集合必须覆盖 worker 注册的全部 kind,防止未来
        新增 kind 漏掉门控评估。"""

        covered = set(_FLAMEGRAPH_REPLAYABLE_KINDS) | set(_FLAMEGRAPH_REJECTED_KINDS)
        assert covered == set(_ALL_WORKER_KINDS)


class TestScratchReleaseRoot:
    def test_sets_env_and_mkdir(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("FINBOARD_DATA_RELEASE_ROOT", raising=False)
        try:
            scratch = _make_scratch_release_root(tmp_path)
            assert scratch.is_dir()
            assert scratch == tmp_path / "scratch-release-root"
            assert os.environ["FINBOARD_DATA_RELEASE_ROOT"] == str(scratch)
        finally:
            monkeypatch.delenv("FINBOARD_DATA_RELEASE_ROOT", raising=False)


class TestSharedRegistry:
    def test_kinds_match_worker_assembly(self) -> None:
        """诊断重放子进程与 worker 主循环共用同一装配函数,kind 集合一致。"""

        from finboard_app.config import Settings

        registry = build_executor_registry(Settings(), MagicMock())
        assert registry.kinds == _ALL_WORKER_KINDS

    def test_research_run_executor_registered(self) -> None:
        from finboard_app.config import Settings
        from finboard_backtest.background_jobs.executors.research_run import (
            ResearchRunExecutor,
        )

        registry = build_executor_registry(Settings(), MagicMock())
        executor = registry.get("research_run")
        assert isinstance(executor, ResearchRunExecutor)
