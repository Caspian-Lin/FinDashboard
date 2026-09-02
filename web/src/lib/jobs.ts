import type { JobStatus } from "@/lib/api";
import type { LocalizedText } from "@/i18n";

/** 任务中心页用的展示映射;与后端 background_jobs kind / status 对齐。 */
export const JOB_STATUS_LABELS: Record<JobStatus, LocalizedText> = {
  queued: { zh: "排队中", en: "Queued" },
  running: { zh: "执行中", en: "Running" },
  retry_waiting: { zh: "等待重试", en: "Waiting to retry" },
  succeeded: { zh: "成功", en: "Succeeded" },
  failed: { zh: "失败", en: "Failed" },
  cancel_requested: { zh: "取消中", en: "Cancelling" },
  cancelled: { zh: "已取消", en: "Cancelled" },
  interrupted: { zh: "中断", en: "Interrupted" },
};

export const JOB_STATUS_OPTIONS: { value: JobStatus; label: LocalizedText }[] = [
  { value: "queued", label: { zh: "排队中", en: "Queued" } },
  { value: "running", label: { zh: "执行中", en: "Running" } },
  { value: "retry_waiting", label: { zh: "等待重试", en: "Waiting to retry" } },
  { value: "cancel_requested", label: { zh: "取消中", en: "Cancelling" } },
  { value: "succeeded", label: { zh: "成功", en: "Succeeded" } },
  { value: "failed", label: { zh: "失败", en: "Failed" } },
  { value: "cancelled", label: { zh: "已取消", en: "Cancelled" } },
  { value: "interrupted", label: { zh: "中断", en: "Interrupted" } },
];

/** 后端 worker 注册的全部 kind;新 kind 上线后同步补这里即可。 */
export const JOB_KINDS: { value: string; label: LocalizedText }[] = [
  { value: "echo", label: { zh: "自检 (echo)", en: "Echo check" } },
  { value: "research_run", label: { zh: "研究运行", en: "Research run" } },
  { value: "feature_snapshot", label: { zh: "特征快照", en: "Feature snapshot" } },
  { value: "bulk_download", label: { zh: "批量下载", en: "Bulk download" } },
  { value: "dataset_publish", label: { zh: "数据集发布", en: "Dataset publish" } },
  { value: "backtest_run", label: { zh: "回测", en: "Backtest" } },
  { value: "data_sync", label: { zh: "数据同步", en: "Data sync" } },
  { value: "fetch_all", label: { zh: "全量拉取", en: "Fetch all" } },
  { value: "quality_repair", label: { zh: "质量修复", en: "Quality repair" } },
  { value: "research_data_sync", label: { zh: "研究数据摄取", en: "Research data ingest" } },
];

/** 归档维度过滤(issue #221),与后端 ARCHIVE_FILTER_VALUES 对齐。 */
export const JOB_ARCHIVED_OPTIONS: { value: "exclude" | "only" | "all"; label: LocalizedText }[] =
  [
    { value: "exclude", label: { zh: "排除已归档", en: "Exclude archived" } },
    { value: "only", label: { zh: "仅已归档", en: "Archived only" } },
    { value: "all", label: { zh: "全部", en: "All" } },
  ];

export function jobKindLabel(kind: string, lang: "zh" | "en" = "zh"): string {
  const found = JOB_KINDS.find((k) => k.value === kind)?.label;
  return found ? found[lang] : kind;
}
