"""内置执行器(issue #117 / #142 / #143 / #144 / #171)。

* ``echo`` —— 把 payload 原样回显到 result_ref,验证 API → DB 队列 → worker
  领取 → 执行 → 状态收口的端到端链路(#142 基础设施自检)。
* ``research_run`` —— 把 ResearchRunCoordinator 接入统一队列(#143)。
* ``bulk_download`` / ``feature_snapshot`` / ``dataset_publish`` /
  ``backtest_run`` / ``data_sync`` / ``fetch_all`` / ``quality_repair``
  —— 把 7 类数据域耗时任务从内存态/同步阻塞迁移到统一队列(#144)。
* ``research_data_sync`` —— research 数据表(估值 / 财务 / 行业)摄取编排(#171)。
* ``research_code_run`` —— 研究代码沙箱执行(一次性 Docker 容器,#216;
  单并发,run 记录 code commit x 数据 release x 输出 checksum 三向引用)。
* ``factor_series_build`` —— 内容寻址因子序列构建(#360;单并发,复用
  research_code_run 的 worker 槽位约定;缓存命中 unchanged / 前缀不变性
  审计抽样 / ``research_factor_series`` 幂等落库)。
* ``validation_experiment`` —— #57 验证实验执行(walk-forward + 一次性揭盲,
  #233;补齐 #219 晋级门 OOS 半边的运营入口,单并发)。
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
from finboard_backtest.background_jobs.executors.factor_series_build import (
    FactorSeriesBuildExecutor,
)
from finboard_backtest.background_jobs.executors.feature_snapshot import (
    FeatureSnapshotExecutor,
)
from finboard_backtest.background_jobs.executors.fetch_all import DataFetchAllExecutor
from finboard_backtest.background_jobs.executors.quality_repair import (
    QualityRepairExecutor,
)
from finboard_backtest.background_jobs.executors.research_code_run import (
    ResearchCodeRunExecutor,
)
from finboard_backtest.background_jobs.executors.research_data_sync import (
    ResearchDataSyncExecutor,
)
from finboard_backtest.background_jobs.executors.research_run import (
    ResearchRunExecutor,
)
from finboard_backtest.background_jobs.executors.validation_experiment import (
    ValidationExperimentExecutor,
)

__all__ = [
    "BacktestRunExecutor",
    "BulkDownloadExecutor",
    "DataFetchAllExecutor",
    "DataSyncExecutor",
    "DatasetPublishExecutor",
    "EchoExecutor",
    "FactorSeriesBuildExecutor",
    "FeatureSnapshotExecutor",
    "QualityRepairExecutor",
    "ResearchCodeRunExecutor",
    "ResearchDataSyncExecutor",
    "ResearchRunExecutor",
    "ValidationExperimentExecutor",
]
