import { WorkflowIndicator, NextStepCTA } from "@/components/research/ResearchHint";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
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
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import {
  factorLabApi,
  researchRunApi,
  strategySpecApi,
  featureSnapshotNames,
  featureSnapshotSymbolCount,
  type ResearchStrategySpec,
  type ResearchRunQueueIn,
  type ResearchRunSummary,
} from "@/lib/research";
import { cn, formatCurrency, formatDateTime, timeAgo } from "@/lib/utils";

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "queued", label: "排队中" },
  { value: "running", label: "运行中" },
  { value: "completed", label: "已完成" },
  { value: "failed", label: "失败" },
  { value: "interrupted", label: "已中断" },
  { value: "rejected", label: "已拒绝" },
  { value: "cancelled", label: "已取消" },
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

function strategyVersionLabel(run: ResearchRunSummary): string {
  const version = strategyVersion(run);
  return version === null ? "版本未记录" : `v${version}`;
}

function initialCapital(run: ResearchRunSummary): number | null {
  if (typeof run.initial_capital === "number") return run.initial_capital;
  const value = run.manifest?.initial_capital;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function specReleaseIds(strategy: ResearchStrategySpec | undefined): string[] {
  const plan = manifestRecord(strategy?.spec, "validation_plan");
  const ids = plan?.dataset_release_ids;
  return Array.isArray(ids) ? ids.filter((item): item is string => typeof item === "string") : [];
}

function specNeedsFactorSnapshot(strategy: ResearchStrategySpec | undefined): boolean {
  const graph = manifestRecord(strategy?.spec, "feature_graph");
  const nodes = graph?.nodes;
  if (!Array.isArray(nodes)) return false;
  return nodes.some((node) => {
    if (!node || typeof node !== "object") return false;
    const item = node as Record<string, unknown>;
    return item.kind === "factor" || item.kind === "risk_factor";
  });
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

function CreateResearchRunDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (run: ResearchRunSummary) => void;
}) {
  const queryClient = useQueryClient();
  const [strategyId, setStrategyId] = useState("");
  const [factorSnapshotIds, setFactorSnapshotIds] = useState<string[]>([]);
  const [initialCapital, setInitialCapital] = useState("100000");
  const [requestedBy, setRequestedBy] = useState("console");
  const [idempotencyKey, setIdempotencyKey] = useState("");

  const strategiesQuery = useQuery({
    queryKey: ["spec-list", "queue"],
    queryFn: () => strategySpecApi.list(100),
    enabled: open,
  });
  const selectedStrategy = strategiesQuery.data?.find(
    (item) => `${item.strategy_id}@${item.version}` === strategyId,
  );
  const releaseIds = specReleaseIds(selectedStrategy);
  const needsFactorSnapshot = specNeedsFactorSnapshot(selectedStrategy);
  const snapshotsQuery = useQuery({
    queryKey: ["feature-snapshots", "queue", releaseIds[0]],
    queryFn: () => factorLabApi.features(releaseIds[0], 100),
    enabled: open && releaseIds.length > 0 && needsFactorSnapshot,
  });

  useEffect(() => {
    if (!open) return;
    setStrategyId("");
    setFactorSnapshotIds([]);
    setInitialCapital("100000");
    setRequestedBy("console");
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      setIdempotencyKey(`research-${crypto.randomUUID()}`);
    } else {
      setIdempotencyKey(`research-${Date.now()}`);
    }
  }, [open]);

  useEffect(() => {
    setFactorSnapshotIds([]);
  }, [strategyId]);

  const queueMutation = useMutation({
    mutationFn: (body: ResearchRunQueueIn) => researchRunApi.queue(body),
    onSuccess: (run) => {
      void queryClient.invalidateQueries({ queryKey: ["research-runs"] });
      onOpenChange(false);
      onCreated(run);
    },
  });

  const factorSnapshotReady = !needsFactorSnapshot || factorSnapshotIds.length > 0;
  const capital = Number(initialCapital);
  const valid =
    !!selectedStrategy &&
    selectedStrategy.published &&
    releaseIds.length > 0 &&
    factorSnapshotReady &&
    capital >= 100000 &&
    capital <= 500000 &&
    requestedBy.trim() !== "" &&
    idempotencyKey.trim().length >= 8;

  const submit = () => {
    if (!selectedStrategy || !valid) return;
    queueMutation.mutate({
      idempotency_key: idempotencyKey.trim(),
      strategy_id: selectedStrategy.strategy_id,
      strategy_version: selectedStrategy.version,
      dataset_release_ids: releaseIds,
      factor_snapshot_ids: factorSnapshotIds,
      parameters: {},
      validation_config: {},
      portfolio_config: {},
      risk_config: {},
      execution_config: {},
      fee_config: {},
      benchmark_config: {},
      code_version: "web-ui-v1",
      initial_capital: capital,
      requested_by: requestedBy.trim(),
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>排队研究运行</DialogTitle>
          <DialogDescription>
            冻结已发布策略、数据发布和因子快照。此操作只登记 queued 任务，不会在网页请求中执行回测。
          </DialogDescription>
        </DialogHeader>

        <Alert variant="info">
          <AlertTitle>执行边界</AlertTitle>
          <AlertDescription>
            提交后需要受控的离线 worker/CLI 消费 queued 任务。运行完成后才可进入组合、模拟盘和研究报告；页面不会自动启动任何运行。
          </AlertDescription>
        </Alert>

        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="research-run-strategy">已发布策略规格</Label>
            <Select value={strategyId} onValueChange={setStrategyId}>
              <SelectTrigger id="research-run-strategy">
                <SelectValue placeholder="选择已发布策略" />
              </SelectTrigger>
              <SelectContent>
                {(strategiesQuery.data ?? [])
                  .filter((item) => item.published)
                  .map((item) => (
                    <SelectItem
                      key={`${item.strategy_id}@${item.version}`}
                      value={`${item.strategy_id}@${item.version}`}
                    >
                      <span className="font-mono">{item.strategy_id}@v{item.version}</span>
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
            {strategiesQuery.isError && (
              <p className="text-xs text-destructive">策略规格加载失败，请关闭后重试。</p>
            )}
            {!strategiesQuery.isLoading && (strategiesQuery.data ?? []).filter((item) => item.published).length === 0 && (
              <p className="text-xs text-warning">
                暂无已发布策略。请先到 <Link className="underline" to="/research/strategy">策略 Studio</Link> 保存并发布版本。
              </p>
            )}
          </div>

          <div className="rounded-md border border-border bg-muted/20 p-3 text-xs">
            <p className="font-medium text-foreground">冻结数据发布</p>
            {releaseIds.length > 0 ? (
              <div className="mt-2 flex flex-wrap gap-1">
                {releaseIds.map((id) => (
                  <Badge key={id} variant="info" className="font-mono text-[10px]">{id}</Badge>
                ))}
              </div>
            ) : (
              <p className="mt-1 text-muted-foreground">选择策略后显示其验证计划要求。</p>
            )}
          </div>

          {needsFactorSnapshot && (
            <div className="space-y-2 rounded-md border border-warning/30 bg-warning/5 p-3">
              <Label>冻结因子快照（至少 1 个）</Label>
              {snapshotsQuery.isLoading ? (
                <p className="text-xs text-muted-foreground">正在加载与数据发布匹配的快照…</p>
              ) : snapshotsQuery.data && snapshotsQuery.data.length > 0 ? (
                <div className="space-y-1">
                  {snapshotsQuery.data.map((snapshot) => {
                    const checked = factorSnapshotIds.includes(snapshot.snapshot_id);
                    return (
                      <label key={snapshot.snapshot_id} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-xs hover:bg-accent">
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => setFactorSnapshotIds((current) => checked ? current.filter((id) => id !== snapshot.snapshot_id) : [...current, snapshot.snapshot_id])}
                        />
                        <span className="font-mono">{snapshot.snapshot_id}</span>
                        <span className="text-muted-foreground">{featureSnapshotSymbolCount(snapshot)} 标的 · {featureSnapshotNames(snapshot).length} 个因子</span>
                      </label>
                    );
                  })}
                </div>
              ) : (
                <p className="text-xs text-warning">
                  暂无匹配快照。请先在 <Link className="underline" to="/research/factors">因子实验室</Link> 生成并发布特征快照。
                </p>
              )}
            </div>
          )}

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="research-run-capital">初始资金</Label>
              <Input id="research-run-capital" type="number" min={100000} max={500000} value={initialCapital} onChange={(event) => setInitialCapital(event.target.value)} />
              <p className="text-[11px] text-muted-foreground">研究运行允许 ¥100,000–¥500,000。</p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="research-run-requested-by">发起人</Label>
              <Input id="research-run-requested-by" value={requestedBy} onChange={(event) => setRequestedBy(event.target.value)} placeholder="analyst@finboard" />
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="research-run-idempotency">幂等键</Label>
            <Input id="research-run-idempotency" value={idempotencyKey} onChange={(event) => setIdempotencyKey(event.target.value)} className="font-mono text-xs" />
            <p className="text-[11px] text-muted-foreground">相同幂等键重复提交不会生成第二个研究运行。</p>
          </div>
        </div>

        {queueMutation.isError && (
          <Alert variant="destructive">
            <AlertTitle>排队失败</AlertTitle>
            <AlertDescription>{errorMessage(queueMutation.error, "研究运行未登记，请检查策略、数据和因子快照")}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={queueMutation.isPending}>取消</Button>
          <Button onClick={submit} disabled={!valid || queueMutation.isPending}>
            <Plus className="mr-2 h-4 w-4" />
            {queueMutation.isPending ? "登记中…" : "登记 queued 运行"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function ResearchRuns() {
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [cancelOpen, setCancelOpen] = useState(false);
  const [replayOpen, setReplayOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [requestedBy, setRequestedBy] = useState("");
  const [lineageOpen, setLineageOpen] = useState(false);

  const listQuery = useQuery({
    queryKey: ["research-runs", "list", { limit: 50 }],
    queryFn: () => researchRunApi.list({ limit: 50 }),
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

  const filteredRuns = useMemo(() => {
    if (!listQuery.data) return [];
    if (statusFilter === "all") return listQuery.data;
    return listQuery.data.filter((r) => r.status === statusFilter);
  }, [listQuery.data, statusFilter]);

  const leafTraceId = useMemo(() => {
    if (!artifactsQuery.data || artifactsQuery.data.length === 0) return null;
    return artifactsQuery.data.reduce(
      (acc, a) => (a.sequence > acc.sequence ? a : acc),
      artifactsQuery.data[0],
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

  const detail = detailQuery.data;

  return (
    <div>
      <PageHeader
        title="研究运行"
        description="冻结输入、血缘追踪与运行重放"
        breadcrumbs={[
          { label: "研究", href: "/research" },
          { label: "研究运行" },
        ]}
        actions={
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => setCreateOpen(true)}>
              <Plus className="h-4 w-4" />
              排队研究运行
            </Button>
            <Button asChild variant="outline" size="sm">
              <Link to="/research">
                <ArrowLeft className="h-4 w-4" />
                返回研究
              </Link>
            </Button>
          </div>
        }
      />
      <WorkflowIndicator currentPath="/research/runs" />

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <Label className="text-xs text-muted-foreground">状态筛选</Label>
          <Select value={statusFilter} onValueChange={setStatusFilter}>
            <SelectTrigger className="h-9 w-[160px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STATUS_OPTIONS.map((opt) => (
                <SelectItem key={opt.value} value={opt.value}>
                  {opt.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <p className="text-sm text-muted-foreground">
          {listQuery.data
            ? `共 ${listQuery.data.length} 个运行（当前显示 ${filteredRuns.length}）`
            : "加载中…"}
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
          刷新
        </Button>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-1">
          <CardHeader className="pb-3">
            <CardTitle className="text-base">运行列表</CardTitle>
          </CardHeader>
          <CardContent>
            {listQuery.isLoading ? (
              <LoadingState rows={5} />
            ) : listQuery.isError ? (
              <ErrorState
                message={errorMessage(listQuery.error, "无法加载运行列表")}
                onRetry={() => listQuery.refetch()}
              />
            ) : filteredRuns.length > 0 ? (
              <ScrollArea className="max-h-[700px] pr-3">
                <div className="space-y-2">
                  {filteredRuns.map((run: ResearchRunSummary) => (
                    <button
                      key={run.run_id}
                      type="button"
                      onClick={() => setSelectedId(run.run_id)}
                      className={cn(
                        "w-full rounded-md border border-border p-3 text-left transition-colors hover:bg-accent",
                        selectedId === run.run_id &&
                          "border-primary bg-accent ring-1 ring-primary/40",
                      )}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <StatusBadge status={run.status} />
                        <span className="font-mono text-xs text-muted-foreground">
                          {run.run_id}
                        </span>
                      </div>
                      <div className="mt-2 flex items-center justify-between gap-2">
                        <span className="truncate font-mono text-xs text-foreground">
                          {run.strategy_id}
                        </span>
                        <Badge variant="secondary" className="font-mono">
                          {strategyVersionLabel(run)}
                        </Badge>
                      </div>
                      <div className="mt-2 flex items-center justify-between gap-2 text-xs text-muted-foreground">
                        <span className="tabular-nums">
                          ¥{formatCurrency(initialCapital(run), 0)}
                        </span>
                        <span>{timeAgo(run.created_at)}</span>
                      </div>
                    </button>
                  ))}
                </div>
              </ScrollArea>
            ) : (
              <EmptyState
                icon={<Activity className="h-8 w-8" />}
                title="暂无运行"
                description="当前筛选条件下没有研究运行，可切换状态筛选或刷新列表。"
              />
            )}
          </CardContent>
        </Card>

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
                          {detail.strategy_id}@{strategyVersionLabel(detail)}
                        </span>
                      </div>
                    )}
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => setSelectedId(null)}
                    aria-label="取消选择"
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
                      "无法加载运行详情",
                    )}
                    onRetry={() => detailQuery.refetch()}
                  />
                ) : detail ? (
                  <ScrollArea className="max-h-[720px] pr-3">
                    <div className="space-y-5">
                      <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
                        <InfoItem label="运行 ID">
                          <span className="font-mono text-xs">
                            {detail.run_id}
                          </span>
                        </InfoItem>
                        <InfoItem label="策略 ID">
                          <span className="font-mono text-xs">
                            {detail.strategy_id}
                          </span>
                        </InfoItem>
                        <InfoItem label="版本">
                          <span className="font-mono">{strategyVersionLabel(detail)}</span>
                        </InfoItem>
                        <InfoItem label="状态">
                          <StatusBadge status={detail.status} />
                        </InfoItem>
                        <InfoItem label="发起人">
                          <span className="font-mono text-xs">
                            {detail.requested_by}
                          </span>
                        </InfoItem>
                        <InfoItem label="初始资金">
                          <span className="tabular-nums font-medium">
                            ¥{formatCurrency(initialCapital(detail), 0)}
                          </span>
                        </InfoItem>
                        <InfoItem label="创建时间">
                          <span className="tabular-nums">
                            {formatDateTime(detail.created_at)}
                          </span>
                        </InfoItem>
                        <InfoItem label="开始时间">
                          <span className="tabular-nums">
                            {formatDateTime(detail.started_at)}
                          </span>
                        </InfoItem>
                        <InfoItem label="完成时间">
                          <span className="tabular-nums">
                            {formatDateTime(detail.completed_at)}
                          </span>
                        </InfoItem>
                      </div>

                      {detail.result_checksum && (
                        <div className="flex items-center gap-2 text-xs text-muted-foreground">
                          <span>结果校验和:</span>
                          <span className="font-mono text-foreground">
                            {detail.result_checksum}
                          </span>
                        </div>
                      )}

                      {detail.error_code && (
                        <Alert variant="destructive">
                          <AlertTitle>运行失败</AlertTitle>
                          <AlertDescription>
                            <span className="font-mono">{detail.error_code}</span>
                          </AlertDescription>
                        </Alert>
                      )}

                      <Separator />

                      <JsonBlock label="冻结清单 (manifest)" value={detail.manifest} />
                      <JsonBlock label="结果 (result)" value={detail.result} />

                      <div>
                        <div className="mb-2 flex items-center justify-between">
                          <p className="text-xs font-medium text-muted-foreground">
                            决策产物（artifacts）
                            {artifactsQuery.data &&
                              `（${artifactsQuery.data.length}）`}
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
                              "无法加载决策产物",
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
                                  <TableHead>阶段</TableHead>
                                  <TableHead>决策 ID</TableHead>
                                  <TableHead>trace_id</TableHead>
                                  <TableHead>父 trace</TableHead>
                                </TableRow>
                              </TableHeader>
                              <TableBody>
                                {artifactsQuery.data.map((art) => (
                                  <TableRow key={art.artifact_id}>
                                    <TableCell className="tabular-nums">
                                      {art.sequence}
                                    </TableCell>
                                    <TableCell>
                                      <Badge variant="info" className="font-mono">
                                        {art.stage}
                                      </Badge>
                                    </TableCell>
                                    <TableCell>
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
                                ))}
                              </TableBody>
                            </Table>
                          </div>
                        ) : (
                          <EmptyState
                            title="暂无决策产物"
                            description="该运行尚未产生任何决策产物 (artifact)。"
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
                          刷新数据
                        </Button>
                        {!TERMINAL_STATUSES.includes(detail.status) && (
                          <Button
                            variant="destructive"
                            size="sm"
                            disabled={cancelMutation.isPending}
                            onClick={() => setCancelOpen(true)}
                          >
                            <XCircle className="h-4 w-4" />
                            取消运行
                          </Button>
                        )}
                        {detail.status === "completed" && (
                          <Button
                            variant="default"
                            size="sm"
                            onClick={openReplayDialog}
                          >
                            <Play className="h-4 w-4" />
                            重放
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
                              查看血缘链路
                            </Button>
                          )}
                      </div>
                    </div>
                  </ScrollArea>
                ) : null}
              </CardContent>
            </Card>
          ) : (
            <EmptyState
              icon={<Activity className="h-8 w-8" />}
              title="请从左侧选择一个研究运行"
              description="选中运行后将展示冻结清单、结果、决策产物血缘，并支持取消与重放操作。"
            />
          )}
        </div>
      </div>

      <Dialog open={cancelOpen} onOpenChange={setCancelOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>取消运行</DialogTitle>
            <DialogDescription>
              取消后该运行将标记为 cancelled，无法继续执行。请确认是否继续。
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-md border border-border bg-muted/30 p-3">
            <p className="text-xs text-muted-foreground">目标运行</p>
            <p className="mt-1 font-mono text-sm">{selectedId ?? "—"}</p>
          </div>
          {cancelMutation.isError && (
            <p className="text-sm text-destructive">
              {errorMessage(cancelMutation.error, "取消失败，请重试")}
            </p>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setCancelOpen(false)}
              disabled={cancelMutation.isPending}
            >
              关闭
            </Button>
            <Button
              variant="destructive"
              disabled={cancelMutation.isPending || !selectedId}
              onClick={() =>
                selectedId && cancelMutation.mutate(selectedId)
              }
            >
              {cancelMutation.isPending ? "取消中…" : "确认取消"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={replayOpen} onOpenChange={setReplayOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>重放运行</DialogTitle>
            <DialogDescription>
              基于该运行的冻结输入重新执行一次。请填写幂等键与发起人以保证可追溯，相同幂等键不会重复执行。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-2">
              <Label htmlFor="idempotency-key">幂等键 (idempotency_key)</Label>
              <Input
                id="idempotency-key"
                value={idempotencyKey}
                onChange={(e) => setIdempotencyKey(e.target.value)}
                placeholder="replay-..."
                className="font-mono"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="requested-by">发起人 (requested_by)</Label>
              <Input
                id="requested-by"
                value={requestedBy}
                onChange={(e) => setRequestedBy(e.target.value)}
                placeholder="例如：analyst@finboard"
              />
            </div>
          </div>
          {replayMutation.isError && (
            <p className="text-sm text-destructive">
              {errorMessage(replayMutation.error, "重放失败，请重试")}
            </p>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setReplayOpen(false)}
              disabled={replayMutation.isPending}
            >
              取消
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
              {replayMutation.isPending ? "提交中…" : "确认重放"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={lineageOpen} onOpenChange={setLineageOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>血缘链路</DialogTitle>
            <DialogDescription>
              以叶子产物 trace 追溯的完整血缘树，展示从根到叶的执行链路与每个阶段的输入产出。
            </DialogDescription>
          </DialogHeader>
          {lineageQuery.isLoading ? (
            <LoadingState rows={4} />
          ) : lineageQuery.isError ? (
            <ErrorState
              message={errorMessage(lineageQuery.error, "无法加载血缘链路")}
              onRetry={() => lineageQuery.refetch()}
            />
          ) : lineageQuery.data ? (
            <ScrollArea className="max-h-[480px] pr-3">
              <div className="space-y-3">
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span>运行:</span>
                  <span className="font-mono text-foreground">
                    {lineageQuery.data.run_id}
                  </span>
                  <span>·</span>
                  <span>叶子 trace:</span>
                  <span className="font-mono text-foreground">
                    {lineageQuery.data.leaf_trace_id}
                  </span>
                </div>
                <div className="rounded-lg border border-border">
                  <Table>
                    <TableHeader className="sticky top-0 bg-card">
                      <TableRow>
                        <TableHead className="w-16">#</TableHead>
                        <TableHead>阶段</TableHead>
                        <TableHead>trace_id</TableHead>
                        <TableHead>父 trace</TableHead>
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
              关闭
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <CreateResearchRunDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={(run) => setSelectedId(run.run_id)}
      />
      <NextStepCTA
        nextPath="/research/portfolio"
        nextLabel="组合与风险"
        description="将冻结的策略转化为目标权重和离散交易计划"
      />
    </div>
  );
}
