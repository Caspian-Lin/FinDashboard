"""内置执行器(issue #117 / #142 / #143 / #144 / #171)。

* ``echo`` —— 把 payload 原样回显到 result_ref,验证 API → DB 队列 → worker
  领取 → 执行 → 状态收口的端到端链路(#142 基础设施自检)。
* ``research_run`` —— 把 ResearchRunCoordinator 接入统一队列(#143)。
* ``bulk_download`` / ``feature_snapshot`` / ``dataset_publish`` /
  ``backtest_run`` / ``data_sync`` / ``fetch_all`` / ``quality_repair``
  —— 把 7 类数据域耗时任务从内存态/同步阻塞迁移到统一队列(#144)。
* ``research_data_sync`` —— research 数据表(估值 / 财务 / 行业)摄取编排(#171)。
"""

from finboard_backtest.background_jobs.executors.backtest_run import (
    BacktestRunExecutor,
)
from finboard_backtest.background_jobs.executors.bulk_download import (
    BulkDownloadExecutor,
)
from finboard_backtest.background_jobs.executors.data_sync import DataSyncExecutor
from finboard_backtest.background_jobs.executors.dataset_publish import (
    DatasetPublishExecutor,
)
from finboard_backtest.background_jobs.executors.echo import EchoExecutor
from finboard_backtest.background_jobs.executors.feature_snapshot import (
    FeatureSnapshotExecutor,
)
from finboard_backtest.background_jobs.executors.fetch_all import DataFetchAllExecutor
from finboard_backtest.background_jobs.executors.quality_repair import (
    QualityRepairExecutor,
)
from finboard_backtest.background_jobs.executors.research_data_sync import (
    ResearchDataSyncExecutor,
)
from finboard_backtest.background_jobs.executors.research_run import (
    ResearchRunExecutor,
)

__all__ = [
    "BacktestRunExecutor",
    "BulkDownloadExecutor",
    "DataFetchAllExecutor",
    "DataSyncExecutor",
    "DatasetPublishExecutor",
    "EchoExecutor",
    "FeatureSnapshotExecutor",
    "QualityRepairExecutor",
    "ResearchDataSyncExecutor",
    "ResearchRunExecutor",
]
