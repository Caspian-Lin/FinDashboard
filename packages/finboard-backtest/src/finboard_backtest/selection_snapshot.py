"""snapshot 输入模式的 ``FactorResearchReader`` 适配器(issue #173)。

把冻结的 ``FeatureSnapshot``(JSON-in-DB)反序列化为 ``FactorInputBatch``:
按 ``snapshot_id`` 读取观测,仅保留 ``available_at <= decision_at`` 的条目
(与 ``frozen_loader`` 的 FeatureSnapshotProvider 过滤口径一致)。

快照观测自带因子值,因此 profile / daily / financial 恒为 None:
ST / 上市天数 / 退市过滤在 snapshot 模式下由 selection 降级为不生效。
source 语义放行:batch.source 直接返回调用方要求的 source,不比对观测来源。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import TYPE_CHECKING

from finboard_data.factors import FactorInputBatch, FactorInputRecord

if TYPE_CHECKING:
    from finboard_backtest.research_run.frozen_loader import FeatureSnapshotProvider
    from finboard_data.factor_lab import FeatureObservation


class FeatureSnapshotFactorReader:
    """按 snapshot_id 加载冻结快照观测的 ``FactorResearchReader`` 实现。"""

    def __init__(
        self,
        snapshot_provider: FeatureSnapshotProvider,
        snapshot_ids: tuple[str, ...],
    ) -> None:
        self._snapshot_provider = snapshot_provider
        self._snapshot_ids = snapshot_ids

    async def load_factor_inputs(
        self,
        *,
        symbols: tuple[str, ...],
        business_date: date,
        decision_at: datetime,
        source: str,
        required_datasets: frozenset[str],
        dataset_versions: dict[str, str],
    ) -> FactorInputBatch:
        wanted = set(symbols)
        issues: list[str] = []
        by_symbol: dict[str, list[FeatureObservation]] = defaultdict(list)
        for snapshot_id in self._snapshot_ids:
            snapshot = await self._snapshot_provider(snapshot_id)
            if snapshot is None:
                issues.append(f"snapshot_not_found:{snapshot_id}")
                continue
            for observation in snapshot.observations:
                if observation.available_at > decision_at:
                    continue
                if observation.symbol in wanted:
                    by_symbol[observation.symbol].append(observation)
        return FactorInputBatch(
            records=tuple(
                FactorInputRecord(
                    symbol=symbol,
                    profile=None,
                    daily=None,
                    financial=None,
                    industry=None,
                    features=tuple(by_symbol[symbol]),
                )
                for symbol in symbols
                if symbol in by_symbol
            ),
            source=source,
            dataset_versions={"feature_snapshots": ",".join(self._snapshot_ids)},
            issues=tuple(issues),
        )


__all__ = ["FeatureSnapshotFactorReader"]
