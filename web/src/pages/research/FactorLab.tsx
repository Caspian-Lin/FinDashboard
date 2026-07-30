import * as React from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Atom,
  Layers,
  Signal,
  TestTube,
  RefreshCw,
  Plus,
  ChevronRight,
  Check,
} from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import {
  Tabs,
  TabsList,
  TabsTrigger,
  TabsContent,
} from "@/components/ui/tabs";
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Input, Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectTrigger,
  SelectContent,
  SelectItem,
  SelectValue,
} from "@/components/ui/select";
import { EmptyState, LoadingState } from "@/components/ui/states";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  ResearchHint,
  WorkflowIndicator,
  NextStepCTA,
} from "@/components/research/ResearchHint";
import { RESEARCH_HINTS } from "@/lib/research-hints";
import {
  factorLabApi,
  datasetApi,
  type FactorCatalogEntry,
  type FeatureSnapshot,
  type FactorSignal,
  type FactorExperiment,
  type FactorExperimentCreate,
  type DatasetReleaseSummary,
} from "@/lib/research";
import { cn, formatDateTime, formatNumber } from "@/lib/utils";

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message) return err.message;
  if (typeof err === "string" && err) return err;
  return fallback;
}

function CatalogTab() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["factor-catalog"],
    queryFn: () => factorLabApi.catalog(),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data ? `共 ${data.length} 个因子` : "加载中…"}
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          刷新
        </Button>
      </div>

      {isLoading ? (
        <LoadingState rows={6} />
      ) : isError ? (
        <EmptyState
          icon={<Atom className="h-8 w-8" />}
          title="加载失败"
          description={
            error instanceof Error ? error.message : "无法加载因子目录"
          }
        />
      ) : data && data.length > 0 ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {data.map((factor: FactorCatalogEntry) => (
            <Card key={factor.name}>
              <CardHeader className="pb-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <CardTitle className="truncate font-mono text-base">
                      {factor.name}
                    </CardTitle>
                    <p className="mt-1 font-mono text-xs text-muted-foreground">
                      v{factor.version}
                    </p>
                  </div>
                  <Badge variant="info">{factor.role}</Badge>
                </div>
              </CardHeader>
              <CardContent className="space-y-3">
                <p className="line-clamp-2 text-sm text-muted-foreground">
                  {factor.description}
                </p>
                <div className="flex flex-wrap gap-1">
                  {(factor.dependencies ?? []).length > 0 ? (
                    (factor.dependencies ?? []).map((dep) => (
                      <Badge key={dep} variant="secondary" className="font-mono">
                        {dep}
                      </Badge>
                    ))
                  ) : (
                    <span className="text-xs text-muted-foreground">无依赖</span>
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={<Atom className="h-8 w-8" />}
          title="暂无因子"
          description="因子目录注册后将在此列出，包含角色、版本与依赖关系。"
        />
      )}
    </div>
  );
}

function FeaturesTab() {
  const [expandedId, setExpandedId] = React.useState<string | null>(null);
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["feature-snapshots", { limit: 50 }],
    queryFn: () => factorLabApi.features(undefined, 50),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data ? `共 ${data.length} 个特征快照` : "加载中…"}
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          刷新
        </Button>
      </div>

      {isLoading ? (
        <LoadingState rows={6} />
      ) : isError ? (
        <EmptyState
          icon={<Layers className="h-8 w-8" />}
          title="加载失败"
          description={
            error instanceof Error ? error.message : "无法加载特征快照"
          }
        />
      ) : data && data.length > 0 ? (
        <div className="overflow-hidden rounded-lg border border-border">
          <ScrollArea className="max-h-[600px]">
            <Table>
              <TableHeader className="sticky top-0 bg-card">
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>快照 ID</TableHead>
                  <TableHead>数据发布 ID</TableHead>
                  <TableHead className="text-right">因子数</TableHead>
                  <TableHead className="text-right">标的数</TableHead>
                  <TableHead className="text-right">行数</TableHead>
                  <TableHead>研究状态</TableHead>
                  <TableHead>创建时间</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((snap: FeatureSnapshot) => {
                  const open = expandedId === snap.snapshot_id;
                  return (
                    <React.Fragment key={snap.snapshot_id}>
                      <TableRow
                        className="cursor-pointer hover:bg-muted/50"
                        onClick={() => setExpandedId(open ? null : snap.snapshot_id)}
                      >
                        <TableCell className="p-0">
                          <ChevronRight
                            className={cn(
                              "h-4 w-4 text-muted-foreground transition-transform",
                              open && "rotate-90",
                            )}
                          />
                        </TableCell>
                        <TableCell>
                          <span className="font-mono text-xs text-muted-foreground">
                            {snap.snapshot_id}
                          </span>
                        </TableCell>
                        <TableCell>
                          <span className="font-mono text-xs text-muted-foreground">
                            {snap.dataset_release_id}
                          </span>
                        </TableCell>
                        <TableCell className="tabular-nums text-right">
                          {snap.factor_names.length}
                        </TableCell>
                        <TableCell className="tabular-nums text-right">
                          {formatNumber(snap.symbol_count, 0)}
                        </TableCell>
                        <TableCell className="tabular-nums text-right">
                          {formatNumber(snap.row_count, 0)}
                        </TableCell>
                        <TableCell>
                          <StatusBadge status={snap.research_status} />
                        </TableCell>
                        <TableCell className="tabular-nums text-muted-foreground">
                          {formatDateTime(snap.created_at)}
                        </TableCell>
                      </TableRow>
                      {open && (
                        <TableRow className="bg-muted/30 hover:bg-muted/30">
                          <TableCell colSpan={8} className="p-4">
                            <div className="space-y-3">
                              <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                                <span>
                                  标的数：
                                  <span className="font-mono text-foreground">
                                    {formatNumber(snap.symbol_count, 0)}
                                  </span>
                                </span>
                                <span>
                                  行数：
                                  <span className="font-mono text-foreground">
                                    {formatNumber(snap.row_count, 0)}
                                  </span>
                                </span>
                                <span>
                                  状态：
                                  <StatusBadge status={snap.research_status} />
                                </span>
                                <span>
                                  校验和：
                                  <span className="font-mono text-foreground">
                                    {snap.checksum}
                                  </span>
                                </span>
                              </div>
                              <Separator />
                              <div>
                                <p className="mb-2 text-xs font-medium text-muted-foreground">
                                  因子列表（{snap.factor_names.length}）
                                </p>
                                <div className="flex flex-wrap gap-1">
                                  {snap.factor_names.map((f) => (
                                    <Badge
                                      key={f}
                                      variant="secondary"
                                      className="font-mono"
                                    >
                                      {f}
                                    </Badge>
                                  ))}
                                </div>
                              </div>
                            </div>
                          </TableCell>
                        </TableRow>
                      )}
                    </React.Fragment>
                  );
                })}
              </TableBody>
            </Table>
          </ScrollArea>
        </div>
      ) : (
        <EmptyState
          icon={<Layers className="h-8 w-8" />}
          title="暂无特征快照"
          description="特征快照由因子引擎批量计算生成，记录每个数据发布下的因子计算结果。"
        />
      )}
    </div>
  );
}

function SignalsTab() {
  const [expandedId, setExpandedId] = React.useState<string | null>(null);
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["factor-signals", { limit: 50 }],
    queryFn: () => factorLabApi.signals({ limit: 50 }),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data ? `共 ${data.length} 个因子信号` : "加载中…"}
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          刷新
        </Button>
      </div>

      {isLoading ? (
        <LoadingState rows={6} />
      ) : isError ? (
        <EmptyState
          icon={<Signal className="h-8 w-8" />}
          title="加载失败"
          description={
            error instanceof Error ? error.message : "无法加载因子信号"
          }
        />
      ) : data && data.length > 0 ? (
        <div className="overflow-hidden rounded-lg border border-border">
          <ScrollArea className="max-h-[600px]">
            <Table>
              <TableHeader className="sticky top-0 bg-card">
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>信号 ID</TableHead>
                  <TableHead>因子名</TableHead>
                  <TableHead>研究状态</TableHead>
                  <TableHead>快照 ID</TableHead>
                  <TableHead>创建时间</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((sig: FactorSignal) => {
                  const open = expandedId === sig.signal_id;
                  const payloadKeys = Object.keys(sig.payload ?? {});
                  return (
                    <React.Fragment key={sig.signal_id}>
                      <TableRow
                        className="cursor-pointer hover:bg-muted/50"
                        onClick={() =>
                          setExpandedId(open ? null : sig.signal_id)
                        }
                      >
                        <TableCell className="p-0">
                          <ChevronRight
                            className={cn(
                              "h-4 w-4 text-muted-foreground transition-transform",
                              open && "rotate-90",
                            )}
                          />
                        </TableCell>
                        <TableCell>
                          <span className="font-mono text-xs text-muted-foreground">
                            {sig.signal_id}
                          </span>
                        </TableCell>
                        <TableCell className="font-mono text-sm">
                          {sig.factor_name}
                        </TableCell>
                        <TableCell>
                          <StatusBadge status={sig.research_status} />
                        </TableCell>
                        <TableCell>
                          <span className="font-mono text-xs text-muted-foreground">
                            {sig.snapshot_id}
                          </span>
                        </TableCell>
                        <TableCell className="tabular-nums text-muted-foreground">
                          {formatDateTime(sig.created_at)}
                        </TableCell>
                      </TableRow>
                      {open && (
                        <TableRow className="bg-muted/30 hover:bg-muted/30">
                          <TableCell colSpan={6} className="p-4">
                            <div className="space-y-3">
                              <p className="text-xs font-medium text-muted-foreground">
                                信号 payload（{payloadKeys.length} 个字段，含
                                IC 值、分层收益等）
                              </p>
                              <pre className="max-h-80 overflow-auto rounded-md border border-border bg-background p-3 font-mono text-xs leading-relaxed">
                                {JSON.stringify(sig.payload, null, 2)}
                              </pre>
                            </div>
                          </TableCell>
                        </TableRow>
                      )}
                    </React.Fragment>
                  );
                })}
              </TableBody>
            </Table>
          </ScrollArea>
        </div>
      ) : (
        <EmptyState
          icon={<Signal className="h-8 w-8" />}
          title="暂无因子信号"
          description="因子信号由因子计算产出，用于驱动策略目标仓位与组合决策。"
        />
      )}
    </div>
  );
}

interface ExperimentFormState {
  hypothesis: string;
  factor_names: string[];
  dataset_release_id: string;
  feature_snapshot_id: string;
  in_sample_start: string;
  in_sample_end: string;
  oos_start: string;
  oos_end: string;
  trial_budget: number;
  benchmark_symbol: string;
  transaction_cost_bps: number;
  quantiles: number;
  comparison_group: string;
}

const DEFAULT_FORM: ExperimentFormState = {
  hypothesis: "",
  factor_names: [],
  dataset_release_id: "",
  feature_snapshot_id: "",
  in_sample_start: "",
  in_sample_end: "",
  oos_start: "",
  oos_end: "",
  trial_budget: 50,
  benchmark_symbol: "000300.SH",
  transaction_cost_bps: 5,
  quantiles: 5,
  comparison_group: "default",
};

function CreateExperimentDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = React.useState<ExperimentFormState>(DEFAULT_FORM);

  const { data: catalog } = useQuery({
    queryKey: ["factor-catalog"],
    queryFn: () => factorLabApi.catalog(),
    enabled: !!open,
  });

  const { data: releases } = useQuery({
    queryKey: ["dataset-releases", { limit: 100 }],
    queryFn: () => datasetApi.releases({ limit: 100 }),
    enabled: !!open,
  });

  const { data: features } = useQuery({
    queryKey: ["feature-snapshots", { limit: 100 }],
    queryFn: () => factorLabApi.features(undefined, 100),
    enabled: !!open,
  });

  const mutation = useMutation({
    mutationFn: (body: FactorExperimentCreate) =>
      factorLabApi.createFactorExperiment(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["factor-experiments"],
      });
      onOpenChange(false);
      setForm(DEFAULT_FORM);
    },
  });

  React.useEffect(() => {
    if (!open) {
      setForm(DEFAULT_FORM);
      mutation.reset();
    }
  }, [open, mutation]);

  const toggleFactor = (name: string) => {
    setForm((prev) => ({
      ...prev,
      factor_names: prev.factor_names.includes(name)
        ? prev.factor_names.filter((f) => f !== name)
        : [...prev.factor_names, name],
    }));
  };

  const valid =
    form.hypothesis.trim().length >= 10 &&
    form.factor_names.length > 0 &&
    form.dataset_release_id !== "" &&
    form.feature_snapshot_id !== "";

  const handleSubmit = () => {
    if (!valid) return;
    mutation.mutate({
      hypothesis: form.hypothesis.trim(),
      factor_names: form.factor_names,
      dataset_release_id: form.dataset_release_id,
      feature_snapshot_id: form.feature_snapshot_id,
      plan: {
        in_sample_start: form.in_sample_start,
        in_sample_end: form.in_sample_end,
        oos_start: form.oos_start,
        oos_end: form.oos_end,
        trial_budget: form.trial_budget,
        benchmark_symbol: form.benchmark_symbol.trim(),
        transaction_cost_bps: form.transaction_cost_bps,
        quantiles: form.quantiles,
      },
      comparison_group: form.comparison_group.trim() || "default",
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>创建因子实验</DialogTitle>
          <DialogDescription>
            冻结因子假设、数据版本与评估计划，用于系统性检验因子的预测能力。
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="exp-hypothesis">
              因子假设
              <span className="ml-1 text-xs text-muted-foreground">
                （至少 10 字符）
              </span>
            </Label>
            <Textarea
              id="exp-hypothesis"
              value={form.hypothesis}
              onChange={(e) =>
                setForm((p) => ({ ...p, hypothesis: e.target.value }))
              }
              placeholder="例如：过去 20 日动量排名对未来 5 日收益有正向预测力"
              className="min-h-[72px]"
            />
          </div>

          <div className="space-y-2">
            <Label>
              因子列表
              <span className="ml-1 text-xs text-muted-foreground">
                （从目录选择，已选 {form.factor_names.length}）
              </span>
            </Label>
            <div className="max-h-44 overflow-y-auto rounded-md border border-border p-2">
              {catalog && catalog.length > 0 ? (
                <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                  {catalog.map((f) => {
                    const checked = form.factor_names.includes(f.name);
                    return (
                      <button
                        key={f.name}
                        type="button"
                        onClick={() => toggleFactor(f.name)}
                        className={cn(
                          "flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors",
                          checked
                            ? "bg-primary/10 text-primary"
                            : "hover:bg-muted",
                        )}
                      >
                        <span
                          className={cn(
                            "flex h-4 w-4 shrink-0 items-center justify-center rounded border",
                            checked
                              ? "border-primary bg-primary text-primary-foreground"
                              : "border-input",
                          )}
                        >
                          {checked && <Check className="h-3 w-3" />}
                        </span>
                        <span className="truncate font-mono text-xs">
                          {f.name}
                        </span>
                        <Badge
                          variant="secondary"
                          className="ml-auto text-[10px]"
                        >
                          {f.role}
                        </Badge>
                      </button>
                    );
                  })}
                </div>
              ) : (
                <p className="p-2 text-xs text-muted-foreground">
                  因子目录为空或加载中…
                </p>
              )}
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-2">
              <Label>数据发布</Label>
              <Select
                value={form.dataset_release_id}
                onValueChange={(v) =>
                  setForm((p) => ({ ...p, dataset_release_id: v }))
                }
              >
                <SelectTrigger>
                  <SelectValue placeholder="选择数据发布" />
                </SelectTrigger>
                <SelectContent>
                  {(releases ?? []).map((r: DatasetReleaseSummary) => (
                    <SelectItem
                      key={r.release_id}
                      value={r.release_id}
                    >
                      <span className="font-mono">
                        {r.dataset_name} · {r.version}
                      </span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>特征快照</Label>
              <Select
                value={form.feature_snapshot_id}
                onValueChange={(v) =>
                  setForm((p) => ({ ...p, feature_snapshot_id: v }))
                }
              >
                <SelectTrigger>
                  <SelectValue placeholder="选择特征快照" />
                </SelectTrigger>
                <SelectContent>
                  {(features ?? []).map((s: FeatureSnapshot) => (
                    <SelectItem
                      key={s.snapshot_id}
                      value={s.snapshot_id}
                    >
                      <span className="font-mono">
                        {s.snapshot_id.slice(0, 12)}… ·{" "}
                        {s.factor_names.length} 因子
                      </span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <Separator />

          <div className="space-y-2">
            <Label className="text-xs font-medium text-muted-foreground">
              评估计划
            </Label>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label htmlFor="is-start" className="text-xs">
                  样本内起始
                </Label>
                <Input
                  id="is-start"
                  type="date"
                  value={form.in_sample_start}
                  onChange={(e) =>
                    setForm((p) => ({
                      ...p,
                      in_sample_start: e.target.value,
                    }))
                  }
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="is-end" className="text-xs">
                  样本内结束
                </Label>
                <Input
                  id="is-end"
                  type="date"
                  value={form.in_sample_end}
                  onChange={(e) =>
                    setForm((p) => ({
                      ...p,
                      in_sample_end: e.target.value,
                    }))
                  }
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="oos-start" className="text-xs">
                  OOS 起始
                </Label>
                <Input
                  id="oos-start"
                  type="date"
                  value={form.oos_start}
                  onChange={(e) =>
                    setForm((p) => ({ ...p, oos_start: e.target.value }))
                  }
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="oos-end" className="text-xs">
                  OOS 结束
                </Label>
                <Input
                  id="oos-end"
                  type="date"
                  value={form.oos_end}
                  onChange={(e) =>
                    setForm((p) => ({ ...p, oos_end: e.target.value }))
                  }
                />
              </div>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div className="space-y-1">
              <Label htmlFor="trial-budget" className="text-xs">
                试验预算
              </Label>
              <Input
                id="trial-budget"
                type="number"
                min={1}
                value={form.trial_budget}
                onChange={(e) =>
                  setForm((p) => ({
                    ...p,
                    trial_budget: Number(e.target.value) || 0,
                  }))
                }
                className="tabular-nums"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="quantiles" className="text-xs">
                分层数
              </Label>
              <Input
                id="quantiles"
                type="number"
                min={2}
                value={form.quantiles}
                onChange={(e) =>
                  setForm((p) => ({
                    ...p,
                    quantiles: Number(e.target.value) || 0,
                  }))
                }
                className="tabular-nums"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="benchmark" className="text-xs">
                基准代码
              </Label>
              <Input
                id="benchmark"
                value={form.benchmark_symbol}
                onChange={(e) =>
                  setForm((p) => ({
                    ...p,
                    benchmark_symbol: e.target.value,
                  }))
                }
                className="font-mono"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="tx-cost" className="text-xs">
                交易成本 (bps)
              </Label>
              <Input
                id="tx-cost"
                type="number"
                min={0}
                value={form.transaction_cost_bps}
                onChange={(e) =>
                  setForm((p) => ({
                    ...p,
                    transaction_cost_bps: Number(e.target.value) || 0,
                  }))
                }
                className="tabular-nums"
              />
            </div>
          </div>

          <div className="space-y-1">
            <Label htmlFor="comparison-group" className="text-xs">
              对比组
            </Label>
            <Input
              id="comparison-group"
              value={form.comparison_group}
              onChange={(e) =>
                setForm((p) => ({ ...p, comparison_group: e.target.value }))
              }
              className="font-mono"
            />
          </div>
        </div>

        {mutation.isError && (
          <p className="text-sm text-destructive">
            {errorMessage(mutation.error, "创建因子实验失败")}
          </p>
        )}

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={mutation.isPending}
          >
            取消
          </Button>
          <Button
            disabled={mutation.isPending || !valid}
            onClick={handleSubmit}
          >
            {mutation.isPending ? "创建中…" : "创建实验"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ExperimentsTab() {
  const [createOpen, setCreateOpen] = React.useState(false);
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["factor-experiments", { limit: 50 }],
    queryFn: () => factorLabApi.listFactorExperiments({ limit: 50 }),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data ? `共 ${data.length} 个因子实验` : "加载中…"}
        </p>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => refetch()}
            disabled={isFetching}
          >
            <RefreshCw
              className={cn("h-4 w-4", isFetching && "animate-spin")}
            />
            刷新
          </Button>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" />
            创建实验
          </Button>
        </div>
      </div>

      {isLoading ? (
        <LoadingState rows={6} />
      ) : isError ? (
        <EmptyState
          icon={<TestTube className="h-8 w-8" />}
          title="加载失败"
          description={
            error instanceof Error ? error.message : "无法加载因子实验"
          }
        />
      ) : data && data.length > 0 ? (
        <div className="overflow-hidden rounded-lg border border-border">
          <ScrollArea className="max-h-[600px]">
            <Table>
              <TableHeader className="sticky top-0 bg-card">
                <TableRow>
                  <TableHead>实验 ID</TableHead>
                  <TableHead>假设</TableHead>
                  <TableHead>因子列表</TableHead>
                  <TableHead>数据发布 ID</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>创建时间</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((exp: FactorExperiment) => (
                  <TableRow key={exp.factor_experiment_id}>
                    <TableCell>
                      <span className="font-mono text-xs text-muted-foreground">
                        {exp.factor_experiment_id}
                      </span>
                    </TableCell>
                    <TableCell className="max-w-xs truncate text-muted-foreground">
                      {exp.hypothesis}
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {exp.factor_names.map((f) => (
                          <Badge
                            key={f}
                            variant="secondary"
                            className="font-mono"
                          >
                            {f}
                          </Badge>
                        ))}
                      </div>
                    </TableCell>
                    <TableCell>
                      <span className="font-mono text-xs text-muted-foreground">
                        {exp.dataset_release_id}
                      </span>
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={exp.status} />
                    </TableCell>
                    <TableCell className="tabular-nums text-muted-foreground">
                      {formatDateTime(exp.created_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </ScrollArea>
        </div>
      ) : (
        <EmptyState
          icon={<TestTube className="h-8 w-8" />}
          title="暂无因子实验"
          description="创建因子实验以系统性验证因子假设的预测力，并与机器验证实验关联。"
          action={
            <Button size="sm" onClick={() => setCreateOpen(true)}>
              <Plus className="h-4 w-4" />
              创建实验
            </Button>
          }
        />
      )}

      <CreateExperimentDialog open={createOpen} onOpenChange={setCreateOpen} />
    </div>
  );
}

export default function FactorLab() {
  return (
    <div>
      <PageHeader
        title="因子实验室"
        description="因子发现、信号预览与实验验证"
        breadcrumbs={[
          { label: "研究", href: "/research" },
          { label: "因子实验室" },
        ]}
      />

      <WorkflowIndicator currentPath="/research/factors" />

      <Tabs defaultValue="catalog">
        <TabsList className="h-auto flex-wrap gap-1">
          <TabsTrigger value="catalog">因子目录</TabsTrigger>
          <ResearchHint hint={RESEARCH_HINTS.factors.catalog} />
          <TabsTrigger value="features">特征快照</TabsTrigger>
          <ResearchHint hint={RESEARCH_HINTS.factors.features} />
          <TabsTrigger value="signals">因子信号</TabsTrigger>
          <ResearchHint hint={RESEARCH_HINTS.factors.signals} />
          <TabsTrigger value="experiments">因子实验</TabsTrigger>
          <ResearchHint hint={RESEARCH_HINTS.factors.experiments} />
        </TabsList>

        <TabsContent value="catalog">
          <CatalogTab />
        </TabsContent>
        <TabsContent value="features">
          <FeaturesTab />
        </TabsContent>
        <TabsContent value="signals">
          <SignalsTab />
        </TabsContent>
        <TabsContent value="experiments">
          <ExperimentsTab />
        </TabsContent>
      </Tabs>

      <NextStepCTA
        nextPath="/research/strategy"
        nextLabel="策略 Studio"
        description="将因子组合为完整的交易策略"
      />
    </div>
  );
}
