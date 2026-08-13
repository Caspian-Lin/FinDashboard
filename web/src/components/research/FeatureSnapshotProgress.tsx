import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Progress } from "@/components/ui/progress";
import { StatusBadge } from "@/components/ui/status-badge";
import type { JobOut } from "@/lib/api";

// 统一任务队列(#144)后,FeatureSnapshotProgress 改用 JobOut。
// JobOut 只有 progress_done/progress_total/phase/started_at/finished_at/error_summary,
// 不再提供 elapsed_seconds / estimated_remaining_seconds —— ETA 由前端按速度估算。
const STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "计算中",
  retry_waiting: "重试等待",
  cancel_requested: "取消中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

interface FeatureSnapshotProgressProps {
  job: JobOut;
}

function computeElapsedSeconds(job: JobOut): number {
  const start = job.started_at ? Date.parse(job.started_at) : 0;
  if (!start) return 0;
  const end = job.finished_at ? Date.parse(job.finished_at) : Date.now();
  const sec = (end - start) / 1000;
  return Number.isFinite(sec) && sec > 0 ? sec : 0;
}

export function FeatureSnapshotProgress({
  job,
}: FeatureSnapshotProgressProps) {
  const done = job.progress_done ?? 0;
  const total = job.progress_total ?? 0;
  const progress =
    total > 0 ? Math.min(100, Math.max(0, (done * 100) / total)) : 0;
  const elapsedSec = computeElapsedSeconds(job);
  // 速度估算:基于已完成的标的数与已耗时,反推剩余时间(只有 running 且有进度时才有意义)。
  const speedPerSec = elapsedSec > 0 && done > 0 ? done / elapsedSec : 0;
  const etaSec =
    speedPerSec > 0 && total > done ? (total - done) / speedPerSec : null;

  return (
    <div className="space-y-3 rounded-lg border border-border bg-muted/20 p-4" data-testid="feature-snapshot-progress">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">特征快照计算</span>
          <StatusBadge status={job.status}>
            {STATUS_LABELS[job.status] ?? job.status}
          </StatusBadge>
        </div>
        <span className="font-mono text-sm tabular-nums">
          {done} / {total} · {progress.toFixed(1)}%
        </span>
      </div>

      <Progress
        value={progress}
        aria-label="特征快照计算进度"
        aria-valuetext={`${done} / ${total}，${progress.toFixed(1)}%`}
        indicatorClassName={job.status === "failed" ? "bg-destructive" : undefined}
      />

      <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
        <span>
          已耗时：
          <strong className="font-mono font-normal text-foreground">
            {formatDuration(elapsedSec)}
          </strong>
        </span>
        <span>
          预计剩余：
          <strong className="font-mono font-normal text-foreground">
            {job.status === "queued"
              ? "任务开始后计算"
              : job.status === "running"
                ? etaSec !== null
                  ? formatDuration(etaSec)
                  : "计算中…"
                : "—"}
          </strong>
        </span>
      </div>

      {job.phase && (
        <p className="text-xs text-muted-foreground font-mono">{job.phase}</p>
      )}

      {(job.status === "failed" ||
        job.status === "interrupted" ||
        job.status === "cancelled") && (
        <Alert variant="destructive">
          <AlertTitle>{STATUS_LABELS[job.status] ?? "任务未成功"}</AlertTitle>
          <AlertDescription>
            {job.error_summary ?? "服务端未返回失败原因"}
          </AlertDescription>
        </Alert>
      )}
    </div>
  );
}

function formatDuration(sec: number): string {
  if (!isFinite(sec) || sec <= 0) return "—";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  if (h > 0) return `${h} 小时 ${m} 分 ${s} 秒`;
  if (m > 0) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
}
