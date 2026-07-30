import { type ReactNode, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Activity,
  ArrowLeft,
  GitBranch,
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
  researchRunApi,
  type ResearchRunSummary,
} from "@/lib/research";
import { cn, formatCurrency, formatDateTime, timeAgo } from "@/lib/utils";

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "queued", label: "排队中" },
  { value: "running", label: "运行中" },
  { value: "completed", label: "已完成" },
  { value: "failed", label: "失败" },
  { value: "cancelled", label: "已取消" },
];

const TERMINAL_STATUSES = ["completed", "failed", "cancelled"];

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
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

export default function ResearchRuns() {
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [cancelOpen, setCancelOpen] = useState(false);
  const [replayOpen, setReplayOpen] = useState(false);
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
          <Button asChild variant="outline" size="sm">
            <Link to="/research">
              <ArrowLeft className="h-4 w-4" />
              返回研究
            </Link>
          </Button>
        }
      />

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
                          v{run.strategy_version}
                        </Badge>
                      </div>
                      <div className="mt-2 flex items-center justify-between gap-2 text-xs text-muted-foreground">
                        <span className="tabular-nums">
                          ¥{formatCurrency(run.initial_capital, 0)}
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
                          {detail.strategy_id}@v{detail.strategy_version}
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
                          <span className="font-mono">v{detail.strategy_version}</span>
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
                            ¥{formatCurrency(detail.initial_capital, 0)}
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
    </div>
  );
}
