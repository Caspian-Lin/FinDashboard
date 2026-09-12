"""``research_factor_series`` 工件存储集成测试(需 PostgreSQL,issue #463)。

覆盖:工件行 upsert(values 落 NULL + relpath/checksum 落列)/ get 回读
惰性 values 逐值等值 / find_matching 不触工件文件即可命中(缓存检查)/
同 key 不同 checksum 冲突 fail-closed / 行内旧行与工件行并存。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_data.factor_series_store import write_series_artifact
from finboard_persistence import (
    FactorSeriesConflictError,
    FactorSeriesRecord,
    FactorSeriesRepository,
)
from tests.integration.conftest import TEST_DB_URL  # noqa: F401 - 连接自检

_COMMIT = "c" * 40

_ARTIFACT_ROOT = ".tmp/test-463-artifacts"


def _kwargs(name: str = "return_21d") -> dict[str, object]:
    return {
        "code_artifact": name,
        "code_commit": _COMMIT,
        "kind": "predefined_factor",
        "release_id": "DR-bars-463",
        "dataset_release_ids": ("DR-daily-463",),
        "params": {},
        "window_start": date(2024, 1, 1),
        "window_end": date(2024, 3, 31),
        "dates": (date(2024, 1, 31), date(2024, 2, 29)),
    }


def _values() -> dict[str, dict[str, float | None]]:
    return {
        "2024-01-31": {"600000.SH": 1.5, "000001.SZ": None},
        "2024-02-29": {"600000.SH": 2.0, "000001.SZ": 0.5},
    }


def _artifact_record(name: str = "return_21d") -> FactorSeriesRecord:
    return FactorSeriesRecord.build_artifact(
        **_kwargs(name),  # type: ignore[arg-type]
        artifact_relpath=f"{_COMMIT[:2]}/{name}.parquet",
        artifact_checksum="d" * 64,
    )


@pytest.fixture
def artifact_root(tmp_path_factory: pytest.TempPathFactory) -> str:
    return str(tmp_path_factory.mktemp("artifacts"))


class TestArtifactRows:
    async def test_upsert_artifact_row_round_trip(
        self, db_session: AsyncSession, artifact_root: str
    ) -> None:
        values = _values()
        record = FactorSeriesRecord.build_artifact(
            **_kwargs(),  # type: ignore[arg-type]
            artifact_relpath="placeholder",
            artifact_checksum="0" * 64,
        )
        meta = write_series_artifact(artifact_root, record.series_key, values)
        record = FactorSeriesRecord.build_artifact(
            **_kwargs(),  # type: ignore[arg-type]
            artifact_relpath=meta.relpath,
            artifact_checksum=meta.checksum,
        )
        repo = FactorSeriesRepository(db_session, artifact_root=artifact_root)
        persisted = await repo.upsert(record)
        await db_session.commit()

        assert persisted.artifact_relpath == meta.relpath
        loaded = await repo.get(record.series_id)
        assert loaded is not None
        assert loaded.artifact_relpath == meta.relpath
        # 惰性 values 逐值等值(显式 None 保留;缺行标的缺失)
        assert loaded.values.get("2024-01-31") == values["2024-01-31"]
        assert loaded.values["2024-02-29"] == values["2024-02-29"]
        assert loaded.dates == record.dates

    async def test_find_matching_hits_without_touching_file(
        self, db_session: AsyncSession, artifact_root: str
    ) -> None:
        # 指向不存在的工件文件:缓存检查只依赖行内列,不得读文件
        repo = FactorSeriesRepository(db_session, artifact_root=artifact_root)
        await repo.upsert(_artifact_record())
        await db_session.commit()
        matched = await repo.find_matching(
            code_artifact="return_21d",
            release_id="DR-bars-463",
            dataset_release_ids=("DR-daily-463",),
            params={},
            window_start=date(2024, 1, 1),
            window_end=date(2024, 3, 31),
        )
        assert matched is not None
        assert matched.series_id == _artifact_record().series_id

    async def test_checksum_conflict_fail_closed(
        self, db_session: AsyncSession, artifact_root: str
    ) -> None:
        repo = FactorSeriesRepository(db_session, artifact_root=artifact_root)
        await repo.upsert(_artifact_record())
        await db_session.commit()
        conflicting = FactorSeriesRecord.build_artifact(
            **_kwargs(),  # type: ignore[arg-type]
            artifact_relpath="ab/other.parquet",
            artifact_checksum="e" * 64,
        )
        with pytest.raises(FactorSeriesConflictError, match="content_checksum 不一致"):
            await repo.upsert(conflicting)

    async def test_inline_row_unchanged_alongside_artifact(
        self, db_session: AsyncSession, artifact_root: str
    ) -> None:
        inline = FactorSeriesRecord.build(**_kwargs(name="mom20"), values=_values())  # type: ignore[arg-type]
        repo = FactorSeriesRepository(db_session, artifact_root=artifact_root)
        persisted = await repo.upsert(inline)
        await db_session.commit()
        assert persisted.artifact_relpath is None
        assert persisted.values == _values()
        loaded = await repo.get(inline.series_id)
        assert loaded is not None
        assert loaded.values == _values()
