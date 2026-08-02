import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, Plus, RefreshCw, Trash2, X } from "lucide-react";
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
import { Badge } from "@/components/ui/badge";
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
import { Input, Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  EmptyState,
  ErrorState,
  LoadingState,
} from "@/components/ui/states";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  HintLabel,
  NextStepCTA,
  WorkflowIndicator,
} from "@/components/research/ResearchHint";
import { RESEARCH_HINTS } from "@/lib/research-hints";
import {
  experimentApi,
  type ExperimentStatus,
  type ValidationExperiment,
} from "@/lib/research";
import { fetchJSON } from "@/lib/api";
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

const STRATEGY_KINDS: { value: string; label: string }[] = [
  { value: "ma_cross", label: "均线交叉 (MA Cross)" },
  { value: "multi_factor", label: "多因子 (Multi-Factor)" },
  { value: "etf_rotation", label: "ETF 轮动 (ETF Rotation)" },
  { value: "mean_reversion", label: "均值回归 (Mean Reversion)" },
  { value: "convertible_double_low", label: "可转债双低 (Convertible Double-Low)" },
  { value: "futures_tsmom", label: "期货动量 (Futures TSMOM)" },
];

const MODE_OPTIONS: { value: string; label: string; desc: string }[] = [
  {
    value: "rolling",
    label: "滚动窗口 (Rolling)",
    desc: "固定长度窗口向前滚动，旧数据逐步丢弃",
  },
  {
    value: "expanding",
    label: "扩展窗口 (Expanding)",
    desc: "起点固定，终点逐步前移，数据量递增",
  },
];

interface HintShape {
  title: string;
  description: string;
  detail?: string;
}

const THRESHOLD_META: { key: string; label: string; hint: HintShape }[] = [
  {
    key: "min_oos_sharpe",
    label: "OOS 夏普下限",
    hint: {
      title: "min_oos_sharpe（样本外夏普比率下限）",
      description:
        "夏普比率衡量风险调整后收益，>0.5 表示每承担 1 单位风险获得 0.5 单位超额收益。",
      detail: "默认 0.5。值越高要求越严格。",
    },
  },
  {
    key: "max_oos_drawdown",
    label: "OOS 最大回撤上限",
    hint: {
      title: "max_oos_drawdown（样本外最大回撤上限）",
      description:
        "回撤是从历史最高点到最低点的跌幅。25% 意味着最多允许亏 25%。",
      detail: "默认 0.25。值越低要求越严格。",
    },
  },
  {
    key: "max_pbo",
    label: "过拟合概率上限",
    hint: {
      title: "max_pbo（回测过拟合概率上限）",
      description:
        "PBO = Probability of Backtest Overfitting。>50% 说明策略大概率是过拟合的。",
      detail: "默认 0.5。越低越严格。",
    },
  },
  {
    key: "min_deflated_sharpe",
    label: "Deflated Sharpe 下限",
    hint: {
      title: "min_deflated_sharpe（Deflated Sharpe Ratio 下限）",
      description:
        "对夏普比率进行多重检验校正后的值，消除'试了很多参数碰巧有一个好'的偏差。",
      detail: "默认 0.0。>0 表示校正后仍有正超额收益。",
    },
  },
];

interface CreateFormState {
  hypothesis: string;
  strategy_kind: string;
  mode: string;
  train_start: string;
  train_end: string;
  validation_start: string;
  validation_end: string;
  test_start: string;
  test_end: string;
  benchmark_symbol: string;
  trial_budget: string;
}

const DEFAULT_FORM: CreateFormState = {
  hypothesis: "",
  strategy_kind: "ma_cross",
  mode: "rolling",
  train_start: "2018-01-01",
  train_end: "2022-12-31",
  validation_start: "2023-01-01",
  validation_end: "2023-12-31",
  test_start: "2024-01-01",
  test_end: "2024-06-30",
  benchmark_symbol: "000300.SH",
  trial_budget: "50",
};

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

function strField(
  record: Record<string, unknown> | undefined,
  key: string,
): string {
  const v = record?.[key];
  if (v === null || v === undefined) return "—";
  return String(v);
}

function versionStampText(value: string | Record<string, unknown>): string {
  if (typeof value === "string") return value;
  const labels: Record<string, string> = {
    matching_model_version: "撮合",
    asset_rules_version: "资产规则",
    factor_version: "因子",
    strategy_kind: "策略",
  };
  const valueText = (item: unknown): string => {
    if (item && typeof item === "object") {
      const size = Object.keys(item as Record<string, unknown>).length;
      return `${size} 项已冻结`;
    }
    return String(item);
  };
  const parts = Object.entries(value)
    .filter(([, item]) => item !== null && item !== undefined && item !== "")
    .map(([key, item]) => `${labels[key] ?? key} ${valueText(item)}`);
  return parts.length > 0 ? parts.join(" · ") : "版本信息待补充";
}

function experimentStatusLabel(status: string): string {
  return (
    {
      hypothesis: "假设已冻结",
      draft: "草稿",
      registered: "已注册",
      running: "运行中",
      completed: "已完成",
      failed: "失败",
      rejected: "已拒绝",
    } as Record<string, string>
  )[status] ?? status;
}

function daysBetween(start: string, end: string): number {
  const s = new Date(start).getTime();
  const e = new Date(end).getTime();
  if (Number.isNaN(s) || Number.isNaN(e)) return 0;
  return Math.max(1, Math.round((e - s) / 86400000));
}

function JsonBlock({
  label,
  value,
}: {
  label: string;
  value: Record<string, unknown>;
}) {
  if (Object.keys(value).length === 0) return null;
  return (
    <div>
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <pre className="mt-1 overflow-x-auto rounded-md border border-border bg-muted/30 p-3 font-mono text-xs leading-relaxed">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

function PlanTimeline({ plan }: { plan: Record<string, unknown> }) {
  const ts = strField(plan, "train_start");
  const te = strField(plan, "train_end");
  const vs = strField(plan, "validation_start");
  const ve = strField(plan, "validation_end");
  const os = strField(plan, "test_start");
  const oe = strField(plan, "test_end");

  const trainDays = ts !== "—" && te !== "—" ? daysBetween(ts, te) : 100;
  const valDays = vs !== "—" && ve !== "—" ? daysBetween(vs, ve) : 50;
  const testDays = os !== "—" && oe !== "—" ? daysBetween(os, oe) : 50;
  const total = trainDays + valDays + testDays;

  const segments = [
    {
      label: "训练期",
      range: `${ts} → ${te}`,
      flex: trainDays / total,
      color: "bg-primary/60",
      track: "bg-primary/15",
    },
    {
      label: "验证期",
      range: `${vs} → ${ve}`,
      flex: valDays / total,
      color: "bg-warning/60",
      track: "bg-warning/15",
    },
    {
      label: "测试期 (OOS)",
      range: `${os} → ${oe}`,
      flex: testDays / total,
      color: "bg-success/60",
      track: "bg-success/15",
    },
  ];

  return (
    <div className="space-y-3">
      <div className="flex h-3 overflow-hidden rounded-full border border-border bg-muted/20">
        {segments.map((seg) => (
          <div
            key={seg.label}
            className={cn("border-r border-card last:border-r-0", seg.color)}
            style={{ flexGrow: seg.flex }}
            title={`${seg.label}: ${seg.range}`}
          />
        ))}
      </div>
      <div className="grid grid-cols-3 gap-2">
        {segments.map((seg) => (
          <div
            key={seg.label}
            className={cn("rounded-md border p-2", seg.track)}
          >
            <div className="flex items-center gap-1.5">
              <span className={cn("h-2 w-2 rounded-full", seg.color)} />
              <span className="text-xs font-medium text-foreground">
                {seg.label}
              </span>
            </div>
            <p className="mt-1 font-mono text-xs text-muted-foreground">
              {seg.range}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}

function PlanParams({ plan }: { plan: Record<string, unknown> }) {
  const cells: { key: string; label: string; hint?: HintShape }[] = [
    {
      key: "mode",
      label: "模式",
      hint: {
        title: "验证模式",
        description:
          "rolling = 固定长度窗口向前滚动；expanding = 起点固定，终点前移。",
      },
    },
    { key: "train_window_days", label: "训练窗口(天)" },
    { key: "test_window_days", label: "测试窗口(天)" },
    { key: "step_days", label: "滚动步长(天)" },
    {
      key: "trial_budget",
      label: "试验预算",
      hint: {
        title: "试验次数预算",
        description:
          "参数搜索的最大试验次数。预算越大搜索越充分，但多重检验风险也越高。",
      },
    },
    {
      key: "benchmark_symbol",
      label: "基准",
      hint: {
        title: "基准代码",
        description:
          "用于计算超额收益的基准标的。000300.SH = 沪深300指数。",
      },
    },
  ];

  return (
    <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
      {cells.map((cell) => (
        <div
          key={cell.key}
          className="rounded-md border border-border bg-muted/20 p-2.5"
        >
          <div className="flex items-center gap-1">
            <span className="text-xs text-muted-foreground">{cell.label}</span>
            {cell.hint && <HintLabel hint={cell.hint}>{""}</HintLabel>}
          </div>
          <p className="mt-0.5 font-mono text-sm font-medium text-foreground">
            {strField(plan, cell.key)}
          </p>
        </div>
      ))}
    </div>
  );
}

function ThresholdTable({
  thresholds,
}: {
  thresholds: Record<string, unknown>;
}) {
  const knownKeys = new Set(THRESHOLD_META.map((m) => m.key));
  const extraKeys = Object.keys(thresholds).filter((k) => !knownKeys.has(k));

  return (
    <div className="rounded-lg border border-border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-1/2">阈值</TableHead>
            <TableHead className="text-right">值</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {THRESHOLD_META.map((meta) => {
            const raw = thresholds[meta.key];
            const display =
              typeof raw === "number"
                ? raw.toFixed(4)
                : raw !== undefined && raw !== null
                  ? String(raw)
                  : "—";
            return (
              <TableRow key={meta.key}>
                <TableCell>
                  <HintLabel hint={meta.hint}>{meta.label}</HintLabel>
                </TableCell>
                <TableCell className="text-right font-mono tabular-nums">
                  {display}
                </TableCell>
              </TableRow>
            );
          })}
          {extraKeys.map((key) => {
            const raw = thresholds[key];
            const display =
              typeof raw === "number"
                ? raw.toFixed(4)
                : String(raw ?? "—");
            return (
              <TableRow key={key}>
                <TableCell className="font-mono text-xs text-muted-foreground">
                  {key}
                </TableCell>
                <TableCell className="text-right font-mono tabular-nums">
                  {display}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}

function CreateExperimentDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = React.useState<CreateFormState>(DEFAULT_FORM);

  React.useEffect(() => {
    if (open) setForm(DEFAULT_FORM);
  }, [open]);

  const update = <K extends keyof CreateFormState>(
    key: K,
    value: CreateFormState[K],
  ) => setForm((prev) => ({ ...prev, [key]: value }));

  const createMutation = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      fetchJSON("/research/experiments", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["validation-experiments"],
      });
      onOpenChange(false);
    },
  });

  const hypothesisValid = form.hypothesis.trim().length >= 10;
  const canSubmit = hypothesisValid && !createMutation.isPending;

  const handleSubmit = () => {
    const body = {
      hypothesis: form.hypothesis.trim(),
      version_stamp: {
        matching_model_version: "v2",
        asset_rules_version: "v1",
        factor_version: null,
        strategy_kind: form.strategy_kind,
      },
      plan: {
        mode: form.mode,
        train_start: form.train_start,
        train_end: form.train_end,
        validation_start: form.validation_start,
        validation_end: form.validation_end,
        test_start: form.test_start,
        test_end: form.test_end,
        train_window_days: 504,
        test_window_days: 63,
        step_days: 63,
        trial_budget: Number(form.trial_budget) || 50,
        benchmark_symbol: form.benchmark_symbol.trim() || "000300.SH",
      },
    };
    createMutation.mutate(body);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>创建验证实验</DialogTitle>
          <DialogDescription>
            定义策略假设、验证计划和 OOS 测试区间。提交后进入 draft 状态，可在实验详情中拒绝或删除。
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="exp-hypothesis">
              <HintLabel
                hint={{
                  title: "策略假设",
                  description:
                    "用一句话描述你要验证的策略假设。需可证伪 —— 明确在什么条件下假设不成立。",
                  detail: "最少 10 个字符，最多 2000 个字符。",
                }}
              >
                策略假设
              </HintLabel>
            </Label>
            <Textarea
              id="exp-hypothesis"
              value={form.hypothesis}
              onChange={(e) => update("hypothesis", e.target.value)}
              placeholder="例如：均线交叉策略在 A 股大盘 ETF 上具有统计显著的超额收益"
              rows={3}
            />
            <p className="text-xs text-muted-foreground">
              {form.hypothesis.trim().length}/2000 字符
              {!hypothesisValid && form.hypothesis.length > 0 && (
                <span className="text-warning"> · 至少需要 10 个字符</span>
              )}
            </p>
          </div>

          <Separator />

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>策略类型</Label>
              <Select
                value={form.strategy_kind}
                onValueChange={(v) => update("strategy_kind", v)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {STRATEGY_KINDS.map((kind) => (
                    <SelectItem key={kind.value} value={kind.value}>
                      {kind.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>
                <HintLabel
                  hint={{
                    title: "验证模式",
                    description:
                      "rolling = 固定长度窗口向前滚动，旧数据逐步丢弃；expanding = 起点固定，终点前移，数据量递增。",
                  }}
                >
                  验证模式
                </HintLabel>
              </Label>
              <Select
                value={form.mode}
                onValueChange={(v) => update("mode", v)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {MODE_OPTIONS.map((opt) => (
                    <SelectItem key={opt.value} value={opt.value}>
                      {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <Separator />

          <div className="space-y-2">
            <p className="text-xs font-medium text-muted-foreground">
              时间区间划分
            </p>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div className="rounded-md border border-primary/20 bg-primary/5 p-2.5">
                <p className="text-xs font-medium text-primary">训练期</p>
                <div className="mt-1.5 grid grid-cols-2 gap-1.5">
                  <Input
                    type="date"
                    value={form.train_start}
                    onChange={(e) => update("train_start", e.target.value)}
                    className="h-8 text-xs"
                  />
                  <Input
                    type="date"
                    value={form.train_end}
                    onChange={(e) => update("train_end", e.target.value)}
                    className="h-8 text-xs"
                  />
                </div>
              </div>
              <div className="rounded-md border border-warning/20 bg-warning/5 p-2.5">
                <p className="text-xs font-medium text-warning">验证期</p>
                <div className="mt-1.5 grid grid-cols-2 gap-1.5">
                  <Input
                    type="date"
                    value={form.validation_start}
                    onChange={(e) =>
                      update("validation_start", e.target.value)
                    }
                    className="h-8 text-xs"
                  />
                  <Input
                    type="date"
                    value={form.validation_end}
                    onChange={(e) =>
                      update("validation_end", e.target.value)
                    }
                    className="h-8 text-xs"
                  />
                </div>
              </div>
              <div className="rounded-md border border-success/20 bg-success/5 p-2.5 sm:col-span-2">
                <p className="flex items-center gap-1 text-xs font-medium text-success">
                  测试期 (OOS)
                  <HintLabel hint={RESEARCH_HINTS.experiments.oos}>
                    {""}
                  </HintLabel>
                </p>
                <div className="mt-1.5 grid grid-cols-2 gap-1.5">
                  <Input
                    type="date"
                    value={form.test_start}
                    onChange={(e) => update("test_start", e.target.value)}
                    className="h-8 text-xs"
                  />
                  <Input
                    type="date"
                    value={form.test_end}
                    onChange={(e) => update("test_end", e.target.value)}
                    className="h-8 text-xs"
                  />
                </div>
              </div>
            </div>
          </div>

          <Separator />

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>
                <HintLabel
                  hint={{
                    title: "基准代码",
                    description:
                      "用于计算超额收益的基准标的。000300.SH = 沪深300指数。",
                  }}
                >
                  基准代码
                </HintLabel>
              </Label>
              <Input
                value={form.benchmark_symbol}
                onChange={(e) => update("benchmark_symbol", e.target.value)}
                placeholder="000300.SH"
              />
            </div>
            <div className="space-y-1.5">
              <Label>
                <HintLabel
                  hint={{
                    title: "试验次数预算",
                    description:
                      "参数搜索的最大试验次数。预算越大搜索越充分，但多重检验风险也越高。",
                  }}
                >
                  试验预算
                </HintLabel>
              </Label>
              <Input
                type="number"
                min={1}
                value={form.trial_budget}
                onChange={(e) => update("trial_budget", e.target.value)}
              />
            </div>
          </div>
        </div>

        {createMutation.isError && (
          <p className="text-sm text-destructive">
            {errorMessage(createMutation.error, "创建失败，请重试")}
          </p>
        )}

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={createMutation.isPending}
          >
            取消
          </Button>
          <Button
            disabled={!canSubmit}
            onClick={handleSubmit}
          >
            {createMutation.isPending ? "创建中…" : "创建实验"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function Experiments() {
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = React.useState<string>("all");
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [createOpen, setCreateOpen] = React.useState(false);
  const [rejectOpen, setRejectOpen] = React.useState(false);
  const [rejectReason, setRejectReason] = React.useState("");
  const [deleteOpen, setDeleteOpen] = React.useState(false);

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
    void queryClient.invalidateQueries({
      queryKey: ["validation-experiments"],
    });
  };

  const detail = detailQuery.data;

  return (
    <div>
      <PageHeader
        title="实验与 OOS"
        description="机器验证实验、样本外检验与过拟合防护"
        breadcrumbs={[
          { label: "研究", href: "/research" },
          { label: "实验与 OOS" },
        ]}
      />

      <WorkflowIndicator currentPath="/research/experiments" />

      <Alert variant="info" className="mb-4">
        <AlertTitle>验证实验 vs 回测 vs 模拟盘</AlertTitle>
        <AlertDescription>
          <div className="space-y-1.5">
            <p>
              <span className="font-medium text-foreground">验证实验</span>{" "}
              用于系统性检验策略是否有效。
            </p>
            <div className="grid grid-cols-1 gap-1 md:grid-cols-3">
              <p className="rounded bg-card/50 px-2 py-1">
                <span className="font-medium text-foreground">回测</span> = 单次运行看结果（回答"赚不赚钱"）
              </p>
              <p className="rounded bg-card/50 px-2 py-1">
                <span className="font-medium text-foreground">验证实验</span> = 多维度检验防过拟合（回答"是不是运气好"）
              </p>
              <p className="rounded bg-card/50 px-2 py-1">
                <span className="font-medium text-foreground">模拟盘</span> = 用纸面资金持续跟踪（回答"真实环境下还行不行"）
              </p>
            </div>
            <p className="text-xs leading-relaxed">
              <span className="font-medium text-foreground">OOS（Out-of-Sample）</span>
              = 用策略参数优化时未使用过的数据检验。如果只在训练数据上调参，策略容易过拟合
              —— 在训练集上表现极好但实盘会亏损。OOS 验证是防止自欺欺人的核心手段。
            </p>
          </div>
        </AlertDescription>
      </Alert>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-1">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between gap-2">
              <CardTitle className="text-base">
                <HintLabel hint={RESEARCH_HINTS.experiments.validation}>
                  实验列表
                </HintLabel>
              </CardTitle>
              <Button size="sm" onClick={() => setCreateOpen(true)}>
                <Plus className="h-4 w-4" />
                创建验证实验
              </Button>
            </div>
            <div className="mt-2 flex items-center gap-2">
              <Select value={statusFilter} onValueChange={setStatusFilter}>
                <SelectTrigger className="h-8 w-full text-xs">
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
              <Button
                variant="outline"
                size="icon"
                className="h-8 w-8 shrink-0"
                onClick={() => listQuery.refetch()}
                disabled={listQuery.isFetching}
              >
                <RefreshCw
                  className={cn(
                    "h-3.5 w-3.5",
                    listQuery.isFetching && "animate-spin",
                  )}
                />
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              {listQuery.data
                ? `共 ${listQuery.data.length} 个实验`
                : "加载中…"}
            </p>
          </CardHeader>
          <CardContent>
            {listQuery.isLoading ? (
              <LoadingState rows={5} />
            ) : listQuery.isError ? (
              <ErrorState
                message={errorMessage(listQuery.error, "无法加载实验列表")}
                onRetry={() => listQuery.refetch()}
              />
            ) : listQuery.data && listQuery.data.length > 0 ? (
              <ScrollArea className="h-[600px] pr-3">
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
                        <StatusBadge status={exp.status}>
                          {experimentStatusLabel(exp.status)}
                        </StatusBadge>
                        <span className="font-mono text-xs text-muted-foreground">
                          {exp.experiment_id}
                        </span>
                      </div>
                      <p className="mt-2 line-clamp-2 text-sm text-foreground">
                        {exp.hypothesis}
                      </p>
                      <div className="mt-1.5 flex items-center justify-between">
                        <span className="text-xs text-muted-foreground">
                          {timeAgo(exp.created_at)}
                        </span>
                        {exp.version_stamp && (
                          <Badge variant="outline" className="font-mono text-[10px]">
                            {versionStampText(exp.version_stamp)}
                          </Badge>
                        )}
                      </div>
                    </button>
                  ))}
                </div>
              </ScrollArea>
            ) : (
              <EmptyState
                icon={<FlaskConical className="h-8 w-8" />}
                title="暂无实验"
                description="点击右上角「创建验证实验」开始检验你的策略假设。"
                action={
                  <Button size="sm" onClick={() => setCreateOpen(true)}>
                    <Plus className="h-4 w-4" />
                    创建验证实验
                  </Button>
                }
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
                      {detail && (
                        <StatusBadge status={detail.status}>
                          {experimentStatusLabel(detail.status)}
                        </StatusBadge>
                      )}
                    </CardTitle>
                    {detail && (
                      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                        {detail.version_stamp && (
                          <Badge variant="outline" className="font-mono">
                            {versionStampText(detail.version_stamp)}
                          </Badge>
                        )}
                        <span>·</span>
                        <span>{formatDateTime(detail.created_at)}</span>
                        {detail.supersedes_id && (
                          <>
                            <span>·</span>
                            <span>
                              取代自{" "}
                              <span className="font-mono">
                                {detail.supersedes_id}
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
                    <X className="h-4 w-4" />
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
                ) : detail ? (
                  <ScrollArea className="h-[560px] pr-3">
                    <div className="space-y-5">
                      <div>
                        <p className="text-xs font-medium text-muted-foreground">
                          假设
                        </p>
                        <p className="mt-1 text-sm leading-relaxed text-foreground">
                          {detail.hypothesis}
                        </p>
                      </div>

                      {detail.notes && (
                        <div>
                          <p className="text-xs font-medium text-muted-foreground">
                            备注
                          </p>
                          <p className="mt-1 whitespace-pre-wrap text-sm text-muted-foreground">
                            {detail.notes}
                          </p>
                        </div>
                      )}

                      <Separator />

                      <div>
                        <p className="mb-2 flex items-center gap-1 text-xs font-medium text-muted-foreground">
                          验证计划 — 时间区间
                          <HintLabel hint={RESEARCH_HINTS.experiments.difference}>
                            {""}
                          </HintLabel>
                        </p>
                        <PlanTimeline plan={detail.plan} />
                        <div className="mt-3">
                          <PlanParams plan={detail.plan} />
                        </div>
                      </div>

                      <Separator />

                      <div>
                        <p className="mb-2 flex items-center gap-1 text-xs font-medium text-muted-foreground">
                          <HintLabel hint={RESEARCH_HINTS.experiments.thresholds}>
                            验收阈值
                          </HintLabel>
                        </p>
                        <ThresholdTable thresholds={detail.thresholds} />
                      </div>

                      {(Object.keys(detail.robustness).length > 0 ||
                        Object.keys(detail.strategy_params_space).length >
                          0) && (
                        <>
                          <Separator />
                          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                            <JsonBlock
                              label="稳健性配置"
                              value={detail.robustness}
                            />
                            <JsonBlock
                              label="策略参数空间"
                              value={detail.strategy_params_space}
                            />
                          </div>
                        </>
                      )}

                      <Separator />

                      <div>
                        <div className="mb-2 flex items-center justify-between">
                          <p className="text-xs font-medium text-muted-foreground">
                            试验记录（{detail.trials.length}）
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
                        {detail.trials.length > 0 ? (
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
                                {detail.trials.map((trial) => (
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
                            detail.status === "rejected" ||
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
              description="选中实验后将展示完整假设、验证计划时间线、验收阈值与试验记录。"
            />
          )}
        </div>
      </div>

      <NextStepCTA
        nextPath="/research/runs"
        nextLabel="研究运行"
        description="将通过验证的策略冻结为可复现的研究运行"
      />

      <CreateExperimentDialog open={createOpen} onOpenChange={setCreateOpen} />

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
              disabled={deleteMutation.isPending || !selectedId}
              onClick={() => selectedId && deleteMutation.mutate(selectedId)}
            >
              {deleteMutation.isPending ? "删除中…" : "确认删除"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
