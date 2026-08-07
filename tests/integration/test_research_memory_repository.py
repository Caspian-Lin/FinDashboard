"""研究记忆 repository 集成测试(需 PostgreSQL)。

覆盖:remember / get / list(过滤) / forget / correct(纠正链) / confirm /
archive / 错误路径(不存在 / 非 active / 非法类型 / 空内容)。
"""

from __future__ import annotations

import pytest

from finboard_persistence import ResearchMemoryRepository, SourceRef


class TestResearchMemoryRepository:
    async def test_remember_and_get(self, db_session):
        repo = ResearchMemoryRepository(db_session)
        record = await repo.remember(
            memory_type="insight",
            content="RR-abc 在 OOS 样本外夏普 1.2",
            source_refs=[SourceRef(kind="research_run", ref_id="RR-abc")],
            tags=["momentum"],
            created_by="agent:mcp",
            conversation_id="CONV-1",
        )
        await db_session.commit()
        assert record.memory_id.startswith("RM-")
        assert record.status == "active"
        assert record.memory_type == "insight"
        assert record.source_refs[0].ref_id == "RR-abc"
        assert record.conversation_id == "CONV-1"
        assert "momentum" in record.tags

        fetched = await repo.get(record.memory_id)
        assert fetched is not None
        assert fetched.content == "RR-abc 在 OOS 样本外夏普 1.2"

    async def test_remember_invalid_type(self, db_session):
        repo = ResearchMemoryRepository(db_session)
        with pytest.raises(ValueError):  # noqa: PT011
            await repo.remember(memory_type="bad", content="x", created_by="a")

    async def test_remember_empty_content(self, db_session):
        repo = ResearchMemoryRepository(db_session)
        with pytest.raises(ValueError):  # noqa: PT011
            await repo.remember(memory_type="note", content="   ", created_by="a")

    async def test_list_filters(self, db_session):
        repo = ResearchMemoryRepository(db_session)
        await repo.remember(
            memory_type="note",
            content="n1",
            source_refs=[SourceRef(kind="research_run", ref_id="RR-1")],
            tags=["alpha"],
            created_by="x",
        )
        await repo.remember(
            memory_type="insight",
            content="i1",
            source_refs=[SourceRef(kind="simulation", ref_id="SIM-1")],
            tags=["beta"],
            created_by="x",
        )
        await db_session.commit()

        all_active = await repo.list_memories(status="active")
        assert len(all_active) == 2

        by_type = await repo.list_memories(memory_type="insight")
        assert len(by_type) == 1
        assert by_type[0].content == "i1"

        by_source_kind = await repo.list_memories(source_kind="simulation")
        assert len(by_source_kind) == 1
        assert by_source_kind[0].source_refs[0].ref_id == "SIM-1"

        by_source_ref = await repo.list_memories(source_ref="RR-1")
        assert len(by_source_ref) == 1

        by_tag = await repo.list_memories(tag="alpha")
        assert len(by_tag) == 1

    async def test_forget(self, db_session):
        repo = ResearchMemoryRepository(db_session)
        r = await repo.remember(memory_type="note", content="temp", created_by="x")
        await db_session.commit()
        forgotten = await repo.forget(r.memory_id)
        await db_session.commit()
        assert forgotten.status == "forgotten"
        # forgotten 仍可 get(保留审计)
        fetched = await repo.get(r.memory_id)
        assert fetched is not None
        assert fetched.status == "forgotten"

    async def test_correct_chain(self, db_session):
        repo = ResearchMemoryRepository(db_session)
        old = await repo.remember(
            memory_type="insight",
            content="wrong conclusion",
            tags=["t1"],
            created_by="x",
        )
        await db_session.commit()
        new = await repo.correct(
            memory_id=old.memory_id,
            content="corrected conclusion",
            tags=["t1", "t2"],
            created_by="x",
        )
        await db_session.commit()
        assert new.status == "active"
        assert new.supersedes_id == old.memory_id
        assert new.content == "corrected conclusion"
        assert new.memory_type == old.memory_type  # 继承类型
        assert "t2" in new.tags
        # 旧记忆标记 forgotten
        old_fetched = await repo.get(old.memory_id)
        assert old_fetched is not None
        assert old_fetched.status == "forgotten"

    async def test_correct_inherits_source_refs(self, db_session):
        repo = ResearchMemoryRepository(db_session)
        old = await repo.remember(
            memory_type="note",
            content="orig",
            source_refs=[SourceRef(kind="dataset", ref_id="DS-1")],
            created_by="x",
        )
        await db_session.commit()
        new = await repo.correct(memory_id=old.memory_id, content="fixed", created_by="x")
        await db_session.commit()
        # 不传 source_refs 则继承
        assert len(new.source_refs) == 1
        assert new.source_refs[0].ref_id == "DS-1"

    async def test_confirm(self, db_session):
        repo = ResearchMemoryRepository(db_session)
        r = await repo.remember(
            memory_type="confirmation", content="verified", created_by="x"
        )
        await db_session.commit()
        confirmed = await repo.confirm(r.memory_id, actor="user:alice")
        await db_session.commit()
        assert confirmed.confirmed_by == "user:alice"
        assert confirmed.confirmed_at is not None

    async def test_archive(self, db_session):
        repo = ResearchMemoryRepository(db_session)
        r = await repo.remember(memory_type="note", content="stale", created_by="x")
        await db_session.commit()
        archived = await repo.archive(r.memory_id)
        await db_session.commit()
        assert archived.status == "archived"

    async def test_forget_nonexistent(self, db_session):
        repo = ResearchMemoryRepository(db_session)
        with pytest.raises(LookupError):
            await repo.forget("RM-missing")

    async def test_archive_non_active(self, db_session):
        repo = ResearchMemoryRepository(db_session)
        r = await repo.remember(memory_type="note", content="x", created_by="a")
        await db_session.commit()
        await repo.forget(r.memory_id)
        await db_session.commit()
        # 已 forgotten 的记忆不能再 archive
        with pytest.raises(ValueError):  # noqa: PT011
            await repo.archive(r.memory_id)
