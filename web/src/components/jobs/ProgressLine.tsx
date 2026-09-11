import { Progress } from "@/components/ui/progress";
import { isJobRunning, type JobOut } from "@/lib/api";
import { formatJobElapsed, parseJobPhase } from "@/lib/jobs";
import { useT } from "@/i18n";
import { timeAgo } from "@/lib/utils";

/**
 * phase 展示文本(issue #308):research_run 的加载 k/N 帧与决策级
 * `#序号@日期` 帧翻译为可读文案;其余(含其他 kind)原样返回。
 * t 由调用方传入(useT 的 t 是稳定的),避免组件文件间搬 hook。
 */
function phaseTextOf(
  phase: string | null,
  t: (path: string, params?: Record<string, string | number>) => string,
): string | null {
  const info = parseJobPhase(phase);
  if (!info) return phase;
  if (info.load) {
    return t("jobs.phaseLoad", {
      done: info.load.done,
      total: info.load.total,
    });
  }
  if (info.decision) {
    return t("jobs.phaseDecision", {
      index: info.decision.index,
      date: info.decision.date,
      stage: info.stage,
    });
  }
  return phase;
}

export interface ProgressLineProps {
  job: JobOut;
  /** row=任务列表行内紧凑(条 + done/total·百分比 + 阶段/时长一行);detail=JobDetail 展开(条 + 阶段 + 时长 + 时龄)。 */
  variant?: "row" | "detail";
}

/**
 * 任务进度行(issue #442):progress_done/total > 0 渲染进度条 + 百分比;
 * 「已运行 X 分 Y 秒」取 started_at → now(running 族,随轮询刷新;now 在
 * 渲染时读取,每次 refetch 的 isFetching 翻转自然带动重算),终态取
 * started_at → finished_at 定格;「最后更新 Xs 前」取 updated_at 时龄作心跳
 * 代理。缺失字段显示 —。运行中/终态样式区分:进度条与时长文案的配色不同。
 */
export function ProgressLine({ job, variant = "row" }: ProgressLineProps) {
  const { t, lang } = useT();
  const active = isJobRunning(job);
  const total = job.progress_total;
  const percent =
    total > 0 ? Math.min(100, Math.round((job.progress_done / total) * 100)) : null;
  const phaseText = phaseTextOf(job.phase, t);
  const endMs = active
    ? Date.now()
    : job.finished_at
      ? new Date(job.finished_at).getTime()
      : null;
  const elapsed = endMs == null ? null : formatJobElapsed(job.started_at, lang, endMs);
  const elapsedText = elapsed
    ? t(active ? "jobs.elapsed" : "jobs.elapsedTerminal", { duration: elapsed })
    : null;
  const updatedText = job.updated_at
    ? t("jobs.updatedAgo", { ago: timeAgo(job.updated_at, lang) })
    : null;
  const indicator = active ? undefined : "bg-muted-foreground/40";

  if (variant === "row") {
    const metaParts = [phaseText, elapsedText].filter(Boolean).join(" · ");
    return (
      <div
        className="min-w-40 space-y-0.5"
        data-job-progress={active ? "running" : "terminal"}
        data-variant={variant}
      >
        {percent !== null && (
          <div className="flex items-center gap-2">
            <Progress
              value={percent}
              className="w-24"
              indicatorClassName={indicator}
              aria-label={t("jobs.progressAria")}
            />
            <span className="whitespace-nowrap font-mono text-xs">
              {job.progress_done}/{total} · {percent}%
            </span>
          </div>
        )}
        <p
          className="whitespace-nowrap text-xs text-muted-foreground"
          title={job.phase ?? undefined}
        >
          {metaParts || "—"}
        </p>
      </div>
    );
  }

  return (
    <div
      className="space-y-1.5"
      data-job-progress={active ? "running" : "terminal"}
      data-variant={variant}
    >
      {percent !== null ? (
        <div className="flex items-center gap-2">
          <Progress
            value={percent}
            className="h-1.5 w-56"
            indicatorClassName={indicator}
            aria-label={t("jobs.progressAria")}
          />
          <span className="font-mono text-xs">{percent}%</span>
          <span className="font-mono text-xs text-muted-foreground">
            {job.progress_done}/{total}
          </span>
        </div>
      ) : (
        <span className="font-mono text-xs text-muted-foreground">—</span>
      )}
      <p className="text-xs" title={job.phase ?? undefined}>
        {phaseText ?? <span className="text-muted-foreground">—</span>}
      </p>
      <p className="text-xs text-muted-foreground">
        {elapsedText ? (
          <span className={active ? "text-foreground" : undefined}>{elapsedText}</span>
        ) : (
          "—"
        )}
        {" · "}
        {updatedText ?? "—"}
      </p>
    </div>
  );
}
