"""课题缺口 → 全局补数 → 复用发布(issue #511)。

模块分工:

* :mod:`contracts` —— 冻结契约(请求 spec / 计划 / 明细 / 配额 / 状态枚举)
  与稳定 fingerprint;#510 的 ``ScopeResolution`` 不落库,补数请求携带
  scope 复算比对;
* :mod:`support` —— 支持清单(由 #509 目录注册表推导)与运行时能力探测;
* :mod:`planner` —— 纯读预检:已完成 并集 在途覆盖、区间差、动作判定、估算、
  配额、发布影响、blockers;
* :mod:`service` —— 提交/幂等复用/部分重叠/重试/取消;
* :mod:`executor` —— ``kind=data_backfill`` 编排既有
  ``dataset_sync`` / ``bulk_download`` / ``quality_repair`` /
  ``dataset_publish`` 执行器,不复制同步或发布逻辑。

语义边界:补数只产生**新的不可变发布**(旧发布只读);异源与不同复权口径
不混接;成功后发布可被其他课题的 #510 scope 解析直接选用。
"""

from finboard_backtest.data_backfill.contracts import (
    BACKFILL_VERSION,
    INFLIGHT_REQUEST_STATUSES,
    BackfillAction,
    BackfillBlocker,
    BackfillCoverage,
    BackfillDatasetSpec,
    BackfillGapSymbols,
    BackfillItem,
    BackfillItemStatus,
    BackfillOverlap,
    BackfillPlan,
    BackfillPlanDriftError,
    BackfillPlanError,
    BackfillPublishImpact,
    BackfillPublishPreview,
    BackfillQuota,
    BackfillRequestSpec,
    BackfillRequestStatus,
    generate_backfill_request_id,
    request_fingerprint,
)
from finboard_backtest.data_backfill.executor import (
    DataBackfillExecutor,
    release_id_for,
    version_for,
)
from finboard_backtest.data_backfill.planner import (
    estimate_requests,
    intersect_ranges,
    merge_ranges,
    plan_backfill,
    subtract_ranges,
)
from finboard_backtest.data_backfill.service import (
    BackfillRequestError,
    BackfillSubmitResult,
    cancel_backfill,
    retry_backfill,
    submit_backfill,
)
from finboard_backtest.data_backfill.support import (
    BACKFILL_SUPPORT,
    BackfillCapabilities,
    BackfillSupport,
    BudgetSnapshot,
    probe_capabilities,
    support_for,
)

__all__ = [
    "BACKFILL_SUPPORT",
    "BACKFILL_VERSION",
    "INFLIGHT_REQUEST_STATUSES",
    "BackfillAction",
    "BackfillBlocker",
    "BackfillCapabilities",
    "BackfillCoverage",
    "BackfillDatasetSpec",
    "BackfillGapSymbols",
    "BackfillItem",
    "BackfillItemStatus",
    "BackfillOverlap",
    "BackfillPlan",
    "BackfillPlanDriftError",
    "BackfillPlanError",
    "BackfillPublishImpact",
    "BackfillPublishPreview",
    "BackfillQuota",
    "BackfillRequestError",
    "BackfillRequestSpec",
    "BackfillRequestStatus",
    "BackfillSubmitResult",
    "BackfillSupport",
    "BudgetSnapshot",
    "DataBackfillExecutor",
    "cancel_backfill",
    "estimate_requests",
    "generate_backfill_request_id",
    "intersect_ranges",
    "merge_ranges",
    "plan_backfill",
    "probe_capabilities",
    "release_id_for",
    "request_fingerprint",
    "retry_backfill",
    "submit_backfill",
    "subtract_ranges",
    "support_for",
    "version_for",
]
