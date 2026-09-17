"""research_code_runs repository 集成测试(需 PostgreSQL)。

覆盖:create(running)/ mark_terminal(succeeded/failed 三向引用补齐)/
get / list_runs / 错误路径(非法 kind/status、run 不存在)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from finboard_persistence import ResearchCodeRunRepository


def _create_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs = {
        "kind": "factor",
        "name": "mom20",
        "commit": "a" * 40,
        "code_checksum": "deadbeef",
        "dataset_release_ids": ["DR-1"],
        "dataset_release_checksums": {"DR-1": "cs1"},
        "decision_at": datetime(2024, 6, 3, 7, 0, tzinfo=UTC),
        "image": "finboard-research-sandbox:0.1.0",
        "image_digest": "sha256:abc",
        "artifact_dir": "data_cache/research_sandbox/RCR-x",
        "job_id": "BJ-1",
        "artifact_id": "RC-1",
        "params": {"window": 20},
    }
    kwargs.update(overrides)
    return kwargs


class TestResearchCodeRunRepository:
    async def test_create_running_then_succeeded(self, db_session):
        repo = ResearchCodeRunRepository(db_session)
        run = await repo.create(**_create_kwargs())
        await db_session.commit()
        assert run.run_id.startswith("RCR-")
        assert run.status == "running"
        assert run.dataset_release_ids == ["DR-1"]

        done = await repo.mark_terminal(
            run.run_id,
            status="succeeded",
            exit_code=0,
            metrics={"coverage": 1.0, "nan_ratio": 0.0},
            scores_checksum="ab" * 32,
            mount_manifest_checksum="cd" * 32,
            usage={"max_mem_mb": 512.0},
        )
        await db_session.commit()
        assert done.status == "succeeded"
        assert done.scores_checksum == "ab" * 32
        assert done.metrics is not None
        assert done.metrics["coverage"] == 1.0
        assert done.exit_code == 0

        fetched = await repo.get(run.run_id)
        assert fetched is not None
        assert fetched.status == "succeeded"

    async def test_mark_terminal_failed_with_audit_fields(self, db_session):
        repo = ResearchCodeRunRepository(db_session)
        run = await repo.create(**_create_kwargs(name="boom"))
        failed = await repo.mark_terminal(
            run.run_id,
            status="failed",
            error_code="oom_killed",
            error_summary="容器内存超限",
            exit_code=137,
            timed_out=False,
            oom_killed=True,
            usage={"max_mem_mb": 2100.0},
        )
        await db_session.commit()
        assert failed.status == "failed"
        assert failed.oom_killed is True
        assert failed.usage is not None
        assert failed.usage["max_mem_mb"] == 2100.0

    async def test_explicit_run_id(self, db_session):
        repo = ResearchCodeRunRepository(db_session)
        run = await repo.create(**_create_kwargs(), run_id="RCR-custom")
        await db_session.commit()
        assert run.run_id == "RCR-custom"

    async def test_invalid_kind_rejected(self, db_session):
        repo = ResearchCodeRunRepository(db_session)
        with pytest.raises(ValueError, match="kind"):
            await repo.create(**_create_kwargs(kind="widget"))

    async def test_invalid_terminal_status_rejected(self, db_session):
        repo = ResearchCodeRunRepository(db_session)
        run = await repo.create(**_create_kwargs())
        with pytest.raises(ValueError, match="status"):
            await repo.mark_terminal(run.run_id, status="queued")

    async def test_mark_terminal_unknown_run(self, db_session):
        repo = ResearchCodeRunRepository(db_session)
        with pytest.raises(LookupError, match="不存在"):
            await repo.mark_terminal("RCR-nope", status="failed")

    async def test_list_runs_filters(self, db_session):
        repo = ResearchCodeRunRepository(db_session)
        await repo.create(**_create_kwargs(name="f1"))
        await repo.create(
            **_create_kwargs(name="f1", commit="b" * 40)
        )
        await db_session.commit()
        rows = await repo.list_runs(kind="factor", name="f1", limit=10)
        assert len(rows) == 2
        assert all(r.name == "f1" for r in rows)
        with pytest.raises(ValueError, match="status"):
            await repo.list_runs(status="weird")
