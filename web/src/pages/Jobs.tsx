import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import {
  Archive,
  ArchiveRestore,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  ListTodo,
  RefreshCw,
} from "lucide-react";
import {
  api,
  isJobRunning,
  type JobArchivedFilter,
  type JobOut,
  type JobStatus,
} from "@/lib/api";
import {
  JOB_ARCHIVED_OPTIONS,
  JOB_KINDS,
  JOB_STATUS_LABELS,
  JOB_STATUS_OPTIONS,
  jobKindLabel,
  parseJobPhase,
} from "@/lib/jobs";
import { PageHeader } from "@/components/ui/page-header";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";
import { CopyButton } from "@/components/ui/copy-button";
import { FlamegraphPanel } from "@/components/jobs/FlamegraphPanel";
import { TimingLine } from "@/components/jobs/TimingLine";
import InfoHint from "@/components/InfoHint";
import { INFO_HINTS } from "@/lib/infoHints";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { cn, formatDateTime, timeAgo } from "@/lib/utils";
import { useT } from "@/i18n";

const ALL = "all";
const POLL_MS = 5000;
/** 每页条数(issue #373):默认前 10 条,翻页查看;页码入 URL search params。 */
const PAGE_SIZE = 10;

/** run 终态但非 completed 且 job 仍在运行族 → 两表状态不一致(RR-7a74 僵尸形态)。 */
const RUN_STUCK_STATUSES: ReadonlySet<string> = new Set([
  "interrupted",
  "failed",
  "cancelled",
]);

/**
 * phase 展示文本(issue #308):research_run 的加载 k/N 帧与决策级
 * `#序号@日期` 帧翻译为可读文案;其余(含其他 kind)原样返回。
 */
function usePhaseText(phase: string | null): string | null {
  const { t } = useT();
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

/** 任务中心:研究/数据/回测域统一后台任务的集中管理页(issue #161;#221 归档)。 */
export default function Jobs() {
  const { t, tl } = useT();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const statusFilter = searchParams.get("status") ?? ALL;
  const kindFilter = searchParams.get("kind") ?? ALL;
  const archivedFilter = (searchParams.get("archived") ?? "exclude") as JobArchivedFilter;
  const page = Math.max(1, Number.parseInt(searchParams.get("page") ?? "1", 10) || 1);
  const [expandedId, setExpandedId] = React.useState<string | null>(() => searchParams.get("job"));
  const [actionError, setActionError] = React.useState<string | null>(null);
  const [bulkNotice, setBulkNotice] = React.useState<string | null>(null);

  const setFilter = (key: "status" | "kind" | "archived", value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value === ALL || (key === "archived" && value === "exclude")) next.delete(key);
    else next.set(key, value);
    // 过滤条件变更时页码重置回第 1 页(issue #373)。
    next.delete("page");
    setSearchParams(next, { replace: true });
  };

  const setPage = React.useCallback(
    (next: number) => {
      const params = new URLSearchParams(searchParams);
      if (next <= 1) params.delete("page");
      else params.set("page", String(next));
      setSearchParams(params, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["jobs", statusFilter, kindFilter, archivedFilter, page],
    queryFn: () =>
      api.listJobs({
        status: statusFilter === ALL ? undefined : [statusFilter as JobStatus],
        kind: kindFilter === ALL ? undefined : [kindFilter],
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
        archived: archivedFilter,
      }),
    // 轮询只在还有非终态任务时进行;全部终态后自动停止,避免静态页面持续请求。
    refetchInterval: (query) => {
      const items = query.state.data?.items;
      return items && items.some(isJobRunning) ? POLL_MS : false;
    },
  });

  // 批量归档目标计数(issue #373):分页后当前页不再是全量 —— 用轻量
  // limit=1 查询取同过滤条件的精确总数(X-Total-Count)。
  const { data: bulkData } = useQuery({
    queryKey: ["jobs", "bulk-target-count"],
    queryFn: () =>
      api.listJobs({
        status: ["succeeded", "cancelled"],
        archived: "exclude",
        limit: 1,
      }),
    enabled: archivedFilter === "exclude",
    staleTime: 30_000,
  });

  const invalidateJobs = () => {
    queryClient.invalidateQueries({ queryKey: ["jobs"] });
  };

  const cancelMutation = useMutation({
    mutationFn: (jobId: string) => api.cancelJob(jobId),
    onSuccess: () => {
      setActionError(null);
      invalidateJobs();
    },
    onError: (err: Error) => setActionError(err.message),
  });

  const archiveMutation = useMutation({
    mutationFn: (jobId: string) => api.archiveJob(jobId),
    onSuccess: () => {
      setActionError(null);
      invalidateJobs();
    },
    onError: (err: Error) => setActionError(err.message),
  });

  const unarchiveMutation = useMutation({
    mutationFn: (jobId: string) => api.unarchiveJob(jobId),
    onSuccess: () => {
      setActionError(null);
      invalidateJobs();
    },
    onError: (err: Error) => setActionError(err.message),
  });

  // 批量归档「已完成」任务:只收 succeeded/cancelled,failed/interrupted 留在
  // 列表里便于排查(issue #221)。
  const bulkArchiveMutation = useMutation({
    mutationFn: () =>
      api.bulkArchiveJobs({ statuses: ["succeeded", "cancelled"], limit: 1000 }),
    onSuccess: (result) => {
      setActionError(null);
      setBulkNotice(t("jobs.bulkArchived", { count: result.archived_count }));
      invalidateJobs();
    },
    onError: (err: Error) => setActionError(err.message),
  });

  const jobs = data?.items ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  // 页码越界(过滤/归档后总页数缩水)自动回退到最后一页。
  React.useEffect(() => {
    if (!isLoading && total > 0 && page > totalPages) setPage(totalPages);
  }, [isLoading, total, page, totalPages, setPage]);
  const activeCount = jobs.filter(isJobRunning).length;
  const bulkTargetCount = bulkData?.total ?? 0;

  return (
    <div className="mx-auto w-full max-w-7xl space-y-6">
      <PageHeader
        title={t("jobs.title")}
        description={t("jobs.description")}
        actions={
          <div className="flex items-center gap-2">
            {archivedFilter === "exclude" && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => bulkArchiveMutation.mutate()}
                disabled={bulkArchiveMutation.isPending || bulkTargetCount === 0}
                aria-label={t("jobs.bulkArchiveAria")}
                title={t("jobs.bulkArchiveTitle", { count: bulkTargetCount })}
              >
                <Archive className="h-4 w-4" />
                {bulkArchiveMutation.isPending ? t("jobs.archiving") : t("jobs.bulkArchive")}
              </Button>
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={() => refetch()}
              disabled={isFetching}
              aria-label={t("jobs.refreshAria")}
            >
              <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
              {t("common.refresh")}
            </Button>
          </div>
        }
      />

      <div className="flex flex-wrap items-end gap-4">
        <div className="space-y-1.5">
          <div className="flex items-center gap-1">
            <Label htmlFor="jobs-status-filter">{t("common.status")}</Label>
            <InfoHint content={INFO_HINTS.jobs.status} />
          </div>
          <Select
            value={statusFilter}
            onValueChange={(v) => setFilter("status", v)}
          >
            <SelectTrigger id="jobs-status-filter" className="w-40" aria-label={t("jobs.filterByStatus")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>{t("jobs.allStatuses")}</SelectItem>
              {JOB_STATUS_OPTIONS.map((opt) => (
                <SelectItem key={opt.value} value={opt.value}>
                  {tl(opt.label)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <div className="flex items-center gap-1">
            <Label htmlFor="jobs-kind-filter">{t("jobs.kind")}</Label>
            <InfoHint content={INFO_HINTS.jobs.kind} />
          </div>
          <Select value={kindFilter} onValueChange={(v) => setFilter("kind", v)}>
            <SelectTrigger id="jobs-kind-filter" className="w-48" aria-label={t("jobs.filterByKind")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>{t("jobs.allKinds")}</SelectItem>
              {JOB_KINDS.map((k) => (
                <SelectItem key={k.value} value={k.value}>
                  {tl(k.label)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="jobs-archived-filter">{t("jobs.archived")}</Label>
          <Select value={archivedFilter} onValueChange={(v) => setFilter("archived", v)}>
            <SelectTrigger id="jobs-archived-filter" className="w-36" aria-label={t("jobs.filterByArchived")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {JOB_ARCHIVED_OPTIONS.map((opt) => (
                <SelectItem key={opt.value} value={opt.value}>
                  {tl(opt.label)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="text-sm text-muted-foreground">
          {t("jobs.totalCount", { count: total })}{activeCount > 0 ? t("jobs.activeCount", { count: activeCount }) : ""}
        </div>
      </div>

      {actionError && (
        <Alert variant="destructive">
          <AlertDescription>{t("jobs.actionFailed")}{actionError}</AlertDescription>
        </Alert>
      )}
      {bulkNotice && (
        <Alert>
          <AlertDescription>{bulkNotice}</AlertDescription>
        </Alert>
      )}

      {isLoading ? (
        <LoadingState rows={6} />
      ) : isError ? (
        <ErrorState
          message={error instanceof Error ? error.message : undefined}
          onRetry={() => refetch()}
        />
      ) : jobs.length === 0 && total === 0 ? (
        <EmptyState
          icon={<ListTodo className="h-8 w-8" />}
          title={t("jobs.emptyTitle")}
          description={t("jobs.emptyDesc")}
        />
      ) : (
        <>
          {/* min-w:9 列表格在窄视口下不挤压,经 Table 内建 overflow-auto 出横向滚动条(issue #373 条目5)。 */}
          <div className="overflow-hidden rounded-lg border border-border bg-card">
            <Table className="min-w-[64rem]">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10" />
                  <TableHead>{t("jobs.jobId")}</TableHead>
                  <TableHead>{t("jobs.kind")}</TableHead>
                  <TableHead>{t("common.status")}</TableHead>
                  <TableHead>{t("jobs.progress")}</TableHead>
                  <TableHead>{t("jobs.attempts")}</TableHead>
                  <TableHead>{t("jobs.requestedBy")}</TableHead>
                  <TableHead>{t("jobs.createdAt")}</TableHead>
                  <TableHead className="text-right">{t("common.actions")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {jobs.map((job) => (
                  <JobRow
                    key={job.job_id}
                    job={job}
                    expanded={expandedId === job.job_id}
                    onToggleExpand={() =>
                      setExpandedId((prev) => (prev === job.job_id ? null : job.job_id))
                    }
                    onCancel={() => cancelMutation.mutate(job.job_id)}
                    cancelling={cancelMutation.isPending && cancelMutation.variables === job.job_id}
                    onArchive={() => archiveMutation.mutate(job.job_id)}
                    archiving={archiveMutation.isPending && archiveMutation.variables === job.job_id}
                    onUnarchive={() => unarchiveMutation.mutate(job.job_id)}
                    unarchiving={unarchiveMutation.isPending && unarchiveMutation.variables === job.job_id}
                  />
                ))}
              </TableBody>
            </Table>
          </div>
          {/* 分页控件(issue #373 条目1):页码入 URL,轮询刷新按 queryKey 自然保页。 */}
          <nav
            className="flex items-center justify-between"
            aria-label={t("jobs.paginationAria")}
          >
            <span className="text-xs text-muted-foreground">
              {t("jobs.pageInfo", { page, totalPages })}
            </span>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={page <= 1}
                onClick={() => setPage(page - 1)}
                aria-label={t("common.prevPage")}
              >
                <ChevronLeft className="h-4 w-4" />
                {t("common.prevPage")}
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={page >= totalPages}
                onClick={() => setPage(page + 1)}
                aria-label={t("common.nextPage")}
              >
                {t("common.nextPage")}
                <ChevronRight className="h-4 w-4" />
              </Button>
            </div>
          </nav>
        </>
      )}
    </div>
  );
}

interface JobRowProps {
  job: JobOut;
  expanded: boolean;
  onToggleExpand: () => void;
  onCancel: () => void;
  cancelling: boolean;
  onArchive: () => void;
  archiving: boolean;
  onUnarchive: () => void;
  unarchiving: boolean;
}

function JobRow({
  job,
  expanded,
  onToggleExpand,
  onCancel,
  cancelling,
  onArchive,
  archiving,
  onUnarchive,
  unarchiving,
}: JobRowProps) {
  const { t, tl, lang } = useT();
  const active = isJobRunning(job);
  const archived = job.archived_at != null;
  const total = job.progress_total;
  const percent =
    total > 0 ? Math.min(100, Math.round((job.progress_done / total) * 100)) : null;
  const phaseText = usePhaseText(job.phase);
  return (
    <>
      <TableRow data-state={expanded ? "selected" : undefined} className={archived ? "opacity-60" : undefined}>
        <TableCell>
          <Button
            variant="ghost"
            size="icon"
            className="h-9 w-9"
            onClick={onToggleExpand}
            aria-expanded={expanded}
            aria-label={expanded ? t("jobs.collapseDetail") : t("jobs.expandDetail")}
          >
            {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
          </Button>
        </TableCell>
        <TableCell className="font-mono text-xs">{job.job_id}</TableCell>
        <TableCell>{jobKindLabel(job.kind, lang)}</TableCell>
        <TableCell>
          <div className="flex items-center gap-1.5">
            <StatusBadge status={job.status}>{tl(JOB_STATUS_LABELS[job.status])}</StatusBadge>
            {archived && (
              <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                {t("jobs.archivedBadge")}
              </span>
            )}
          </div>
        </TableCell>
        <TableCell className="min-w-40">
          {job.status === "running" && total > 0 ? (
            <div className="flex items-center gap-2">
              <Progress value={percent ?? 0} className="w-24" aria-label={t("jobs.progressAria")} />
              <span
                className="text-xs text-muted-foreground whitespace-nowrap"
                title={job.phase ?? undefined}
              >
                {job.progress_done}/{total}
                {phaseText ? ` ${phaseText}` : ""}
              </span>
            </div>
          ) : (
            <span
              className="text-xs text-muted-foreground"
              title={job.phase ?? undefined}
            >
              {phaseText ?? "—"}
            </span>
          )}
        </TableCell>
        <TableCell className="text-xs text-muted-foreground">
          {job.attempt}/{job.max_attempts}
        </TableCell>
        <TableCell className="text-xs text-muted-foreground">{job.requested_by}</TableCell>
        <TableCell className="text-xs text-muted-foreground" title={formatDateTime(job.created_at)}>
          {timeAgo(job.created_at, lang)}
        </TableCell>
        <TableCell className="text-right">
          {active ? (
            <Button
              variant="outline"
              size="sm"
              onClick={onCancel}
              disabled={cancelling}
              aria-label={t("jobs.cancelJobAria", { id: job.job_id })}
            >
              {cancelling ? t("jobs.cancelling") : t("common.cancel")}
            </Button>
          ) : archived ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={onUnarchive}
              disabled={unarchiving}
              aria-label={t("jobs.unarchiveJobAria", { id: job.job_id })}
            >
              <ArchiveRestore className="h-4 w-4" />
              {unarchiving ? t("jobs.restoring") : t("jobs.restore")}
            </Button>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              onClick={onArchive}
              disabled={archiving}
              aria-label={t("jobs.archiveJobAria", { id: job.job_id })}
            >
              <Archive className="h-4 w-4" />
              {archiving ? t("jobs.archiving") : t("jobs.archive")}
            </Button>
          )}
        </TableCell>
      </TableRow>
      {expanded && (
        <TableRow>
          <TableCell colSpan={9} className="bg-muted/30 p-4">
            <JobDetail job={job} />
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

function JobDetail({ job }: { job: JobOut }) {
  const { t } = useT();
  // kind=research_run 时单查最新任务(issue #306/#308):run_status 只在
  // GET /api/jobs/{id} 透传(列表不 join),展开详情即实时可见 run/job 两表
  // 一致性与决策级进度;运行中随轮询刷新。
  const isResearchRun = job.kind === "research_run";
  const { data: live } = useQuery({
    queryKey: ["job", job.job_id],
    queryFn: () => api.getJob(job.job_id),
    enabled: isResearchRun,
    refetchInterval: isJobRunning(job) ? POLL_MS : false,
  });
  const runStatus = live?.run_status ?? job.run_status ?? null;
  const phaseInfo = parseJobPhase(live?.phase ?? job.phase);
  const mismatched =
    runStatus != null && RUN_STUCK_STATUSES.has(runStatus) && isJobRunning(job);
  const rows: [string, string][] = [
    [t("jobs.queue"), job.queue],
    [t("jobs.priority"), String(job.priority)],
    ["Worker", job.worker_id ?? "—"],
    [t("jobs.idempotencyKey"), job.idempotency_key],
    ...(isResearchRun
      ? ([[t("jobs.runStatus"), runStatus ?? "—"]] as [string, string][])
      : []),
    ...(phaseInfo?.decision
      ? ([
          [
            t("jobs.decisionProgress"),
            t("jobs.phaseDecision", {
              index: phaseInfo.decision.index,
              date: phaseInfo.decision.date,
              stage: phaseInfo.stage,
            }),
          ],
        ] as [string, string][])
      : []),
    [t("jobs.resultRef"), job.result_ref ?? "—"],
    [t("jobs.errorCode"), job.error_code ?? "—"],
    [t("jobs.heartbeatAt"), formatDateTime(job.heartbeat_at)],
    [t("jobs.leaseUntil"), formatDateTime(job.lease_until)],
    [t("jobs.startedAt"), formatDateTime(job.started_at)],
    [t("jobs.finishedAt"), formatDateTime(job.finished_at)],
    [t("jobs.archivedAt"), formatDateTime(job.archived_at)],
    [t("jobs.updatedAt"), formatDateTime(job.updated_at)],
  ];
  return (
    <div className="space-y-3">
      {mismatched && (
        <Alert variant="destructive">
          <AlertDescription>
            {t("jobs.runStatusMismatch", {
              runStatus,
              jobStatus: job.status,
            })}
          </AlertDescription>
        </Alert>
      )}
      <div className="grid gap-6 lg:grid-cols-2">
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          {rows.map(([label, value]) => (
            <div key={label} className="min-w-0">
              <dt className="text-xs text-muted-foreground">{label}</dt>
              <dd className="truncate font-mono text-xs" title={value}>
                {value}
              </dd>
            </div>
          ))}
        </dl>
        <div className="min-w-0 space-y-3">
          {/* 耗时 / IO 聚合(issue #383 落库、#373 透出):一行读法 + 占比条。 */}
          <div>
            <p className="mb-1 text-xs text-muted-foreground">{t("jobs.timing")}</p>
            <TimingLine timing={live?.timing ?? job.timing} />
          </div>
          <div>
            <p className="mb-1 text-xs text-muted-foreground">{t("jobs.payload")}</p>
            <pre className="max-h-56 overflow-auto scrollbar-thin rounded-md border border-border bg-background p-3 font-mono text-xs">
              {JSON.stringify(job.payload, null, 2)}
            </pre>
          </div>
        </div>
      </div>
      {/* 错误摘要(issue #373 条目2):从单行 truncate 通用行拆出 —— 多行完整
          展示(含 #263 定位头),文本可选中,附一键复制(对齐 ResearchRuns
          侧 whitespace-pre-wrap 先例)。服务端已按 1000 字符保头保尾截断。 */}
      {job.error_summary && (
        <div>
          <div className="mb-1 flex items-center gap-1">
            <p className="text-xs text-muted-foreground">{t("jobs.errorSummary")}</p>
            <CopyButton
              text={job.error_summary}
              labels={{ copy: t("jobs.copyErrorSummary"), copied: t("common.copied") }}
            />
          </div>
          <pre className="max-h-48 overflow-auto scrollbar-thin rounded-md border border-border bg-background p-3 font-mono text-xs whitespace-pre-wrap break-words">
            {job.error_summary}
          </pre>
        </div>
      )}
      {/* 诊断重放(火焰图)入口(issue #373 条目4):仅终态 job;panel 内部
          做能力表门控 / 副作用确认 / 会话轮询。 */}
      <FlamegraphPanel job={live ?? job} />
    </div>
  );
}
