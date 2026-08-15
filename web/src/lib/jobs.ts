import type { JobStatus } from "@/lib/api";

/** 任务中心页用的展示映射;与后端 background_jobs kind / status 对齐。 */
export const JOB_STATUS_LABELS: Record<JobStatus, string> = {
  queued: "排队中",
  running: "执行中",
  retry_waiting: "等待重试",
  succeeded: "成功",
  failed: "失败",
  cancel_requested: "取消中",
  cancelled: "已取消",
  interrupted: "中断",
};

export const JOB_STATUS_OPTIONS: { value: JobStatus; label: string }[] = [
  { value: "queued", label: "排队中" },
  { value: "running", label: "执行中" },
  { value: "retry_waiting", label: "等待重试" },
  { value: "cancel_requested", label: "取消中" },
  { value: "succeeded", label: "成功" },
  { value: "failed", label: "失败" },
  { value: "cancelled", label: "已取消" },
  { value: "interrupted", label: "中断" },
];

/** 后端 worker 注册的全部 kind;新 kind 上线后同步补这里即可。 */
export const JOB_KINDS: { value: string; label: string }[] = [
  { value: "echo", label: "自检 (echo)" },
  { value: "research_run", label: "研究运行" },
  { value: "feature_snapshot", label: "特征快照" },
  { value: "bulk_download", label: "批量下载" },
  { value: "dataset_publish", label: "数据集发布" },
  { value: "backtest_run", label: "回测" },
  { value: "data_sync", label: "数据同步" },
  { value: "fetch_all", label: "全量拉取" },
  { value: "quality_repair", label: "质量修复" },
];

export function jobKindLabel(kind: string): string {
  return JOB_KINDS.find((k) => k.value === kind)?.label ?? kind;
}
