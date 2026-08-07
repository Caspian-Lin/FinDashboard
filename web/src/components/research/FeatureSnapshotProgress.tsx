import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Progress } from "@/components/ui/progress";
import { StatusBadge } from "@/components/ui/status-badge";
import type { FeatureSnapshotJobStatus } from "@/lib/research";
import { formatDuration } from "./FeatureSnapshotProgress.utils";

const STATUS_LABELS: Record<FeatureSnapshotJobStatus["status"], string> = {
  queued: "排队中",
  running: "计算中",
  succeeded: "已完成",
  failed: "失败",
};

interface FeatureSnapshotProgressProps {
  job: FeatureSnapshotJobStatus;
}

export function FeatureSnapshotProgress({
  job,
}: FeatureSnapshotProgressProps) {
  const progress = Math.min(100, Math.max(0, job.progress_pct));
  const eta =
    job.estimated_remaining_seconds == null
      ? job.status === "queued"
        ? "任务开始后计算"
        : job.status === "running"
          ? "计算中…"
          : "—"
      : formatDuration(job.estimated_remaining_seconds);

  return (
    <div className="space-y-3 rounded-lg border border-border bg-muted/20 p-4" data-testid="feature-snapshot-progress">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">特征快照计算</span>
          <StatusBadge status={job.status}>
            {STATUS_LABELS[job.status]}
          </StatusBadge>
        </div>
        <span className="font-mono text-sm tabular-nums">
          {job.completed_symbols} / {job.total_symbols} · {progress.toFixed(1)}%
        </span>
      </div>

      <Progress
        value={progress}
        aria-label="特征快照计算进度"
        aria-valuetext={`${job.completed_symbols} / ${job.total_symbols}，${progress.toFixed(1)}%`}
        indicatorClassName={job.status === "failed" ? "bg-destructive" : undefined}
      />

      <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
        <span>
          已耗时：<strong className="font-mono font-normal text-foreground">{formatDuration(job.elapsed_seconds)}</strong>
        </span>
        <span>
          预计剩余：<strong className="font-mono font-normal text-foreground">{eta}</strong>
        </span>
      </div>

      {job.status === "failed" && (
        <Alert variant="destructive">
          <AlertTitle>快照生成失败</AlertTitle>
          <AlertDescription>{job.error ?? "服务端未返回失败原因"}</AlertDescription>
        </Alert>
      )}
    </div>
  );
}
