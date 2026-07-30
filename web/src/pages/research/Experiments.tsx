import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ArrowLeft, FlaskConical, RefreshCw, Trash2 } from "lucide-react";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  EmptyState,
  ErrorState,
  LoadingState,
} from "@/components/ui/states";
import {
  experimentApi,
  type ExperimentStatus,
  type ValidationExperiment,
} from "@/lib/research";
import { cn, formatDateTime, timeAgo } from "@/lib/utils";

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "draft", label: "草稿" },
  { value: "registered", label: "已注册" },
  { value: "running", label: "运行中" },
  { value: "completed", label: "已完成" },
  { value: "failed", label: "失败" },
  { value: "rejected", label: "已拒绝" },
];

function JsonBlock({
  label,
  value,
}: {
  label: string;
  value: Record<string, unknown>;
}) {
  return (
    <div>
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <pre className="mt-1 overflow-x-auto rounded-md border border-border bg-muted/30 p-3 font-mono text-xs leading-relaxed">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

export default function Experiments() {
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [deleteOpen, setDeleteOpen] = useState(false);

  const listQuery = useQuery({
    queryKey: ["validation-experiments", "list", { status: statusFilter }],
    queryFn: () =>
      experimentApi.list({
        status:
          statusFilter === "all"
            ? undefined
            : (statusFilter as ExperimentStatus),
        limit: 50,
      }),
  });

  const detailQuery = useQuery({
    queryKey: ["validation-experiments", "detail", selectedId],
    queryFn: () => experimentApi.get(selectedId as string),
    enabled: selectedId !== null,
  });

  const rejectMutation = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      experimentApi.reject(id, reason),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["validation-experiments"],
      });
      setRejectOpen(false);
      setRejectReason("");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => experimentApi.delete(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["validation-experiments"],
      });
      setSelectedId(null);
      setDeleteOpen(false);
    },
  });

  const invalidateAll = () => {
    void queryClient.invalidateQueries({ queryKey: ["validation-experiments"] });
  };

  return (
    <div>
      <PageHeader
        title="实验与 OOS"
        description="机器验证实验、阈值与稳健性检验"
        breadcrumbs={[
          { label: "研究", href: "/research" },
          { label: "实验与 OOS" },
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
            ? `共 ${listQuery.data.length} 个实验`
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
            <CardTitle className="text-base">实验列表</CardTitle>
          </CardHeader>
          <CardContent>
            {listQuery.isLoading ? (
              <LoadingState rows={5} />
            ) : listQuery.isError ? (
              <ErrorState
                message={errorMessage(
                  listQuery.error,
                  "无法加载实验列表",
                )}
                onRetry={() => listQuery.refetch()}
              />
            ) : listQuery.data && listQuery.data.length > 0 ? (
              <ScrollArea className="h-[640px] pr-3">
                <div className="space-y-2">
                  {listQuery.data.map((exp: ValidationExperiment) => (
                    <button
                      key={exp.experiment_id}
                      type="button"
                      onClick={() => setSelectedId(exp.experiment_id)}
                      className={cn(
                        "w-full rounded-md border border-border p-3 text-left transition-colors hover:bg-accent",
                        selectedId === exp.experiment_id &&
                          "border-primary bg-accent ring-1 ring-primary/40",
                      )}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <StatusBadge status={exp.status} />
                        <span className="font-mono text-xs text-muted-foreground">
                          {exp.experiment_id}
                        </span>
                      </div>
                      <p className="mt-2 line-clamp-2 text-sm text-foreground">
                        {exp.hypothesis}
                      </p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {timeAgo(exp.created_at)}
                      </p>
                    </button>
                  ))}
                </div>
              </ScrollArea>
            ) : (
              <EmptyState
                icon={<FlaskConical className="h-8 w-8" />}
                title="暂无实验"
                description="当前筛选条件下没有验证实验，可切换状态筛选或刷新列表。"
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
                      <span className="font-mono text-sm">
                        {selectedId}
                      </span>
                    </CardTitle>
                    {detailQuery.data && (
                      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                        <StatusBadge status={detailQuery.data.status} />
                        <span className="font-mono">
                          v{detailQuery.data.version_stamp}
                        </span>
                        <span>·</span>
                        <span>{formatDateTime(detailQuery.data.created_at)}</span>
                        {detailQuery.data.supersedes_id && (
                          <>
                            <span>·</span>
                            <span>
                              取代自{" "}
                              <span className="font-mono">
                                {detailQuery.data.supersedes_id}
                              </span>
                            </span>
                          </>
                        )}
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
                      "无法加载实验详情",
                    )}
                    onRetry={() => detailQuery.refetch()}
                  />
                ) : detailQuery.data ? (
                  <ScrollArea className="h-[600px] pr-3">
                    <div className="space-y-4">
                      <div>
                        <p className="text-xs font-medium text-muted-foreground">
                          假设
                        </p>
                        <p className="mt-1 text-sm leading-relaxed text-foreground">
                          {detailQuery.data.hypothesis}
                        </p>
                      </div>

                      {detailQuery.data.notes && (
                        <div>
                          <p className="text-xs font-medium text-muted-foreground">
                            备注
                          </p>
                          <p className="mt-1 whitespace-pre-wrap text-sm text-muted-foreground">
                            {detailQuery.data.notes}
                          </p>
                        </div>
                      )}

                      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                        <JsonBlock
                          label="验证计划"
                          value={detailQuery.data.plan}
                        />
                        <JsonBlock
                          label="阈值"
                          value={detailQuery.data.thresholds}
                        />
                        <JsonBlock
                          label="稳健性"
                          value={detailQuery.data.robustness}
                        />
                        <JsonBlock
                          label="策略参数空间"
                          value={detailQuery.data.strategy_params_space}
                        />
                      </div>

                      <div>
                        <div className="mb-2 flex items-center justify-between">
                          <p className="text-xs font-medium text-muted-foreground">
                            试验记录（{detailQuery.data.trials.length}）
                          </p>
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => detailQuery.refetch()}
                            disabled={detailQuery.isFetching}
                          >
                            <RefreshCw
                              className={cn(
                                "h-3.5 w-3.5",
                                detailQuery.isFetching && "animate-spin",
                              )}
                            />
                          </Button>
                        </div>
                        {detailQuery.data.trials.length > 0 ? (
                          <div className="rounded-lg border border-border">
                            <Table>
                              <TableHeader>
                                <TableRow>
                                  <TableHead>试验 ID</TableHead>
                                  <TableHead>状态</TableHead>
                                  <TableHead>失败原因</TableHead>
                                  <TableHead>创建时间</TableHead>
                                </TableRow>
                              </TableHeader>
                              <TableBody>
                                {detailQuery.data.trials.map((trial) => (
                                  <TableRow key={trial.trial_id}>
                                    <TableCell>
                                      <span className="font-mono text-xs text-muted-foreground">
                                        {trial.trial_id}
                                      </span>
                                    </TableCell>
                                    <TableCell>
                                      <StatusBadge status={trial.status} />
                                    </TableCell>
                                    <TableCell className="max-w-xs">
                                      {trial.failure_reason ? (
                                        <span className="text-xs text-destructive">
                                          {trial.failure_reason}
                                        </span>
                                      ) : (
                                        <span className="text-xs text-muted-foreground">
                                          —
                                        </span>
                                      )}
                                    </TableCell>
                                    <TableCell className="tabular-nums text-xs text-muted-foreground">
                                      {formatDateTime(trial.created_at)}
                                    </TableCell>
                                  </TableRow>
                                ))}
                              </TableBody>
                            </Table>
                          </div>
                        ) : (
                          <EmptyState
                            title="暂无试验"
                            description="该实验尚未登记任何试验记录。"
                          />
                        )}
                      </div>

                      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => invalidateAll()}
                        >
                          <RefreshCw className="h-4 w-4" />
                          刷新数据
                        </Button>
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={
                            detailQuery.data.status === "rejected" ||
                            rejectMutation.isPending
                          }
                          onClick={() => {
                            setRejectReason("");
                            setRejectOpen(true);
                          }}
                        >
                          拒绝实验
                        </Button>
                        <Button
                          variant="destructive"
                          size="sm"
                          disabled={deleteMutation.isPending}
                          onClick={() => setDeleteOpen(true)}
                        >
                          <Trash2 className="h-4 w-4" />
                          删除实验
                        </Button>
                      </div>
                    </div>
                  </ScrollArea>
                ) : null}
              </CardContent>
            </Card>
          ) : (
            <EmptyState
              icon={<FlaskConical className="h-8 w-8" />}
              title="请从左侧选择一个实验"
              description="选中实验后将展示完整假设、验证计划、阈值、稳健性配置以及试验记录。"
            />
          )}
        </div>
      </div>

      <Dialog open={rejectOpen} onOpenChange={setRejectOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>拒绝实验</DialogTitle>
            <DialogDescription>
              拒绝后该实验将标记为 rejected，无法继续注册试验。请填写拒绝原因以便审计追溯。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="reject-reason">拒绝原因</Label>
            <Textarea
              id="reject-reason"
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              placeholder="例如：样本外夏普未达阈值 / 数据泄漏 / 参数过拟合..."
              rows={4}
            />
          </div>
          {rejectMutation.isError && (
            <p className="text-sm text-destructive">
              {errorMessage(rejectMutation.error, "拒绝失败，请重试")}
            </p>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setRejectOpen(false)}
              disabled={rejectMutation.isPending}
            >
              取消
            </Button>
            <Button
              variant="destructive"
              disabled={
                !rejectReason.trim() || rejectMutation.isPending || !selectedId
              }
              onClick={() =>
                selectedId &&
                rejectMutation.mutate({
                  id: selectedId,
                  reason: rejectReason.trim(),
                })
              }
            >
              {rejectMutation.isPending ? "提交中…" : "确认拒绝"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>删除实验</DialogTitle>
            <DialogDescription>
              该操作不可撤销，将永久删除实验及其试验记录。请确认是否继续。
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-md border border-border bg-muted/30 p-3">
            <p className="text-xs text-muted-foreground">目标实验</p>
            <p className="mt-1 font-mono text-sm">{selectedId ?? "—"}</p>
          </div>
          {deleteMutation.isError && (
            <p className="text-sm text-destructive">
              {errorMessage(deleteMutation.error, "删除失败，请重试")}
            </p>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setDeleteOpen(false)}
              disabled={deleteMutation.isPending}
            >
              取消
            </Button>
            <Button
              variant="destructive"
              disabled={
                deleteMutation.isPending || !selectedId
              }
              onClick={() =>
                selectedId && deleteMutation.mutate(selectedId)
              }
            >
              {deleteMutation.isPending ? "删除中…" : "确认删除"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
