import ResearchRunSetup from "@/components/research/ResearchRunSetup";
import StrategyExplanation from "@/components/research/StrategyExplanation";
import { runSchedule } from "@/lib/research-semantics";
import { ResearchHint } from "@/components/research/ResearchHint";
import { RESEARCH_HINTS } from "@/lib/research-hints";
import { RunReportView } from "@/components/research/report/RunReportView";
import { Fragment, type ReactNode, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import {
  Activity,
  ArrowLeft,
  GitBranch,
  Plus,
  Play,
  RefreshCw,
  XCircle,
} from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { MasterList, MasterListItem } from "@/components/ui/master-list";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { StatusBadge } from "@/components/ui/status-badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import {
  researchRunApi,
  runningRunLabel,
  type ResearchRunQueueIn,
  type ResearchRunSummary,
} from "@/lib/research";
import { api, isJobRunning, isJobTerminal } from "@/lib/api";
import { cn, formatCurrency, formatDateTime, timeAgo } from "@/lib/utils";
import { useT, type LocalizedText } from "@/i18n";

const STATUS_OPTIONS: { value: string; label: LocalizedText }[] = [
  { value: "all", label: { zh: "全部", en: "All" } },
  { value: "queued", label: { zh: "排队中", en: "Queued" } },
  { value: "running", label: { zh: "运行中", en: "Running" } },
  { value: "completed", label: { zh: "已完成", en: "Completed" } },
  { value: "failed", label: { zh: "失败", en: "Failed" } },
  { value: "interrupted", label: { zh: "已中断", en: "Interrupted" } },
  { value: "rejected", label: { zh: "已拒绝", en: "Rejected" } },
  { value: "cancelled", label: { zh: "已取消", en: "Cancelled" } },
];

const TERMINAL_STATUSES = ["completed", "failed", "interrupted", "rejected", "cancelled"];

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

function manifestRecord(
  manifest: Record<string, unknown> | undefined,
  key: string,
): Record<string, unknown> | undefined {
  const value = manifest?.[key];
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function strategyVersion(run: ResearchRunSummary): number | null {
  if (typeof run.strategy_version === "number") return run.strategy_version;
  const frozenVersion = run.manifest?.strategy_version;
  if (typeof frozenVersion === "number") return frozenVersion;
  const value = run.manifest?.strategy_spec_version;
  return typeof value === "number" ? value : null;
}

function strategyVersionLabel(run: ResearchRunSummary): LocalizedText {
  const version = strategyVersion(run);
  return version === null ? { zh: "版本未记录", en: "Version not recorded" } : { zh: `v${version}`, en: `v${version}` };
}

function initialCapital(run: ResearchRunSummary): number | null {
  if (typeof run.initial_capital === "number") return run.initial_capital;
  const value = run.manifest?.initial_capital;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

// issue #312:发起方类型(actor_type)随 manifest 返回;human/agent 之外的历史
// 值原样透传展示,llm 不会出现在合法记录中(契约层 fail-closed)。
function actorType(run: ResearchRunSummary): string {
  const value = run.manifest?.actor_type;
  return typeof value === "string" && value !== "" ? value : "human";
}

function JsonBlock({
  label,
  value,
}: {
  label: string;
  value: Record<string, unknown> | undefined;
}) {
  return (
    <div>
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <pre className="mt-1 overflow-x-auto rounded-md border border-border bg-muted/30 p-3 font-mono text-xs leading-relaxed">
        {value ? JSON.stringify(value, null, 2) : "—"}
      </pre>
    </div>
  );
}

function InfoItem({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <div className="text-sm text-foreground">{children}</div>
    </div>
  );
}

interface FrozenRef {
  id: string;
  version: string;
  checksum: string;
}

/** manifest.dataset_releases / manifest.factor_snapshots 是 FrozenArtifactRef 数组。 */
function frozenRefs(value: unknown): FrozenRef[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      const rec = item && typeof item === "object" ? (item as Record<string, unknown>) : {};
      return {
        id: typeof rec.artifact_id === "string" ? rec.artifact_id : "",
        version: typeof rec.version === "string" ? rec.version : "",
        checksum: typeof rec.checksum === "string" ? rec.checksum : "",
      };
    })
    .filter((item) => item.id !== "");
}

/**
 * 冻结输入结构化视图:把 manifest 里埋着的数据发布 / 因子快照 / 执行模式
 * 提升为可读卡片,dataset_release_ids 可跳数据页详情、快照可跳因子实验室。
 */
function FrozenInputsCard({ manifest, result }: { manifest: Record<string, unknown>; result?: Record<string, unknown> | null }) {
  const { tl } = useT();
  const releases = frozenRefs(manifest.dataset_releases);
  const snapshots = frozenRefs(manifest.factor_snapshots);
  const benchmarkConfig = manifestRecord(manifest, "benchmark_config");
  const benchmarkSymbol =
    typeof benchmarkConfig?.symbol === "string" && benchmarkConfig.symbol !== ""
      ? benchmarkConfig.symbol
      : null;
  const schedule = runSchedule({ manifest, result });
  const executionMode = schedule.mode;
  const rebalanceFrequency = schedule.frequency;
  const specChecksum =
    typeof manifest.strategy_spec_checksum === "string" ? manifest.strategy_spec_checksum : null;
  const codeVersion =
    typeof manifest.code_version === "string" && manifest.code_version !== ""
      ? manifest.code_version
      : null;


  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-1 text-sm">
          {tl({ zh: "冻结输入", en: "Frozen inputs" })}
          <ResearchHint hint={RESEARCH_HINTS.runs.freeze} />
          <span className="ml-1 text-xs font-normal text-muted-foreground">
            {tl({
              zh: "运行执行时只读取以下内容寻址引用,与后续数据变化隔离",
              en: "The run only reads these content-addressed references, isolated from later data changes",
            })}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-xs">
        <div className="flex flex-wrap gap-x-6 gap-y-1">
          <span>
            {tl({ zh: "执行模式:", en: "Execution mode:" })}
            <Badge variant="info" className="ml-1.5 font-mono">
              {executionMode === "unknown" ? tl({ zh: "执行模式未知", en: "Unknown execution mode" }) : executionMode}
            </Badge>
            {rebalanceFrequency && (
              <span className="ml-2 text-muted-foreground">
                {tl({ zh: "调仓频率:", en: "Rebalance:" })}
                <span className="ml-1 font-mono text-foreground">{rebalanceFrequency}</span>
              </span>
            )}
          </span>
          {schedule.count !== null && <span>决策日历：{schedule.first} 至 {schedule.last}，{schedule.count} 次</span>}
          {schedule.conflict && <span className="text-warning">决策日历与旧频率同时存在，请核对冻结输入</span>}
          {benchmarkSymbol && (
            <span>
              {tl({ zh: "基准:", en: "Benchmark:" })}
              <span className="ml-1 font-mono text-foreground">{benchmarkSymbol}</span>
            </span>
          )}
          {codeVersion && (
            <span>
              {tl({ zh: "代码版本:", en: "Code version:" })}
              <span className="ml-1 font-mono text-foreground">{codeVersion}</span>
            </span>
          )}
        </div>

        <div>
          <p className="font-medium text-foreground">
            {tl({ zh: "数据发布", en: "Data releases" })}
            <span className="ml-1 text-muted-foreground">({releases.length})</span>
          </p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {releases.map((ref) => (
              <Tooltip key={ref.id}>
                <TooltipTrigger asChild>
                  <Link
                    to={`/data?release=${encodeURIComponent(ref.id)}`}
                    className="inline-flex items-center rounded-md border border-border bg-card px-2 py-1 font-mono text-[11px] text-foreground transition-colors hover:bg-accent"
                  >
                    {ref.id}
                    {ref.version && <span className="ml-1 text-muted-foreground">@{ref.version}</span>}
                  </Link>
                </TooltipTrigger>
                <TooltipContent className="max-w-sm break-all font-mono text-[10px]">
                  {tl({ zh: "点击到数据页查看发布详情", en: "Open release details in the Data page" })}
                  {ref.checksum && (
                    <>
                      <br />
                      checksum: {ref.checksum}
                    </>
                  )}
                </TooltipContent>
              </Tooltip>
            ))}
          </div>
        </div>

        {snapshots.length > 0 && (
          <div>
            <p className="font-medium text-foreground">
              {tl({ zh: "因子快照", en: "Factor snapshots" })}
              <span className="ml-1 text-muted-foreground">({snapshots.length})</span>
            </p>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {snapshots.map((ref) => (
                <Tooltip key={ref.id}>
                  <TooltipTrigger asChild>
                    <Link
                      to={`/research/factors?snapshot=${encodeURIComponent(ref.id)}`}
                      className="inline-flex items-center rounded-md border border-border bg-card px-2 py-1 font-mono text-[11px] text-foreground transition-colors hover:bg-accent"
                    >
                      {ref.id}
                    </Link>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-sm break-all font-mono text-[10px]">
                    {tl({ zh: "点击到因子实验室查看快照详情", en: "Open snapshot details in Factor Lab" })}
                    {ref.checksum && (
                      <>
                        <br />
                        checksum: {ref.checksum}
                      </>
                    )}
                  </TooltipContent>
                </Tooltip>
              ))}
            </div>
          </div>
        )}

        {specChecksum && (
          <p className="break-all text-muted-foreground">
            {tl({ zh: "策略规格 checksum:", en: "Strategy spec checksum:" })}
            <span className="ml-1 font-mono text-foreground">{specChecksum}</span>
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function CreateResearchRunDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (run: ResearchRunSummary) => void;
}) {
  const { tl } = useT();
  const queryClient = useQueryClient();
  const queueMutation = useMutation({
    mutationFn: (body: ResearchRunQueueIn) => researchRunApi.queue(body),
    onSuccess: (run) => {
      void queryClient.invalidateQueries({ queryKey: ["research-runs"] });
      onOpenChange(false);
      onCreated(run);
    },
  });

  const dismiss = (next: boolean) => {
    if (queueMutation.isPending && !next) return;
    queueMutation.reset();
    onOpenChange(next);
  };

  return (
    <Dialog open={open} onOpenChange={dismiss}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{tl({ zh: "新建研究运行", en: "New research run" })}</DialogTitle>
          <DialogDescription>
            {tl({ zh: "先对照生效输入，再启动离线研究任务。", en: "Review effective inputs before queuing the offline research job." })}
          </DialogDescription>
        </DialogHeader>
        {open && (
          <ResearchRunSetup
            onStart={(payload) => queueMutation.mutate(payload)}
            pending={queueMutation.isPending}
            error={queueMutation.error}
            onRepreview={() => queueMutation.reset()}
            onCancel={() => dismiss(false)}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

export default function ResearchRuns() {
  const { tl, lang } = useT();
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<string>("all");
  // 选中运行进 URL(?run=RR-xxx):刷新/分享/返回不再丢详情。
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedId = searchParams.get("run");
  const setSelectedId = (runId: string | null) => {
    setSearchParams(runId ? { run: runId } : {}, { replace: true });
  };
  const [cancelOpen, setCancelOpen] = useState(false);
  const [replayOpen, setReplayOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [requestedBy, setRequestedBy] = useState("");
  const [lineageOpen, setLineageOpen] = useState(false);
  // 决策产物表中被展开查看 payload 的 artifact。
  const [expandedArtifacts, setExpandedArtifacts] = useState<Set<string>>(new Set());

  // 状态过滤走服务端(status 多值查询参数);"all" 不传,由后端返回全部。
  const listQuery = useQuery({
    queryKey: ["research-runs", "list", { status: statusFilter, limit: 50 }],
    queryFn: () =>
      researchRunApi.list({
        status: statusFilter === "all" ? undefined : [statusFilter],
        limit: 50,
      }),
    // 存在非终态运行(queued/running)时自动轮询,worker 推进后列表自动刷新。
    refetchInterval: (query) => {
      const runs = query.state.data ?? [];
      return runs.some((r) => !TERMINAL_STATUSES.includes(r.status))
        ? 10_000
        : false;
    },
  });

  const detailQuery = useQuery({
    queryKey: ["research-runs", "detail", selectedId],
    queryFn: () => researchRunApi.get(selectedId as string),
    enabled: selectedId !== null,
  });

  const artifactsQuery = useQuery({
    queryKey: ["research-runs", "artifacts", selectedId],
    queryFn: () => researchRunApi.artifacts(selectedId as string),
    enabled: selectedId !== null,
  });

  const detail = detailQuery.data;

  // 绩效报告页签:仅 completed 运行可用;页签状态进 URL(?tab=overview|report),
  // 与 ?run= 参数并存(setSearchParams replace,不产生历史记录)。
  const reportAvailable = detail?.status === "completed";
  const requestedTab = searchParams.get("tab");
  const activeTab: "overview" | "report" | "explanation" =
    requestedTab === "explanation" ? "explanation" : requestedTab === "report" && reportAvailable ? "report" : "overview";
  const handleTabChange = (value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value === "report" || value === "explanation") {
      next.set("tab", value);
    } else {
      next.delete("tab");
    }
    setSearchParams(next, { replace: true });
  };

  // 统一任务队列 job 状态(#157):queued/running 运行轮询 /api/jobs/{job_id},
  // 展示 phase / progress / worker / 失败原因 / result_ref;job 终态后刷新运行。
  const detailJobId = detail?.job_id ?? null;
  const jobQuery = useQuery({
    queryKey: ["research-runs", "job", detailJobId],
    queryFn: () => api.getJob(detailJobId as string),
    enabled:
      detailJobId != null &&
      detail != null &&
      !TERMINAL_STATUSES.includes(detail.status),
    refetchInterval: (query) =>
      query.state.data && isJobRunning(query.state.data) ? 5_000 : false,
  });

  const jobData = jobQuery.data;
  useEffect(() => {
    if (jobData && isJobTerminal(jobData)) {
      void queryClient.invalidateQueries({ queryKey: ["research-runs"] });
    }
  }, [jobData, queryClient]);

  const filteredRuns = useMemo(() => listQuery.data ?? [], [listQuery.data]);

  const leafTraceId = useMemo(() => {
    if (!artifactsQuery.data || artifactsQuery.data.length === 0) return null;
    const withTrace = artifactsQuery.data.filter((a) => a.trace_id);
    if (withTrace.length === 0) return null;
    return withTrace.reduce(
      (acc, a) => (a.sequence > acc.sequence ? a : acc),
      withTrace[0],
    ).trace_id;
  }, [artifactsQuery.data]);

  const lineageQuery = useQuery({
    queryKey: ["research-runs", "lineage", selectedId, leafTraceId],
    queryFn: () =>
      researchRunApi.lineage(selectedId as string, leafTraceId as string),
    enabled: lineageOpen && selectedId !== null && leafTraceId !== null,
  });

  const cancelMutation = useMutation({
    mutationFn: (runId: string) => researchRunApi.cancel(runId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["research-runs"] });
      setCancelOpen(false);
    },
  });

  const replayMutation = useMutation({
    mutationFn: ({
      runId,
      body,
    }: {
      runId: string;
      body: { idempotency_key: string; requested_by: string };
    }) => researchRunApi.replay(runId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["research-runs"] });
      setReplayOpen(false);
      setIdempotencyKey("");
    },
  });

  const invalidateAll = () => {
    void queryClient.invalidateQueries({ queryKey: ["research-runs"] });
  };

  const openReplayDialog = () => {
    if (
      typeof crypto !== "undefined" &&
      typeof crypto.randomUUID === "function"
    ) {
      setIdempotencyKey(`replay-${crypto.randomUUID()}`);
    } else {
      setIdempotencyKey(`replay-${Date.now()}`);
    }
    setReplayOpen(true);
  };

  return (
    <div>
      <PageHeader
        title={tl({ zh: "研究运行", en: "Research runs" })}
        description={tl({ zh: "冻结输入、血缘追踪与运行重放", en: "Frozen inputs, lineage tracing, and run replay" })}
        actions={
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => setCreateOpen(true)}>
              <Plus className="h-4 w-4" />
              {tl({ zh: "排队研究运行", en: "Queue research run" })}
            </Button>
            <Button asChild variant="outline" size="sm">
              <Link to="/research">
                <ArrowLeft className="h-4 w-4" />
                {tl({ zh: "返回研究", en: "Back to research" })}
              </Link>
            </Button>
          </div>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <Label className="text-xs text-muted-foreground">{tl({ zh: "状态筛选", en: "Status filter" })}</Label>
          <ResearchHint hint={RESEARCH_HINTS.runs.statusFilter} />
          <Select value={statusFilter} onValueChange={setStatusFilter}>
            <SelectTrigger className="h-9 w-[160px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STATUS_OPTIONS.map((opt) => (
                <SelectItem key={opt.value} value={opt.value}>
                  {tl(opt.label)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <p className="text-sm text-muted-foreground">
          {listQuery.data
            ? tl({
                zh: `筛选结果共 ${listQuery.data.length} 个运行(服务端按状态过滤)`,
                en: `${listQuery.data.length} runs shown (filtered by status on the server)`,
              })
            : tl({ zh: "加载中…", en: "Loading…" })}
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => listQuery.refetch()}
          disabled={listQuery.isFetching}
        >
          <RefreshCw
            className={cn("h-4 w-4", listQuery.isFetching && "animate-spin")}
          />
          {tl({ zh: "刷新", en: "Refresh" })}
        </Button>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <MasterList
          className="lg:col-span-1"
          title={tl({ zh: "运行列表", en: "Run list" })}
          count={listQuery.isSuccess ? filteredRuns.length : undefined}
        >
          {listQuery.isLoading ? (
            <LoadingState rows={5} />
          ) : listQuery.isError ? (
            <ErrorState
              message={errorMessage(listQuery.error, tl({ zh: "无法加载运行列表", en: "Failed to load run list" }))}
              onRetry={() => listQuery.refetch()}
            />
          ) : filteredRuns.length > 0 ? (
            filteredRuns.map((run: ResearchRunSummary) => {
              const live = runningRunLabel(run);
              return (
                <MasterListItem
                  key={run.run_id}
                  selected={selectedId === run.run_id}
                  onClick={() => setSelectedId(run.run_id)}
                >
                  <div className="flex items-center justify-between gap-2">
                    <StatusBadge status={run.status} />
                    <span className="min-w-0 truncate font-mono text-xs text-muted-foreground">
                      {run.run_id}
                    </span>
                  </div>
                  <div className="mt-2 flex items-center justify-between gap-2">
                    <span className="min-w-0 truncate font-mono text-xs text-foreground">
                      {run.strategy_id}
                    </span>
                    <Badge variant="secondary" className="font-mono">
                      {tl(strategyVersionLabel(run))}
                    </Badge>
                  </div>
                  <div className="mt-2 flex items-center justify-between gap-2 text-xs text-muted-foreground">
                    <span className="tabular-nums">
                      ¥{formatCurrency(initialCapital(run), 0)}
                    </span>
                    <span>{timeAgo(run.created_at, lang)}</span>
                  </div>
                  {live && (
                    <div className="mt-2 flex items-center gap-1.5 text-xs text-info">
                      <Activity className="h-3.5 w-3.5 shrink-0" />
                      <span className="tabular-nums">{tl(live)}</span>
                    </div>
                  )}
                  {run.status === "failed" && run.error_summary && (
                    <p
                      className="mt-2 truncate text-xs text-destructive"
                      title={run.error_summary}
                    >
                      {run.error_summary}
                    </p>
                  )}
                </MasterListItem>
              );
            })
          ) : (
            <EmptyState
              icon={<Activity className="h-8 w-8" />}
              title={tl({ zh: "暂无运行", en: "No runs yet" })}
              description={tl({ zh: "当前筛选条件下没有研究运行，可切换状态筛选或刷新列表。", en: "No research runs under the current filter. Switch the status filter or refresh the list." })}
            />
          )}
        </MasterList>

        <div className="lg:col-span-2">
          {selectedId ? (
            <Card>
              <CardHeader className="pb-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <CardTitle className="flex items-center gap-2 text-base">
                      <span className="font-mono text-sm">{selectedId}</span>
                    </CardTitle>
                    {detail && (
                      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                        <StatusBadge status={detail.status} />
                        <span className="font-mono">{detail.strategy_kind}</span>
                        <span>·</span>
                        <span className="font-mono">
                          {detail.strategy_id}@{tl(strategyVersionLabel(detail))}
                        </span>
                      </div>
                    )}
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => setSelectedId(null)}
                      aria-label={tl({ zh: "取消选择", en: "Clear selection" })}
                  >
                    <ArrowLeft className="h-4 w-4" />
                  </Button>
                </div>
              </CardHeader>
              <CardContent>
                {detailQuery.isLoading ? (
                  <LoadingState rows={6} />
                ) : detailQuery.isError ? (
                  <ErrorState
                    message={errorMessage(
                      detailQuery.error,
                      tl({ zh: "无法加载运行详情", en: "Failed to load run details" }),
                    )}
                    onRetry={() => detailQuery.refetch()}
                  />
                ) : detail ? (
                  <Tabs value={activeTab} onValueChange={handleTabChange}>
                    <div className="mb-3 flex flex-wrap items-center gap-3">
                      <TabsList>
                        <TabsTrigger value="explanation">策略说明书</TabsTrigger>
                        <TabsTrigger value="overview">
                          {tl({ zh: "概览", en: "Overview" })}
                        </TabsTrigger>
                        <TabsTrigger value="report" disabled={!reportAvailable}>
                          {tl({ zh: "绩效报告", en: "Performance report" })}
                        </TabsTrigger>
                      </TabsList>
                      {!reportAvailable && (
                        <p className="text-xs text-muted-foreground">
                          {tl({
                            zh: "绩效报告在运行完成后生成；当前运行尚未完成，完成后此页签可查看收益、权益曲线与回撤。",
                            en: "The performance report is generated once the run completes; this tab becomes available afterwards.",
                          })}
                        </p>
                      )}
                    </div>
                    <TabsContent value="explanation"><StrategyExplanation key={detail.run_id} runId={detail.run_id} /></TabsContent>
                    <TabsContent value="overview">
                      <ScrollArea className="max-h-[720px] pr-3">
                        <div className="space-y-5">
                          <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
                            <InfoItem label={tl({ zh: "运行 ID", en: "Run ID" })}>
                              <span className="font-mono text-xs">
                                {detail.run_id}
                              </span>
                            </InfoItem>
                            <InfoItem label={tl({ zh: "策略 ID", en: "Strategy ID" })}>
                              <span className="font-mono text-xs">
                                {detail.strategy_id}
                              </span>
                            </InfoItem>
                            <InfoItem label={tl({ zh: "版本", en: "Version" })}>
                              <span className="font-mono">{tl(strategyVersionLabel(detail))}</span>
                            </InfoItem>
                            <InfoItem label={tl({ zh: "状态", en: "Status" })}>
                              <StatusBadge status={detail.status} />
                            </InfoItem>
                            <InfoItem label={tl({ zh: "发起人", en: "Requested by" })}>
                              <span className="font-mono text-xs">
                                {detail.requested_by}
                              </span>
                            </InfoItem>
                            <InfoItem label={tl({ zh: "发起方类型", en: "Actor type" })}>
                              <span className="font-mono text-xs">
                                {actorType(detail)}
                              </span>
                            </InfoItem>
                            <InfoItem label={tl({ zh: "初始资金", en: "Initial capital" })}>
                              <span className="tabular-nums font-medium">
                                ¥{formatCurrency(initialCapital(detail), 0)}
                              </span>
                            </InfoItem>
                            <InfoItem label={tl({ zh: "创建时间", en: "Created" })}>
                              <span className="tabular-nums">
                                {formatDateTime(detail.created_at)}
                              </span>
                            </InfoItem>
                            <InfoItem label={tl({ zh: "开始时间", en: "Started" })}>
                              <span className="tabular-nums">
                                {formatDateTime(detail.started_at)}
                              </span>
                            </InfoItem>
                            <InfoItem label={tl({ zh: "完成时间", en: "Completed" })}>
                              <span className="tabular-nums">
                                {formatDateTime(detail.completed_at)}
                              </span>
                            </InfoItem>
                            {detail.job_id && (
                              <InfoItem label={tl({ zh: "后台任务", en: "Background job" })}>
                                <span className="font-mono text-xs">
                                  {detail.job_id}
                                </span>
                              </InfoItem>
                            )}
                          </div>

                          {/* 统一任务队列 job 状态(#157):queued/running 时轮询展示阶段/进度/worker */}
                          {detail.job_id && (
                            <Card>
                              <CardHeader className="pb-3">
                                <CardTitle className="flex items-center justify-between text-sm">
                                  <span className="flex items-center gap-2">
                                    <Activity className="h-4 w-4 text-primary" />
                                    {tl({ zh: "后台任务进度", en: "Background job progress" })}
                                    <ResearchHint hint={RESEARCH_HINTS.runs.jobProgress} />
                                  </span>
                                  {jobQuery.data && (
                                    <StatusBadge status={jobQuery.data.status} />
                                  )}
                                </CardTitle>
                              </CardHeader>
                              <CardContent className="space-y-3 text-xs">
                                {jobQuery.isLoading ? (
                                  <p className="text-muted-foreground">
                                    {tl({ zh: "正在加载任务状态…", en: "Loading job status…" })}
                                  </p>
                                ) : jobQuery.isError ? (
                                  <p className="text-destructive">
                                    {tl({ zh: "任务状态加载失败:", en: "Failed to load job status: " })}
                                    {errorMessage(jobQuery.error, tl({ zh: "无法连接任务队列", en: "Cannot connect to the job queue" }))}
                                  </p>
                                ) : jobQuery.data ? (
                                  <>
                                    <div className="grid grid-cols-2 gap-x-4 gap-y-1 md:grid-cols-3">
                                      <span>
                                        {tl({ zh: "任务:", en: "Job:" })}
                                        <span className="ml-1 font-mono text-foreground">
                                          {jobQuery.data.job_id}
                                        </span>
                                      </span>
                                      <span>
                                        {tl({ zh: "当前阶段(含阶段内进度):", en: "Current phase (in-phase progress):" })}
                                        <span className="ml-1 font-mono text-foreground">
                                          {jobQuery.data.phase ?? "—"}
                                        </span>
                                      </span>
                                      <span>
                                        {tl({ zh: "作业整体进度:", en: "Overall job progress:" })}
                                        <span className="ml-1 tabular-nums text-foreground">
                                          {jobQuery.data.progress_done}/
                                          {jobQuery.data.progress_total}
                                        </span>
                                      </span>
                                      <span>
                                        {tl({ zh: "尝试:", en: "Attempts:" })}
                                        <span className="ml-1 tabular-nums text-foreground">
                                          {jobQuery.data.attempt}/
                                          {jobQuery.data.max_attempts}
                                        </span>
                                      </span>
                                      <span>
                                        Worker:
                                        <span className="ml-1 font-mono text-foreground">
                                          {jobQuery.data.worker_id ?? "—"}
                                        </span>
                                      </span>
                                      {jobQuery.data.heartbeat_at && (
                                        <span>
                                          {tl({ zh: "心跳:", en: "Heartbeat:" })}
                                          <span className="ml-1 text-foreground">
                                            {timeAgo(jobQuery.data.heartbeat_at, lang)}
                                          </span>
                                        </span>
                                      )}
                                    </div>
                                    {jobQuery.data.progress_total > 0 && (
                                      <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
                                        <div
                                          className="h-full rounded-full bg-primary transition-all"
                                          style={{
                                            width: `${Math.min(
                                              100,
                                              Math.round(
                                                (jobQuery.data.progress_done /
                                                  jobQuery.data.progress_total) *
                                                  100,
                                              ),
                                            )}%`,
                                          }}
                                        />
                                      </div>
                                    )}
                                    {jobQuery.data.error_code && (
                                      <Alert variant="destructive">
                                        <AlertTitle>
                                          {tl({ zh: "任务失败:", en: "Job failed:" })}
                                          <span className="ml-1 font-mono">
                                            {jobQuery.data.error_code}
                                          </span>
                                        </AlertTitle>
                                        <AlertDescription>
                                          {jobQuery.data.error_summary ?? tl({ zh: "无详细信息", en: "No details available" })}
                                        </AlertDescription>
                                      </Alert>
                                    )}
                                    {jobQuery.data.result_ref && (
                                      <p className="text-muted-foreground">
                                        {tl({ zh: "结果引用:", en: "Result ref:" })}
                                        <span className="ml-1 font-mono text-foreground">
                                          {jobQuery.data.result_ref}
                                        </span>
                                      </p>
                                    )}
                                  </>
                                ) : (
                                  <p className="text-muted-foreground">
                                    {tl({ zh: "任务已结束(运行已进入终态,无活跃 job)。", en: "The job has ended (the run reached a terminal state; no active job)." })}
                                  </p>
                                )}
                              </CardContent>
                            </Card>
                          )}

                          {detail.result_checksum && (
                            <div className="flex items-center gap-2 text-xs text-muted-foreground">
                              <span>{tl({ zh: "结果校验和:", en: "Result checksum:" })}</span>
                              <span className="font-mono text-foreground">
                                {detail.result_checksum}
                              </span>
                            </div>
                          )}

                          {detail.error_code && (
                            <Alert variant="destructive">
                              <AlertTitle>{tl({ zh: "运行失败", en: "Run failed" })}</AlertTitle>
                              <AlertDescription>
                                <span className="font-mono">{detail.error_code}</span>
                                {detail.error_summary && (
                                  <p className="mt-1.5 whitespace-pre-wrap break-words">{detail.error_summary}</p>
                                )}
                              </AlertDescription>
                            </Alert>
                          )}

                          {detail.manifest && <FrozenInputsCard manifest={detail.manifest} result={detail.result} />}

                          <Separator />

                          <div className="space-y-2">
                            <details className="group rounded-md border border-border">
                              <summary className="cursor-pointer select-none px-3 py-2 text-xs font-medium text-muted-foreground hover:text-foreground">
                                {tl({ zh: "冻结清单 (manifest) 原始 JSON", en: "Frozen manifest (raw JSON)" })}
                              </summary>
                              <div className="px-3 pb-3">
                                <JsonBlock label="" value={detail.manifest} />
                              </div>
                            </details>
                            <details className="group rounded-md border border-border">
                              <summary className="cursor-pointer select-none px-3 py-2 text-xs font-medium text-muted-foreground hover:text-foreground">
                                {tl({ zh: "结果 (result) 原始 JSON", en: "Result (raw JSON)" })}
                              </summary>
                              <div className="px-3 pb-3">
                                <JsonBlock label="" value={detail.result} />
                              </div>
                            </details>
                          </div>

                          <div>
                            <div className="mb-2 flex items-center justify-between">
                              <p className="flex items-center gap-1 text-xs font-medium text-muted-foreground">
                                {tl({ zh: "决策产物（artifacts）", en: "Decision artifacts" })}
                                <ResearchHint hint={RESEARCH_HINTS.runs.artifacts} />
                                {artifactsQuery.data &&
                                  tl({
                                    zh: `（${artifactsQuery.data.length}）`,
                                    en: ` (${artifactsQuery.data.length})`,
                                  })}
                              </p>
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => artifactsQuery.refetch()}
                                disabled={artifactsQuery.isFetching}
                              >
                                <RefreshCw
                                  className={cn(
                                    "h-3.5 w-3.5",
                                    artifactsQuery.isFetching && "animate-spin",
                                  )}
                                />
                              </Button>
                            </div>
                            {artifactsQuery.isLoading ? (
                              <LoadingState rows={3} />
                            ) : artifactsQuery.isError ? (
                              <ErrorState
                                message={errorMessage(
                                  artifactsQuery.error,
                                  tl({ zh: "无法加载决策产物", en: "Failed to load decision artifacts" }),
                                )}
                                onRetry={() => artifactsQuery.refetch()}
                              />
                            ) : artifactsQuery.data &&
                              artifactsQuery.data.length > 0 ? (
                              <div className="rounded-lg border border-border">
                                <Table>
                                  <TableHeader className="sticky top-0 bg-card">
                                    <TableRow>
                                      <TableHead className="w-16">#</TableHead>
                                      <TableHead>{tl({ zh: "阶段", en: "Stage" })}</TableHead>
                                      <TableHead>{tl({ zh: "决策 ID", en: "Decision ID" })}</TableHead>
                                      <TableHead>trace_id</TableHead>
                                      <TableHead>{tl({ zh: "父 trace", en: "Parent trace" })}</TableHead>
                                    </TableRow>
                                  </TableHeader>
                                  <TableBody>
                                    {artifactsQuery.data.map((art) => {
                                      const expanded = expandedArtifacts.has(art.artifact_id);
                                      return (
                                        <Fragment key={art.artifact_id}>
                                        <TableRow
                                          className="cursor-pointer"
                                          onClick={() =>
                                            setExpandedArtifacts((current) => {
                                              const next = new Set(current);
                                              if (next.has(art.artifact_id)) {
                                                next.delete(art.artifact_id);
                                              } else {
                                                next.add(art.artifact_id);
                                              }
                                              return next;
                                            })
                                          }
                                        >
                                          <TableCell className="tabular-nums">
                                            <span className={cn("mr-1 inline-block text-[10px] text-muted-foreground transition-transform", expanded && "rotate-90")}>
                                              ▶
                                            </span>
                                            {art.sequence}
                                          </TableCell>
                                          <TableCell>
                                            <Badge variant="info" className="font-mono">
                                              {art.stage}
                                            </Badge>
                                          </TableCell>
                                          <TableCell>
                                            {art.decision_id ? (
                                              <Tooltip>
                                                <TooltipTrigger asChild>
                                                  <span className="font-mono text-xs text-muted-foreground">
                                                    {art.decision_id.slice(0, 12)}…
                                                  </span>
                                                </TooltipTrigger>
                                                <TooltipContent className="font-mono">
                                                  {art.decision_id}
                                                </TooltipContent>
                                              </Tooltip>
                                            ) : (
                                              <span className="text-xs text-muted-foreground">—</span>
                                            )}
                                          </TableCell>
                                          <TableCell>
                                            {art.trace_id ? (
                                              <Tooltip>
                                                <TooltipTrigger asChild>
                                                  <span className="font-mono text-xs text-muted-foreground">
                                                    {art.trace_id.slice(0, 12)}…
                                                  </span>
                                                </TooltipTrigger>
                                                <TooltipContent className="font-mono">
                                                  {art.trace_id}
                                                </TooltipContent>
                                              </Tooltip>
                                            ) : (
                                              <span className="text-xs text-muted-foreground">—</span>
                                            )}
                                          </TableCell>
                                          <TableCell>
                                            {art.parent_trace_ids.length > 0 ? (
                                              <div className="flex flex-wrap gap-1">
                                                {art.parent_trace_ids.map((pid) => (
                                                  <Tooltip key={pid}>
                                                    <TooltipTrigger asChild>
                                                      <Badge
                                                        variant="outline"
                                                        className="font-mono text-[10px]"
                                                      >
                                                        {pid.slice(0, 8)}…
                                                      </Badge>
                                                    </TooltipTrigger>
                                                    <TooltipContent className="font-mono">
                                                      {pid}
                                                    </TooltipContent>
                                                  </Tooltip>
                                                ))}
                                              </div>
                                            ) : (
                                              <span className="text-xs text-muted-foreground">
                                                —
                                              </span>
                                            )}
                                          </TableCell>
                                        </TableRow>
                                        {expanded && (
                                          <TableRow className="hover:bg-transparent">
                                            <TableCell colSpan={5} className="bg-muted/20">
                                              <pre className="max-h-64 overflow-auto rounded-md border border-border bg-muted/30 p-3 font-mono text-[11px] leading-relaxed">
                                                {art.payload ? JSON.stringify(art.payload, null, 2) : "—"}
                                              </pre>
                                            </TableCell>
                                          </TableRow>
                                        )}
                                        </Fragment>
                                      );
                                    })}
                                  </TableBody>
                                </Table>
                              </div>
                            ) : (
                              <EmptyState
                                title={tl({ zh: "暂无决策产物", en: "No decision artifacts yet" })}
                                description={tl({ zh: "该运行尚未产生任何决策产物 (artifact)。", en: "This run has not produced any decision artifacts yet." })}
                              />
                            )}
                          </div>

                          <Separator />

                          <div className="flex flex-wrap items-center gap-2">
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => invalidateAll()}
                            >
                              <RefreshCw className="h-4 w-4" />
                              {tl({ zh: "刷新数据", en: "Refresh data" })}
                            </Button>
                            {!TERMINAL_STATUSES.includes(detail.status) && (
                              <Button
                                variant="destructive"
                                size="sm"
                                disabled={cancelMutation.isPending}
                                onClick={() => setCancelOpen(true)}
                              >
                                <XCircle className="h-4 w-4" />
                                {tl({ zh: "取消运行", en: "Cancel run" })}
                              </Button>
                            )}
                            {detail.status === "completed" && (
                              <Button
                                variant="default"
                                size="sm"
                                onClick={openReplayDialog}
                              >
                                <Play className="h-4 w-4" />
                                {tl({ zh: "重放", en: "Replay" })}
                              </Button>
                            )}
                            {artifactsQuery.data &&
                              artifactsQuery.data.length > 0 && (
                                <Button
                                  variant="outline"
                                  size="sm"
                                  onClick={() => setLineageOpen(true)}
                                >
                                  <GitBranch className="h-4 w-4" />
                                  {tl({ zh: "查看血缘链路", en: "View lineage" })}
                                </Button>
                              )}
                          </div>
                        </div>
                      </ScrollArea>
                    </TabsContent>
                    <TabsContent value="report">
                      <ScrollArea className="max-h-[720px] pr-3">
                        <RunReportView runId={detail.run_id} />
                      </ScrollArea>
                    </TabsContent>
                  </Tabs>
                ) : null}
              </CardContent>
            </Card>
          ) : (
            <EmptyState
              icon={<Activity className="h-8 w-8" />}
              title={tl({ zh: "请从左侧选择一个研究运行", en: "Select a research run on the left" })}
              description={tl({ zh: "选中运行后将展示冻结清单、结果、决策产物血缘，并支持取消与重放操作。", en: "Once selected, the frozen manifest, result, and decision artifact lineage are shown, with cancel and replay actions." })}
            />
          )}
        </div>
      </div>

      <Dialog open={cancelOpen} onOpenChange={setCancelOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{tl({ zh: "取消运行", en: "Cancel run" })}</DialogTitle>
            <DialogDescription>
              {tl({ zh: "取消后该运行将标记为 cancelled，无法继续执行。请确认是否继续。", en: "After cancellation the run is marked cancelled and cannot continue. Are you sure you want to proceed?" })}
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-md border border-border bg-muted/30 p-3">
            <p className="text-xs text-muted-foreground">{tl({ zh: "目标运行", en: "Target run" })}</p>
            <p className="mt-1 font-mono text-sm">{selectedId ?? "—"}</p>
          </div>
          {cancelMutation.isError && (
            <p className="text-sm text-destructive">
              {errorMessage(cancelMutation.error, tl({ zh: "取消失败，请重试", en: "Cancellation failed. Please retry" }))}
            </p>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setCancelOpen(false)}
              disabled={cancelMutation.isPending}
            >
              {tl({ zh: "关闭", en: "Close" })}
            </Button>
            <Button
              variant="destructive"
              disabled={cancelMutation.isPending || !selectedId}
              onClick={() =>
                selectedId && cancelMutation.mutate(selectedId)
              }
            >
              {cancelMutation.isPending ? tl({ zh: "取消中…", en: "Cancelling…" }) : tl({ zh: "确认取消", en: "Confirm cancel" })}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={replayOpen} onOpenChange={setReplayOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{tl({ zh: "重放运行", en: "Replay run" })}</DialogTitle>
            <DialogDescription>
              {tl({ zh: "基于该运行的冻结输入重新执行一次。请填写幂等键与发起人以保证可追溯，相同幂等键不会重复执行。", en: "Re-executes once from this run's frozen inputs. Provide an idempotency key and requester for traceability; the same idempotency key will not execute twice." })}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-2">
              <Label htmlFor="idempotency-key">{tl({ zh: "幂等键 (idempotency_key)", en: "Idempotency key (idempotency_key)" })}</Label>
              <Input
                id="idempotency-key"
                value={idempotencyKey}
                onChange={(e) => setIdempotencyKey(e.target.value)}
                placeholder="replay-..."
                className="font-mono"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="requested-by">{tl({ zh: "发起人 (requested_by)", en: "Requested by (requested_by)" })}</Label>
              <Input
                id="requested-by"
                value={requestedBy}
                onChange={(e) => setRequestedBy(e.target.value)}
                placeholder={tl({ zh: "例如：analyst@finboard", en: "e.g. analyst@finboard" })}
              />
            </div>
          </div>
          {replayMutation.isError && (
            <p className="text-sm text-destructive">
              {errorMessage(replayMutation.error, tl({ zh: "重放失败，请重试", en: "Replay failed. Please retry" }))}
            </p>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setReplayOpen(false)}
              disabled={replayMutation.isPending}
            >
              {tl({ zh: "取消", en: "Cancel" })}
            </Button>
            <Button
              disabled={
                replayMutation.isPending ||
                !idempotencyKey.trim() ||
                !requestedBy.trim() ||
                !selectedId
              }
              onClick={() =>
                selectedId &&
                replayMutation.mutate({
                  runId: selectedId,
                  body: {
                    idempotency_key: idempotencyKey.trim(),
                    requested_by: requestedBy.trim(),
                  },
                })
              }
            >
              {replayMutation.isPending ? tl({ zh: "提交中…", en: "Submitting…" }) : tl({ zh: "确认重放", en: "Confirm replay" })}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={lineageOpen} onOpenChange={setLineageOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{tl({ zh: "血缘链路", en: "Lineage" })}</DialogTitle>
            <DialogDescription>
              {tl({ zh: "以叶子产物 trace 追溯的完整血缘树，展示从根到叶的执行链路与每个阶段的输入产出。", en: "The complete lineage tree traced from the leaf artifact, showing the execution chain from root to leaf and the inputs and outputs of each stage." })}
            </DialogDescription>
          </DialogHeader>
          {lineageQuery.isLoading ? (
            <LoadingState rows={4} />
          ) : lineageQuery.isError ? (
            <ErrorState
              message={errorMessage(lineageQuery.error, tl({ zh: "无法加载血缘链路", en: "Failed to load lineage" }))}
              onRetry={() => lineageQuery.refetch()}
            />
          ) : lineageQuery.data ? (
            <ScrollArea className="max-h-[480px] pr-3">
              <div className="space-y-3">
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span>{tl({ zh: "运行:", en: "Run:" })}</span>
                  <span className="font-mono text-foreground">
                    {lineageQuery.data.run_id}
                  </span>
                  <span>·</span>
                  <span>{tl({ zh: "叶子 trace:", en: "Leaf trace:" })}</span>
                  <span className="font-mono text-foreground">
                    {lineageQuery.data.leaf_trace_id}
                  </span>
                </div>
                <div className="rounded-lg border border-border">
                  <Table>
                    <TableHeader className="sticky top-0 bg-card">
                      <TableRow>
                        <TableHead className="w-16">#</TableHead>
                        <TableHead>{tl({ zh: "阶段", en: "Stage" })}</TableHead>
                        <TableHead>trace_id</TableHead>
                        <TableHead>{tl({ zh: "父 trace", en: "Parent trace" })}</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {lineageQuery.data.artifacts.map((art, idx) => (
                        <TableRow key={art.trace_id}>
                          <TableCell className="tabular-nums">{idx + 1}</TableCell>
                          <TableCell>
                            <Badge variant="info" className="font-mono">
                              {art.stage}
                            </Badge>
                          </TableCell>
                          <TableCell>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <span className="font-mono text-xs text-muted-foreground">
                                  {art.trace_id.slice(0, 12)}…
                                </span>
                              </TooltipTrigger>
                              <TooltipContent className="font-mono">
                                {art.trace_id}
                              </TooltipContent>
                            </Tooltip>
                          </TableCell>
                          <TableCell>
                            {art.parent_trace_ids.length > 0 ? (
                              <div className="flex flex-wrap gap-1">
                                {art.parent_trace_ids.map((pid) => (
                                  <Badge
                                    key={pid}
                                    variant="outline"
                                    className="font-mono text-[10px]"
                                  >
                                    {pid.slice(0, 8)}…
                                  </Badge>
                                ))}
                              </div>
                            ) : (
                              <span className="text-xs text-muted-foreground">
                                —
                              </span>
                            )}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </div>
            </ScrollArea>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setLineageOpen(false)}>
              {tl({ zh: "关闭", en: "Close" })}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <CreateResearchRunDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={(run) => setSelectedId(run.run_id)}
      />
    </div>
  );
}
