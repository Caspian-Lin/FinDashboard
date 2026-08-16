"""特征快照后台任务管理(已迁移到 ``finboard_backtest.feature_snapshot_jobs``)。

本模块保留为向后兼容垫片,直接 re-export 新位置的公开符号。
新代码应从 ``finboard_backtest`` 直接导入。
"""

from __future__ import annotations

from finboard_backtest.feature_snapshot_jobs import (
    FeatureSnapshotJob,
    FeatureSnapshotJobConflictError,
    FeatureSnapshotJobManager,
    FeatureSnapshotJobRunner,
    FeatureSnapshotJobStatus,
)

__all__ = [
    "FeatureSnapshotJob",
    "FeatureSnapshotJobConflictError",
    "FeatureSnapshotJobManager",
    "FeatureSnapshotJobRunner",
    "FeatureSnapshotJobStatus",
]
