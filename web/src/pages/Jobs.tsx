import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { ChevronDown, ChevronUp, ListTodo, RefreshCw } from "lucide-react";
import { api, isJobRunning, type JobOut, type JobStatus } from "@/lib/api";
import {
  JOB_KINDS,
  JOB_STATUS_LABELS,
  JOB_STATUS_OPTIONS,
  jobKindLabel,
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

const ALL = "all";
const POLL_MS = 5000;

/** 任务中心:研究/数据/回测域统一后台任务的集中管理页(issue #161)。 */
export default function Jobs() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const statusFilter = searchParams.get("status") ?? ALL;
  const kindFilter = searchParams.get("kind") ?? ALL;
  const [expandedId, setExpandedId] = React.useState<string | null>(null);
  const [cancelError, setCancelError] = React.useState<string | null>(null);

  const setFilter = (key: "status" | "kind", value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value === ALL) next.delete(key);
    else next.set(key, value);
    setSearchParams(next, { replace: true });
  };

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["jobs", statusFilter, kindFilter],
    queryFn: () =>
      api.listJobs({
        status: statusFilter === ALL ? undefined : [statusFilter as JobStatus],
        kind: kindFilter === ALL ? undefined : [kindFilter],
        limit: 200,
      }),
    // 轮询只在还有非终态任务时进行;全部终态后自动停止,避免静态页面持续请求。
    refetchInterval: (query) => {
      const jobs = query.state.data;
      return jobs && jobs.some(isJobRunning) ? POLL_MS : false;
    },
  });

  const cancelMutation = useMutation({
    mutationFn: (jobId: string) => api.cancelJob(jobId),
    onSuccess: () => {
      setCancelError(null);
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (err: Error) => setCancelError(err.message),
  });

  const jobs = data ?? [];
  const activeCount = jobs.filter(isJobRunning).length;

  return (
    <div className="mx-auto w-full max-w-7xl space-y-6">
      <PageHeader
        title="任务中心"
        description="研究 / 数据 / 回测域后台任务的统一查看与取消;任务由独立 Worker 进程消费,刷新页面后状态自动恢复。"
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={() => refetch()}
            disabled={isFetching}
            aria-label="刷新任务列表"
          >
            <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
            刷新
          </Button>
        }
      />

      <div className="flex flex-wrap items-end gap-4">
        <div className="space-y-1.5">
          <Label htmlFor="jobs-status-filter">状态</Label>
          <Select
            value={statusFilter}
            onValueChange={(v) => setFilter("status", v)}
          >
            <SelectTrigger id="jobs-status-filter" className="w-40" aria-label="按状态过滤">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>全部状态</SelectItem>
              {JOB_STATUS_OPTIONS.map((opt) => (
                <SelectItem key={opt.value} value={opt.value}>
                  {opt.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="jobs-kind-filter">任务类型</Label>
          <Select value={kindFilter} onValueChange={(v) => setFilter("kind", v)}>
            <SelectTrigger id="jobs-kind-filter" className="w-48" aria-label="按任务类型过滤">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>全部类型</SelectItem>
              {JOB_KINDS.map((k) => (
                <SelectItem key={k.value} value={k.value}>
                  {k.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="text-sm text-muted-foreground">
          共 {jobs.length} 条{activeCount > 0 ? `,${activeCount} 条进行中` : ""}
        </div>
      </div>

      {cancelError && (
        <Alert variant="destructive">
          <AlertDescription>取消失败:{cancelError}</AlertDescription>
        </Alert>
      )}

      {isLoading ? (
        <LoadingState rows={6} />
      ) : isError ? (
        <ErrorState
          message={error instanceof Error ? error.message : undefined}
          onRetry={() => refetch()}
        />
      ) : jobs.length === 0 ? (
        <EmptyState
          icon={<ListTodo className="h-8 w-8" />}
          title="暂无任务"
          description="在数据、回测或研究页面提交的任务会出现在这里。"
        />
      ) : (
        <div className="overflow-hidden rounded-lg border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10" />
                <TableHead>任务 ID</TableHead>
                <TableHead>类型</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>进度</TableHead>
                <TableHead>尝试</TableHead>
                <TableHead>提交人</TableHead>
                <TableHead>创建时间</TableHead>
                <TableHead className="text-right">操作</TableHead>
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
                />
              ))}
            </TableBody>
          </Table>
        </div>
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
}

function JobRow({ job, expanded, onToggleExpand, onCancel, cancelling }: JobRowProps) {
  const active = isJobRunning(job);
  const total = job.progress_total;
  const percent =
    total > 0 ? Math.min(100, Math.round((job.progress_done / total) * 100)) : null;
  return (
    <>
      <TableRow data-state={expanded ? "selected" : undefined}>
        <TableCell>
          <Button
            variant="ghost"
            size="icon"
            className="h-9 w-9"
            onClick={onToggleExpand}
            aria-expanded={expanded}
            aria-label={expanded ? "收起任务详情" : "展开任务详情"}
          >
            {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
          </Button>
        </TableCell>
        <TableCell className="font-mono text-xs">{job.job_id}</TableCell>
        <TableCell>{jobKindLabel(job.kind)}</TableCell>
        <TableCell>
          <StatusBadge status={job.status}>{JOB_STATUS_LABELS[job.status]}</StatusBadge>
        </TableCell>
        <TableCell className="min-w-40">
          {job.status === "running" && total > 0 ? (
            <div className="flex items-center gap-2">
              <Progress value={percent ?? 0} className="w-24" aria-label="执行进度" />
              <span className="text-xs text-muted-foreground whitespace-nowrap">
                {job.progress_done}/{total}
                {job.phase ? ` ${job.phase}` : ""}
              </span>
            </div>
          ) : (
            <span className="text-xs text-muted-foreground">{job.phase ?? "—"}</span>
          )}
        </TableCell>
        <TableCell className="text-xs text-muted-foreground">
          {job.attempt}/{job.max_attempts}
        </TableCell>
        <TableCell className="text-xs text-muted-foreground">{job.requested_by}</TableCell>
        <TableCell className="text-xs text-muted-foreground" title={formatDateTime(job.created_at)}>
          {timeAgo(job.created_at)}
        </TableCell>
        <TableCell className="text-right">
          {active ? (
            <Button
              variant="outline"
              size="sm"
              onClick={onCancel}
              disabled={cancelling}
              aria-label={`取消任务 ${job.job_id}`}
            >
              {cancelling ? "取消中…" : "取消"}
            </Button>
          ) : (
            <span className="text-xs text-muted-foreground">—</span>
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
  const rows: [string, string][] = [
    ["队列", job.queue],
    ["优先级", String(job.priority)],
    ["Worker", job.worker_id ?? "—"],
    ["幂等键", job.idempotency_key],
    ["结果引用", job.result_ref ?? "—"],
    ["错误码", job.error_code ?? "—"],
    ["错误摘要", job.error_summary ?? "—"],
    ["心跳时间", formatDateTime(job.heartbeat_at)],
    ["租约到期", formatDateTime(job.lease_until)],
    ["开始时间", formatDateTime(job.started_at)],
    ["完成时间", formatDateTime(job.finished_at)],
    ["更新时间", formatDateTime(job.updated_at)],
  ];
  return (
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
      <div className="min-w-0">
        <p className="mb-1 text-xs text-muted-foreground">入参 payload</p>
        <pre className="max-h-56 overflow-auto scrollbar-thin rounded-md border border-border bg-background p-3 font-mono text-xs">
          {JSON.stringify(job.payload, null, 2)}
        </pre>
      </div>
    </div>
  );
}
