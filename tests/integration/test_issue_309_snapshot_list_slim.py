"""issue #309:FeatureSnapshotRepository header 投影 + source_run_id 过滤。

真实 PostgreSQL(测试库由 conftest 自动创建):

* ``list_headers``——字段全部来自表列(含 feature_names/symbol_count/
  observation_count 覆盖统计),``as_dict()`` 不含 observations 键,
  decision_at 倒序与 :meth:`list` 一致;
* ``list`` / ``list_headers`` 的 ``source_run_id`` 过滤——RCR→快照映射
  一条查询(原无此过滤,#217 沙箱快照只能逐个单查);
* ``dataset_release_id`` 过滤行为保持。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_data.cache import ParquetCache
from finboard_data.factor_lab import FeatureObservation, build_feature_snapshot
from finboard_data.releases import DatasetReleaseSpec, ResearchDatasetRelease
from finboard_persistence import (
    FeatureSnapshotRepository,
    ResearchDatasetReleaseService,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

pytestmark = pytest.mark.asyncio

_SYMBOLS = ("510300.SH", "513100.SH", "518880.SH", "511010.SH")
_START = date(2024, 1, 2)
_END = date(2024, 1, 5)
_DECISION = datetime(2024, 1, 5, 8, tzinfo=UTC)
_RUN_A = "RCR-309aaaaaaaaaaaaaaaa"
_RUN_B = "RCR-309bbbbbbbbbbbbbbbb"


async def _publish_release(
    session: AsyncSession,
    tmp_path: Path,
) -> ResearchDatasetRelease:
    cache = ParquetCache(tmp_path / "cache")
    for symbol_index, code in enumerate(_SYMBOLS):
        symbol = Symbol(code, Market.A_SHARE)
        bars = [
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime.combine(
                    _START + timedelta(days=index),
                    datetime.min.time(),
                    tzinfo=UTC,
                ),
                open=Decimal(str(3 + symbol_index + index * 0.01)),
                high=Decimal(str(3.1 + symbol_index + index * 0.01)),
                low=Decimal(str(2.9 + symbol_index + index * 0.01)),
                close=Decimal(str(3 + symbol_index + index * 0.01)),
                volume=Decimal("1000000"),
                amount=Decimal("3000000"),
                source="fixed_sample",
            )
            for index in range(4)
        ]
        await cache.write(symbol, BarPeriod.D1, "qfq", bars)
    service = ResearchDatasetReleaseService(
        session,
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    )
    return await service.publish(
        DatasetReleaseSpec(
            release_id="integration-309-v1",
            dataset_name="factor_lab_integration",
            source="fixed_sample",
            version="integration-v1",
            start_date=_START,
            end_date=_END,
            code_version="integration",
            required_capabilities=(
                "etf:index",
                "etf:cross_border",
                "etf:commodity",
                "etf:bond",
            ),
        ),
        list(_SYMBOLS),
    )


def _observations(
    codes: tuple[str, ...],
    *,
    feature_name: str = "momentum",
    source_version: str = "integration-v1",
    decision_at: datetime = _DECISION,
) -> list[FeatureObservation]:
    return [
        FeatureObservation(
            symbol=code,
            feature_name=feature_name,
            value=float(index),
            observed_at=decision_at - timedelta(minutes=30),
            available_at=decision_at - timedelta(minutes=30),
            source="fixed_sample",
            source_version=source_version,
        )
        for index, code in enumerate(codes)
    ]


class TestIssue309SnapshotListSlim:
    async def test_list_headers_projection_and_filters(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        release = await _publish_release(db_session, tmp_path)
        repo = FeatureSnapshotRepository(db_session)

        release_snapshot = build_feature_snapshot(
            dataset_release_id=release.release_id,
            dataset_release_checksum=release.release_checksum,
            decision_at=_DECISION,
            code_version="integration",
            observations=_observations(_SYMBOLS, source_version=release.version),
            calculation_windows={"momentum": 20},
        )
        # 沙箱快照(#217):dataset_release_id=None + source_run_id,
        # decision_at 严格递减,保证 header 列表顺序确定。
        sandbox_a = build_feature_snapshot(
            dataset_release_id=None,
            dataset_release_checksum="m" * 64,
            decision_at=_DECISION - timedelta(hours=1),
            code_version="a" * 40,
            observations=_observations(
                ("S0", "S1"),
                feature_name="u_mom20",
                source_version="a" * 40,
                decision_at=_DECISION - timedelta(hours=1),
            ),
            source_run_id=_RUN_A,
        )
        sandbox_b = build_feature_snapshot(
            dataset_release_id=None,
            dataset_release_checksum="n" * 64,
            decision_at=_DECISION - timedelta(days=1),
            code_version="b" * 40,
            observations=_observations(
                ("S0",),
                feature_name="u_rev5",
                source_version="b" * 40,
                decision_at=_DECISION - timedelta(days=1),
            ),
            source_run_id=_RUN_B,
        )
        await repo.publish(release_snapshot)
        await repo.publish(sandbox_a)
        await repo.publish(sandbox_b)
        await db_session.commit()

        headers = await repo.list_headers()
        assert [h.snapshot_id for h in headers] == [
            release_snapshot.snapshot_id,
            sandbox_a.snapshot_id,
            sandbox_b.snapshot_id,
        ]

        first = headers[0].as_dict()
        # 瘦身核心断言:header 投影不携带逐条值
        assert "observations" not in first
        assert first["dataset_release_id"] == release.release_id
        assert first["source_run_id"] is None
        assert first["feature_names"] == ["momentum"]
        assert first["symbol_count"] == len(_SYMBOLS)
        assert first["observation_count"] == len(_SYMBOLS)
        assert first["checksum"] == release_snapshot.checksum

        sandbox_header = headers[1].as_dict()
        assert sandbox_header["dataset_release_id"] is None
        assert sandbox_header["source_run_id"] == _RUN_A
        assert sandbox_header["feature_names"] == ["u_mom20"]

        # source_run_id 过滤:RCR→快照映射一条查询(repo.list 全量语义不变)
        only_a = await repo.list(source_run_id=_RUN_A)
        assert [s.snapshot_id for s in only_a] == [sandbox_a.snapshot_id]
        assert only_a[0].source_run_id == _RUN_A
        only_b = await repo.list_headers(source_run_id=_RUN_B)
        assert [h.snapshot_id for h in only_b] == [sandbox_b.snapshot_id]
        assert await repo.list_headers(source_run_id="RCR-missing") == []

        # dataset_release_id 过滤行为保持
        release_only = await repo.list_headers(dataset_release_id=release.release_id)
        assert [h.snapshot_id for h in release_only] == [
            release_snapshot.snapshot_id
        ]

    async def test_list_still_returns_full_domain_objects(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        await _publish_release(db_session, tmp_path)
        repo = FeatureSnapshotRepository(db_session)
        sandbox = build_feature_snapshot(
            dataset_release_id=None,
            dataset_release_checksum="m" * 64,
            decision_at=_DECISION,
            code_version="a" * 40,
            observations=_observations(
                ("S0", "S1"), feature_name="u_mom20", source_version="a" * 40
            ),
            source_run_id=_RUN_A,
        )
        await repo.publish(sandbox)
        await db_session.commit()

        snapshots = await repo.list(source_run_id=_RUN_A)
        assert len(snapshots) == 1
        restored = snapshots[0]
        assert restored.source_run_id == _RUN_A
        # include_observations=true 兼容路径依赖 list 全量:observations 仍在
        assert {o.feature_name for o in restored.observations} == {"u_mom20"}
        as_dict = restored.as_dict()
        assert len(as_dict["observations"]) == 2  # type: ignore[arg-type]
