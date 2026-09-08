import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FlameKindling } from "lucide-react";
import {
  api,
  flamegraphSvgUrl,
  isJobTerminal,
  type FlamegraphSession,
  type JobOut,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { StatusBadge } from "@/components/ui/status-badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { TimingLine } from "@/components/jobs/TimingLine";
import { jobKindLabel } from "@/lib/jobs";
import { formatDateTime } from "@/lib/utils";
import { useT } from "@/i18n";

const SESSION_POLL_MS = 3000;

const SESSION_STATUS_LABELS: Record<FlamegraphSession["status"], string> = {
  running: "jobs.flamegraphRunning",
  done: "jobs.flamegraphDone",
  failed: "jobs.flamegraphFailed",
  orphaned: "jobs.flamegraphOrphaned",
};

/**
 * 任务诊断重放(火焰图)面板(issue #373 服务化,#383 CLI 同源门控)。
 *
 * 仅终态 job 渲染;kind 门控经能力表判定 —— 拒绝 kind 按钮置灰 + tooltip
 * 原因;放行 kind 触发前弹确认框明示重放副作用,202 后轮询会话列表,
 * done 会话内嵌火焰图 svg + 重放 timing/result 摘要 + 下载入口。
 */
export function FlamegraphPanel({ job }: { job: JobOut }) {
  const { t, lang } = useT();
  const queryClient = useQueryClient();
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [actionError, setActionError] = React.useState<string | null>(null);

  const terminal = isJobTerminal(job);
  const { data: meta } = useQuery({
    queryKey: ["flamegraph-meta"],
    queryFn: () => api.flamegraphMeta(),
    enabled: terminal,
    staleTime: Infinity,
  });
  const sessionsQuery = useQuery({
    queryKey: ["flamegraph", job.job_id],
    queryFn: () => api.listFlamegraphSessions(job.job_id),
    enabled: terminal,
    refetchInterval: (query) =>
      query.state.data?.some((s) => s.status === "running") ? SESSION_POLL_MS : false,
  });
  const sessions = sessionsQuery.data ?? [];
  const hasRunning = sessions.some((s) => s.status === "running");

  const sideEffect = meta?.replayable_kinds[job.kind];
  const rejectedReason = meta ? (sideEffect === undefined ? (meta.rejected_kinds[job.kind] ?? t("jobs.flamegraphUnknownKind")) : null) : null;

  const startMutation = useMutation({
    mutationFn: () => api.startFlamegraph(job.job_id),
    onSuccess: () => {
      setConfirmOpen(false);
      setActionError(null);
      queryClient.invalidateQueries({ queryKey: ["flamegraph", job.job_id] });
    },
    onError: (err: Error) => {
      setConfirmOpen(false);
      setActionError(err.message);
    },
  });

  if (!terminal) return null;

  return (
    <section className="space-y-2" aria-label={t("jobs.flamegraphTitle")}>
      <div className="flex items-center justify-between gap-2">
        <p className="flex items-center gap-1 text-xs text-muted-foreground">
          <FlameKindling className="h-3.5 w-3.5" />
          {t("jobs.flamegraphTitle")}
        </p>
        <Button
          variant="outline"
          size="sm"
          disabled={sideEffect === undefined || hasRunning || startMutation.isPending}
          title={
            rejectedReason ??
            (hasRunning ? t("jobs.flamegraphConflict") : sideEffect) ??
            undefined
          }
          onClick={() => setConfirmOpen(true)}
        >
          <FlameKindling className="h-4 w-4" />
          {t("jobs.flamegraphStart")}
        </Button>
      </div>

      {actionError && (
        <Alert variant="destructive">
          <AlertDescription>{t("jobs.actionFailed")}{actionError}</AlertDescription>
        </Alert>
      )}

      {sessions.length > 0 && (
        <ul className="space-y-2">
          {sessions.map((session) => (
            <SessionItem key={session.session_id} session={session} />
          ))}
        </ul>
      )}

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("jobs.flamegraphConfirmTitle")}</DialogTitle>
            <DialogDescription>
              {t("jobs.flamegraphConfirmDesc", {
                id: job.job_id,
                kind: jobKindLabel(job.kind, lang),
              })}
            </DialogDescription>
          </DialogHeader>
          {sideEffect && (
            <p className="rounded-md border border-border bg-muted/40 p-3 text-xs whitespace-pre-wrap break-words">
              {sideEffect}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setConfirmOpen(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              size="sm"
              disabled={startMutation.isPending}
              onClick={() => startMutation.mutate()}
            >
              {startMutation.isPending ? t("jobs.flamegraphStarting") : t("common.confirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

function SessionItem({ session }: { session: FlamegraphSession }) {
  const { t } = useT();
  const svgUrl = flamegraphSvgUrl(session.meta?.job_id ?? "", session.session_id);
  return (
    <li className="space-y-2 rounded-md border border-border p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={session.status}>
          {t(SESSION_STATUS_LABELS[session.status])}
        </StatusBadge>
        <span className="font-mono text-xs text-muted-foreground">
          {session.session_id}
        </span>
        {session.meta && (
          <span className="text-xs text-muted-foreground">
            {formatDateTime(session.meta.started_at)}
          </span>
        )}
      </div>
      {session.status === "failed" && session.result?.error_summary && (
        <pre className="max-h-32 overflow-auto scrollbar-thin rounded-md border border-border bg-background p-2 font-mono text-xs whitespace-pre-wrap break-words">
          {session.result.error_summary}
        </pre>
      )}
      {session.status === "done" && (
        <>
          <dl className="grid grid-cols-1 gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
            <div>
              <dt className="text-muted-foreground">{t("jobs.flamegraphReplayStatus")}</dt>
              <dd className="font-mono">{session.result?.status ?? "—"}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">{t("jobs.timing")}</dt>
              <dd>
                <TimingLine timing={session.timing} />
              </dd>
            </div>
          </dl>
          <figure className="space-y-1">
            <img
              src={svgUrl}
              alt={t("jobs.flamegraphAlt", { id: session.session_id })}
              className="max-h-96 w-full rounded-md border border-border bg-background"
              loading="lazy"
            />
            <figcaption>
              <a href={svgUrl} download={`${session.session_id}-flamegraph.svg`}>
                <Button variant="ghost" size="sm" className="h-7 px-2 text-xs">
                  {t("jobs.flamegraphDownload")}
                </Button>
              </a>
            </figcaption>
          </figure>
        </>
      )}
    </li>
  );
}
