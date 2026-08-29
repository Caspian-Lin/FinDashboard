"""研究代码产物 repository 集成测试(需 PostgreSQL)。

覆盖:register(active/retired 生命周期) / get_active / list_artifacts /
rollback_to / 错误路径(非法 kind/status、回滚不存在的历史 commit)。
"""

from __future__ import annotations

import pytest

from finboard_persistence import ResearchCodeArtifactRepository


class TestResearchCodeArtifactRepository:
    async def test_register_and_get_active(self, db_session):
        repo = ResearchCodeArtifactRepository(db_session)
        record = await repo.register(
            kind="factor",
            name="momentum",
            commit="a" * 40,
            path="factors/momentum",
            checksum="deadbeef",
            created_by="agent:mcp",
        )
        await db_session.commit()
        assert record.artifact_id.startswith("RC-")
        assert record.status == "active"
        assert record.created_by == "agent:mcp"

        active = await repo.get_active(kind="factor", name="momentum")
        assert active is not None
        assert active.commit == "a" * 40

    async def test_resubmit_retires_previous(self, db_session):
        repo = ResearchCodeArtifactRepository(db_session)
        r1 = await repo.register(
            kind="factor",
            name="momentum",
            commit="a" * 40,
            path="factors/momentum",
            checksum="c1",
            created_by="agent:mcp",
        )
        r2 = await repo.register(
            kind="factor",
            name="momentum",
            commit="b" * 40,
            path="factors/momentum",
            checksum="c2",
            created_by="agent:mcp",
        )
        await db_session.commit()
        assert r2.status == "active"
        # register 返回的是登记时的快照;旧版本状态以 DB 重查为准
        r1_after = await repo.get(r1.artifact_id)
        assert r1_after is not None
        assert r1_after.status == "retired"
        active = await repo.get_active(kind="factor", name="momentum")
        assert active is not None
        assert active.commit == "b" * 40
        # 完整历史仍可查
        records = await repo.list_artifacts(kind="factor", name="momentum")
        assert len(records) == 2

    async def test_rollback_to_history_commit(self, db_session):
        repo = ResearchCodeArtifactRepository(db_session)
        r1 = await repo.register(
            kind="strategy",
            name="etf_rot",
            commit="a" * 40,
            path="strategies/etf_rot",
            checksum="c1",
            created_by="agent:mcp",
        )
        await repo.register(
            kind="strategy",
            name="etf_rot",
            commit="b" * 40,
            path="strategies/etf_rot",
            checksum="c2",
            created_by="agent:mcp",
        )
        await db_session.commit()
        rb = await repo.rollback_to(
            kind="strategy", name="etf_rot", commit=r1.commit
        )
        await db_session.commit()
        assert rb.status == "active"
        assert rb.commit == r1.commit
        active = await repo.get_active(kind="strategy", name="etf_rot")
        assert active is not None
        assert active.commit == r1.commit
        # 全部历史都在(r1 两次登记 + r2 retired)
        records = await repo.list_artifacts(kind="strategy", name="etf_rot")
        assert len(records) == 3

    async def test_rollback_missing_commit(self, db_session):
        repo = ResearchCodeArtifactRepository(db_session)
        with pytest.raises(LookupError):
            await repo.rollback_to(
                kind="factor", name="ghost", commit="c" * 40
            )

    async def test_register_invalid_kind(self, db_session):
        repo = ResearchCodeArtifactRepository(db_session)
        with pytest.raises(ValueError):  # noqa: PT011
            await repo.register(
                kind="widget",
                name="x",
                commit="a" * 40,
                path="widgets/x",
                checksum="c",
                created_by="a",
            )

    async def test_list_filters(self, db_session):
        repo = ResearchCodeArtifactRepository(db_session)
        await repo.register(
            kind="factor",
            name="f1",
            commit="a" * 40,
            path="factors/f1",
            checksum="c",
            created_by="a",
        )
        await repo.register(
            kind="strategy",
            name="s1",
            commit="b" * 40,
            path="strategies/s1",
            checksum="c",
            created_by="a",
        )
        await db_session.commit()
        factors = await repo.list_artifacts(kind="factor")
        assert factors
        assert all(r.kind == "factor" for r in factors)
        strategies = await repo.list_artifacts(kind="strategy")
        assert strategies
        assert all(r.kind == "strategy" for r in strategies)
        retired = await repo.list_artifacts(status="retired")
        assert retired == []
