import { Progress } from "@/components/ui/progress";
import type { JobTiming } from "@/lib/api";
import { useT } from "@/i18n";

/**
 * job 耗时 / parquet IO 聚合展示(issue #383 落库、#373 任务中心透出)。
 *
 * 一行文案 + IO 占比小进度条:`read_elapsed_ms / execute 墙钟` 占比高 ≈
 * IO 瓶颈、低 ≈ CPU 瓶颈(配合火焰图定位热点)。timing=null(旧行 / 未
 * 走到收口)显示 —。
 */
export function TimingLine({ timing }: { timing: JobTiming | null }) {
  const { t } = useT();
  if (!timing) {
    return <span className="font-mono text-xs text-muted-foreground">—</span>;
  }
  const reads = timing.parquet_reads;
  const wallMs = timing.execute_elapsed_seconds * 1000;
  const share =
    wallMs > 0
      ? Math.min(100, Math.max(0, Math.round((reads.read_elapsed_ms / wallMs) * 100)))
      : 0;
  return (
    <div className="space-y-1.5">
      <p className="font-mono text-xs">
        {t("jobs.timingSummary", {
          seconds: timing.execute_elapsed_seconds.toFixed(1),
          ops: reads.read_ops,
          ms: Math.round(reads.read_elapsed_ms),
          mb: ((reads.read_bytes ?? 0) / 1e6).toFixed(1),
          share,
        })}
      </p>
      <div className="flex items-center gap-2">
        <Progress
          value={share}
          className="h-1.5 w-40"
          aria-label={t("jobs.timingIoAria")}
        />
        <span className="text-xs text-muted-foreground">
          {t("jobs.timingIoShare", { share })}
        </span>
      </div>
    </div>
  );
}
