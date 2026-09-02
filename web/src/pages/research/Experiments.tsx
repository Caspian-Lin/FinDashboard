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
import { useT, useLanguage, type LocalizedText } from "@/i18n";

const STATUS_OPTIONS: { value: string; label: LocalizedText }[] = [
  { value: "all", label: { zh: "全部", en: "All" } },
  { value: "draft", label: { zh: "草稿", en: "Draft" } },
  { value: "registered", label: { zh: "已注册", en: "Registered" } },
  { value: "running", label: { zh: "运行中", en: "Running" } },
  { value: "completed", label: { zh: "已完成", en: "Completed" } },
  { value: "failed", label: { zh: "失败", en: "Failed" } },
  { value: "rejected", label: { zh: "已拒绝", en: "Rejected" } },
];

const STRATEGY_KINDS: { value: string; label: LocalizedText }[] = [
  { value: "ma_cross", label: { zh: "均线交叉 (MA Cross)", en: "MA Cross" } },
  { value: "multi_factor", label: { zh: "多因子 (Multi-Factor)", en: "Multi-Factor" } },
  { value: "etf_rotation", label: { zh: "ETF 轮动 (ETF Rotation)", en: "ETF Rotation" } },
  { value: "mean_reversion", label: { zh: "均值回归 (Mean Reversion)", en: "Mean Reversion" } },
  { value: "convertible_double_low", label: { zh: "可转债双低 (Convertible Double-Low)", en: "Convertible Double-Low" } },
  { value: "futures_tsmom", label: { zh: "期货动量 (Futures TSMOM)", en: "Futures TSMOM" } },
];

const MODE_OPTIONS: { value: string; label: LocalizedText; desc: LocalizedText }[] = [
  {
    value: "rolling",
    label: { zh: "滚动窗口 (Rolling)", en: "Rolling window" },
    desc: {
      zh: "固定长度窗口向前滚动，旧数据逐步丢弃",
      en: "A fixed-length window rolls forward, dropping older data step by step",
    },
  },
  {
    value: "expanding",
    label: { zh: "扩展窗口 (Expanding)", en: "Expanding window" },
    desc: {
      zh: "起点固定，终点逐步前移，数据量递增",
      en: "Fixed start with the end moving forward, growing the data over time",
    },
  },
];

interface HintShape {
  title: LocalizedText;
  description: LocalizedText;
  detail?: LocalizedText;
}

const THRESHOLD_META: { key: string; label: LocalizedText; hint: HintShape }[] = [
  {
    key: "min_oos_sharpe",
    label: { zh: "OOS 夏普下限", en: "Min OOS Sharpe" },
    hint: {
      title: {
        zh: "min_oos_sharpe（样本外夏普比率下限）",
        en: "min_oos_sharpe (minimum out-of-sample Sharpe ratio)",
      },
      description: {
        zh: "夏普比率衡量风险调整后收益，>0.5 表示每承担 1 单位风险获得 0.5 单位超额收益。",
        en: "The Sharpe ratio measures risk-adjusted return; >0.5 means 0.5 units of excess return per unit of risk taken.",
      },
      detail: {
        zh: "默认 0.5。值越高要求越严格。",
        en: "Default 0.5. Higher values are stricter.",
      },
    },
  },
  {
    key: "max_oos_drawdown",
    label: { zh: "OOS 最大回撤上限", en: "Max OOS drawdown" },
    hint: {
      title: {
        zh: "max_oos_drawdown（样本外最大回撤上限）",
        en: "max_oos_drawdown (maximum out-of-sample drawdown)",
      },
      description: {
        zh: "回撤是从历史最高点到最低点的跌幅。25% 意味着最多允许亏 25%。",
        en: "Drawdown is the peak-to-trough decline. 25% means a loss of at most 25% is allowed.",
      },
      detail: {
        zh: "默认 0.25。值越低要求越严格。",
        en: "Default 0.25. Lower values are stricter.",
      },
    },
  },
  {
    key: "max_pbo",
    label: { zh: "过拟合概率上限", en: "Max overfitting probability" },
    hint: {
      title: {
        zh: "max_pbo（回测过拟合概率上限）",
        en: "max_pbo (maximum probability of backtest overfitting)",
      },
      description: {
        zh: "PBO = Probability of Backtest Overfitting。>50% 说明策略大概率是过拟合的。",
        en: "PBO = Probability of Backtest Overfitting. >50% means the strategy is very likely overfitted.",
      },
      detail: {
        zh: "默认 0.5。越低越严格。",
        en: "Default 0.5. Lower is stricter.",
      },
    },
  },
  {
    key: "min_deflated_sharpe",
    label: { zh: "Deflated Sharpe 下限", en: "Min Deflated Sharpe" },
    hint: {
      title: {
        zh: "min_deflated_sharpe（Deflated Sharpe Ratio 下限）",
        en: "min_deflated_sharpe (minimum Deflated Sharpe Ratio)",
      },
      description: {
        zh: "对夏普比率进行多重检验校正后的值，消除'试了很多参数碰巧有一个好'的偏差。",
        en: "The Sharpe ratio after multiple-testing correction, removing the bias of trying many parameter sets until one happens to look good.",
      },
      detail: {
        zh: "默认 0.0。>0 表示校正后仍有正超额收益。",
        en: "Default 0.0. >0 means positive excess return survives the correction.",
      },
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

function versionStampText(
  value: string | Record<string, unknown>,
  lang: "zh" | "en",
): string {
  if (typeof value === "string") return value;
  const labels: Record<string, LocalizedText> = {
    matching_model_version: { zh: "撮合", en: "Matching" },
    asset_rules_version: { zh: "资产规则", en: "Asset rules" },
    factor_version: { zh: "因子", en: "Factors" },
    strategy_kind: { zh: "策略", en: "Strategy" },
  };
  const valueText = (item: unknown): string => {
    if (item && typeof item === "object") {
      const size = Object.keys(item as Record<string, unknown>).length;
      return lang === "en" ? `${size} frozen` : `${size} 项已冻结`;
    }
    return String(item);
  };
  const parts = Object.entries(value)
    .filter(([, item]) => item !== null && item !== undefined && item !== "")
    .map(([key, item]) =>
      `${labels[key]?.[lang] ?? key} ${valueText(item)}`,
    );
  return parts.length > 0
    ? parts.join(" · ")
    : lang === "en"
      ? "Version info pending"
      : "版本信息待补充";
}

function experimentStatusLabel(status: string, lang: "zh" | "en"): string {
  const labels: Record<string, LocalizedText> = {
    hypothesis: { zh: "假设已冻结", en: "Hypothesis frozen" },
    draft: { zh: "草稿", en: "Draft" },
    registered: { zh: "已注册", en: "Registered" },
    running: { zh: "运行中", en: "Running" },
    completed: { zh: "已完成", en: "Completed" },
    failed: { zh: "失败", en: "Failed" },
    rejected: { zh: "已拒绝", en: "Rejected" },
  };
  return labels[status]?.[lang] ?? status;
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
  const { tl } = useT();
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

  const segments: { label: LocalizedText; range: string; flex: number; color: string; track: string }[] = [
    {
      label: { zh: "训练期", en: "Train" },
      range: `${ts} → ${te}`,
      flex: trainDays / total,
      color: "bg-primary/60",
      track: "bg-primary/15",
    },
    {
      label: { zh: "验证期", en: "Validation" },
      range: `${vs} → ${ve}`,
      flex: valDays / total,
      color: "bg-warning/60",
      track: "bg-warning/15",
    },
    {
      label: { zh: "测试期 (OOS)", en: "Test (OOS)" },
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
            key={seg.range}
            className={cn("border-r border-card last:border-r-0", seg.color)}
            style={{ flexGrow: seg.flex }}
            title={`${tl(seg.label)}: ${seg.range}`}
          />
        ))}
      </div>
      <div className="grid grid-cols-3 gap-2">
        {segments.map((seg) => (
          <div
            key={seg.range}
            className={cn("rounded-md border p-2", seg.track)}
          >
            <div className="flex items-center gap-1.5">
              <span className={cn("h-2 w-2 rounded-full", seg.color)} />
              <span className="text-xs font-medium text-foreground">
                {tl(seg.label)}
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
  const { tl } = useT();
  const cells: { key: string; label: LocalizedText; hint?: HintShape }[] = [
    {
      key: "mode",
      label: { zh: "模式", en: "Mode" },
      hint: {
        title: { zh: "验证模式", en: "Validation mode" },
        description: {
          zh: "rolling = 固定长度窗口向前滚动；expanding = 起点固定，终点前移。",
          en: "rolling = a fixed-length window rolls forward; expanding = fixed start, the end moves forward.",
        },
      },
    },
    { key: "train_window_days", label: { zh: "训练窗口(天)", en: "Train window (days)" } },
    { key: "test_window_days", label: { zh: "测试窗口(天)", en: "Test window (days)" } },
    { key: "step_days", label: { zh: "滚动步长(天)", en: "Step size (days)" } },
    {
      key: "trial_budget",
      label: { zh: "试验预算", en: "Trial budget" },
      hint: {
        title: { zh: "试验次数预算", en: "Trial count budget" },
        description: {
          zh: "参数搜索的最大试验次数。预算越大搜索越充分，但多重检验风险也越高。",
          en: "Maximum number of trials in the parameter search. A larger budget explores more thoroughly but raises multiple-testing risk.",
        },
      },
    },
    {
      key: "benchmark_symbol",
      label: { zh: "基准", en: "Benchmark" },
      hint: {
        title: { zh: "基准代码", en: "Benchmark symbol" },
        description: {
          zh: "用于计算超额收益的基准标的。000300.SH = 沪深300指数。",
          en: "Benchmark used to compute excess returns. 000300.SH = CSI 300 index.",
        },
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
            <span className="text-xs text-muted-foreground">{tl(cell.label)}</span>
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
  const { tl } = useT();
  const knownKeys = new Set(THRESHOLD_META.map((m) => m.key));
  const extraKeys = Object.keys(thresholds).filter((k) => !knownKeys.has(k));

  return (
    <div className="rounded-lg border border-border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-1/2">{tl({ zh: "阈值", en: "Threshold" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "值", en: "Value" })}</TableHead>
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
                  <HintLabel hint={meta.hint}>{tl(meta.label)}</HintLabel>
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
  const { tl } = useT();
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
          <DialogTitle>{tl({ zh: "创建验证实验", en: "Create validation experiment" })}</DialogTitle>
          <DialogDescription>
            {tl({
              zh: "定义策略假设、验证计划和 OOS 测试区间。提交后进入 draft 状态，可在实验详情中拒绝或删除。",
              en: "Define the strategy hypothesis, validation plan and OOS test window. After submission the experiment enters draft status and can be rejected or deleted from the experiment detail view.",
            })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="exp-hypothesis">
              <HintLabel
                hint={{
                  title: { zh: "策略假设", en: "Strategy hypothesis" },
                  description: {
                    zh: "用一句话描述你要验证的策略假设。需可证伪 —— 明确在什么条件下假设不成立。",
                    en: "Describe the strategy hypothesis you want to test in one sentence. It must be falsifiable — state explicitly under what conditions it fails.",
                  },
                  detail: {
                    zh: "最少 10 个字符，最多 2000 个字符。",
                    en: "Between 10 and 2000 characters.",
                  },
                }}
              >
                {tl({ zh: "策略假设", en: "Strategy hypothesis" })}
              </HintLabel>
            </Label>
            <Textarea
              id="exp-hypothesis"
              value={form.hypothesis}
              onChange={(e) => update("hypothesis", e.target.value)}
              placeholder={tl({
                zh: "例如：均线交叉策略在 A 股大盘 ETF 上具有统计显著的超额收益",
                en: "e.g. The MA cross strategy has statistically significant excess returns on A-share broad-market ETFs",
              })}
              rows={3}
            />
            <p className="text-xs text-muted-foreground">
              {form.hypothesis.trim().length}/2000 {tl({ zh: "字符", en: "characters" })}
              {!hypothesisValid && form.hypothesis.length > 0 && (
                <span className="text-warning">
                  {tl({ zh: " · 至少需要 10 个字符", en: " · At least 10 characters required" })}
                </span>
              )}
            </p>
          </div>

          <Separator />

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>{tl({ zh: "策略类型", en: "Strategy type" })}</Label>
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
                      {tl(kind.label)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>
                <HintLabel
                  hint={{
                    title: { zh: "验证模式", en: "Validation mode" },
                    description: {
                      zh: "rolling = 固定长度窗口向前滚动，旧数据逐步丢弃；expanding = 起点固定，终点前移，数据量递增。",
                      en: "rolling = a fixed-length window rolls forward, dropping old data step by step; expanding = fixed start, the end moves forward and data grows.",
                    },
                  }}
                >
                  {tl({ zh: "验证模式", en: "Validation mode" })}
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
                      {tl(opt.label)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <Separator />

          <div className="space-y-2">
            <p className="text-xs font-medium text-muted-foreground">
              {tl({ zh: "时间区间划分", en: "Time window breakdown" })}
            </p>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div className="rounded-md border border-primary/20 bg-primary/5 p-2.5">
                <p className="text-xs font-medium text-primary">{tl({ zh: "训练期", en: "Train" })}</p>
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
                <p className="text-xs font-medium text-warning">{tl({ zh: "验证期", en: "Validation" })}</p>
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
                  {tl({ zh: "测试期 (OOS)", en: "Test (OOS)" })}
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
                    title: { zh: "基准代码", en: "Benchmark symbol" },
                    description: {
                      zh: "用于计算超额收益的基准标的。000300.SH = 沪深300指数。",
                      en: "Benchmark used to compute excess returns. 000300.SH = CSI 300 index.",
                    },
                  }}
                >
                  {tl({ zh: "基准代码", en: "Benchmark symbol" })}
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
                    title: { zh: "试验次数预算", en: "Trial count budget" },
                    description: {
                      zh: "参数搜索的最大试验次数。预算越大搜索越充分，但多重检验风险也越高。",
                      en: "Maximum number of trials in the parameter search. A larger budget explores more thoroughly but raises multiple-testing risk.",
                    },
                  }}
                >
                  {tl({ zh: "试验预算", en: "Trial budget" })}
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
            {errorMessage(createMutation.error, tl({ zh: "创建失败，请重试", en: "Creation failed. Please retry." }))}
          </p>
        )}

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={createMutation.isPending}
          >
            {tl({ zh: "取消", en: "Cancel" })}
          </Button>
          <Button
            disabled={!canSubmit}
            onClick={handleSubmit}
          >
            {createMutation.isPending
              ? tl({ zh: "创建中…", en: "Creating…" })
              : tl({ zh: "创建实验", en: "Create experiment" })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function Experiments() {
  const { tl } = useT();
  const { lang } = useLanguage();
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
        title={tl({ zh: "实验与 OOS", en: "Experiments & OOS" })}
        description={tl({
          zh: "机器验证实验、样本外检验与过拟合防护",
          en: "Machine validation experiments, out-of-sample checks and overfitting protection",
        })}
        breadcrumbs={[
          { label: tl({ zh: "研究", en: "Research" }), href: "/research" },
          { label: tl({ zh: "实验与 OOS", en: "Experiments & OOS" }) },
        ]}
      />

      <WorkflowIndicator currentPath="/research/experiments" />

      <Alert variant="info" className="mb-4">
        <AlertTitle>{tl({ zh: "验证实验 vs 回测 vs 模拟盘", en: "Validation experiment vs backtest vs simulation" })}</AlertTitle>
        <AlertDescription>
          <div className="space-y-1.5">
            <p>
              <span className="font-medium text-foreground">
                {tl({ zh: "验证实验", en: "Validation experiment" })}
              </span>{" "}
              {tl({
                zh: "用于系统性检验策略是否有效。",
                en: "systematically checks whether a strategy actually works.",
              })}
            </p>
            <div className="grid grid-cols-1 gap-1 md:grid-cols-3">
              <p className="rounded bg-card/50 px-2 py-1">
                <span className="font-medium text-foreground">{tl({ zh: "回测", en: "Backtest" })}</span>{" "}
                {tl({
                  zh: '= 单次运行看结果（回答"赚不赚钱"）',
                  en: '= one run to see the result (answers “is it profitable”)',
                })}
              </p>
              <p className="rounded bg-card/50 px-2 py-1">
                <span className="font-medium text-foreground">{tl({ zh: "验证实验", en: "Validation experiment" })}</span>{" "}
                {tl({
                  zh: '= 多维度检验防过拟合（回答"是不是运气好"）',
                  en: '= multi-dimensional checks against overfitting (answers “was it luck”)',
                })}
              </p>
              <p className="rounded bg-card/50 px-2 py-1">
                <span className="font-medium text-foreground">{tl({ zh: "模拟盘", en: "Simulation" })}</span>{" "}
                {tl({
                  zh: '= 用纸面资金持续跟踪（回答"真实环境下还行不行"）',
                  en: '= continuous tracking with paper money (answers “does it hold up in a real environment”)',
                })}
              </p>
            </div>
            <p className="text-xs leading-relaxed">
              <span className="font-medium text-foreground">
                {tl({ zh: "OOS（Out-of-Sample）", en: "OOS (Out-of-Sample)" })}
              </span>
              {tl({
                zh: "= 用策略参数优化时未使用过的数据检验。如果只在训练数据上调参，策略容易过拟合 —— 在训练集上表现极好但实盘会亏损。OOS 验证是防止自欺欺人的核心手段。",
                en: "= testing on data never used during parameter optimization. If you only tune on training data, the strategy easily overfits — it looks great on the training set but loses money live. OOS validation is the core defense against self-deception.",
              })}
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
                  {tl({ zh: "实验列表", en: "Experiment list" })}
                </HintLabel>
              </CardTitle>
              <Button size="sm" onClick={() => setCreateOpen(true)}>
                <Plus className="h-4 w-4" />
                {tl({ zh: "创建验证实验", en: "Create validation experiment" })}
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
                      {tl(opt.label)}
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
                ? tl({
                    zh: `共 ${listQuery.data.length} 个实验`,
                    en: `${listQuery.data.length} experiments`,
                  })
                : tl({ zh: "加载中…", en: "Loading…" })}
            </p>
          </CardHeader>
          <CardContent>
            {listQuery.isLoading ? (
              <LoadingState rows={5} />
            ) : listQuery.isError ? (
              <ErrorState
                message={errorMessage(
                  listQuery.error,
                  tl({ zh: "无法加载实验列表", en: "Failed to load experiment list" }),
                )}
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
                          {experimentStatusLabel(exp.status, lang)}
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
                          {timeAgo(exp.created_at, lang)}
                        </span>
                        {exp.version_stamp && (
                          <Badge variant="outline" className="font-mono text-[10px]">
                            {versionStampText(exp.version_stamp, lang)}
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
                title={tl({ zh: "暂无实验", en: "No experiments yet" })}
                description={tl({
                  zh: "点击右上角「创建验证实验」开始检验你的策略假设。",
                  en: 'Click “Create validation experiment” in the top right to start testing your strategy hypothesis.',
                })}
                action={
                  <Button size="sm" onClick={() => setCreateOpen(true)}>
                    <Plus className="h-4 w-4" />
                    {tl({ zh: "创建验证实验", en: "Create validation experiment" })}
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
                          {experimentStatusLabel(detail.status, lang)}
                        </StatusBadge>
                      )}
                    </CardTitle>
                    {detail && (
                      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                        {detail.version_stamp && (
                          <Badge variant="outline" className="font-mono">
                            {versionStampText(detail.version_stamp, lang)}
                          </Badge>
                        )}
                        <span>·</span>
                        <span>{formatDateTime(detail.created_at)}</span>
                        {detail.supersedes_id && (
                          <>
                            <span>·</span>
                            <span>
                              {tl({ zh: "取代自", en: "Supersedes" })}{" "}
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
                    aria-label={tl({ zh: "取消选择", en: "Clear selection" })}
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
                      tl({ zh: "无法加载实验详情", en: "Failed to load experiment details" }),
                    )}
                    onRetry={() => detailQuery.refetch()}
                  />
                ) : detail ? (
                  <ScrollArea className="h-[560px] pr-3">
                    <div className="space-y-5">
                      <div>
                        <p className="text-xs font-medium text-muted-foreground">
                          {tl({ zh: "假设", en: "Hypothesis" })}
                        </p>
                        <p className="mt-1 text-sm leading-relaxed text-foreground">
                          {detail.hypothesis}
                        </p>
                      </div>

                      {detail.notes && (
                        <div>
                          <p className="text-xs font-medium text-muted-foreground">
                            {tl({ zh: "备注", en: "Notes" })}
                          </p>
                          <p className="mt-1 whitespace-pre-wrap text-sm text-muted-foreground">
                            {detail.notes}
                          </p>
                        </div>
                      )}

                      <Separator />

                      <div>
                        <p className="mb-2 flex items-center gap-1 text-xs font-medium text-muted-foreground">
                          {tl({ zh: "验证计划 — 时间区间", en: "Validation plan — time windows" })}
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
                            {tl({ zh: "验收阈值", en: "Acceptance thresholds" })}
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
                              label={tl({ zh: "稳健性配置", en: "Robustness config" })}
                              value={detail.robustness}
                            />
                            <JsonBlock
                              label={tl({ zh: "策略参数空间", en: "Strategy parameter space" })}
                              value={detail.strategy_params_space}
                            />
                          </div>
                        </>
                      )}

                      <Separator />

                      <div>
                        <div className="mb-2 flex items-center justify-between">
                          <p className="text-xs font-medium text-muted-foreground">
                            {tl({
                              zh: `试验记录（${detail.trials.length}）`,
                              en: `Trial records (${detail.trials.length})`,
                            })}
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
                                  <TableHead>{tl({ zh: "试验 ID", en: "Trial ID" })}</TableHead>
                                  <TableHead>{tl({ zh: "状态", en: "Status" })}</TableHead>
                                  <TableHead>{tl({ zh: "失败原因", en: "Failure reason" })}</TableHead>
                                  <TableHead>{tl({ zh: "创建时间", en: "Created" })}</TableHead>
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
                            title={tl({ zh: "暂无试验", en: "No trials yet" })}
                            description={tl({
                              zh: "该实验尚未登记任何试验记录。",
                              en: "No trial records have been registered for this experiment yet.",
                            })}
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
                          {tl({ zh: "刷新数据", en: "Refresh data" })}
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
                          {tl({ zh: "拒绝实验", en: "Reject experiment" })}
                        </Button>
                        <Button
                          variant="destructive"
                          size="sm"
                          disabled={deleteMutation.isPending}
                          onClick={() => setDeleteOpen(true)}
                        >
                          <Trash2 className="h-4 w-4" />
                          {tl({ zh: "删除实验", en: "Delete experiment" })}
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
              title={tl({ zh: "请从左侧选择一个实验", en: "Select an experiment from the left" })}
              description={tl({
                zh: "选中实验后将展示完整假设、验证计划时间线、验收阈值与试验记录。",
                en: "Once selected, the full hypothesis, validation plan timeline, acceptance thresholds and trial records will be shown.",
              })}
            />
          )}
        </div>
      </div>

      <NextStepCTA
        nextPath="/research/runs"
        nextLabel={{ zh: "研究运行", en: "Research runs" }}
        description={{
          zh: "将通过验证的策略冻结为可复现的研究运行",
          en: "Freeze the validated strategy into a reproducible research run",
        }}
      />

      <CreateExperimentDialog open={createOpen} onOpenChange={setCreateOpen} />

      <Dialog open={rejectOpen} onOpenChange={setRejectOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{tl({ zh: "拒绝实验", en: "Reject experiment" })}</DialogTitle>
            <DialogDescription>
              {tl({
                zh: "拒绝后该实验将标记为 rejected，无法继续注册试验。请填写拒绝原因以便审计追溯。",
                en: "Once rejected, the experiment is marked rejected and can no longer register trials. Please provide a rejection reason for audit traceability.",
              })}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="reject-reason">{tl({ zh: "拒绝原因", en: "Rejection reason" })}</Label>
            <Textarea
              id="reject-reason"
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              placeholder={tl({
                zh: "例如：样本外夏普未达阈值 / 数据泄漏 / 参数过拟合...",
                en: "e.g. OOS Sharpe below threshold / data leakage / parameter overfitting...",
              })}
              rows={4}
            />
          </div>
          {rejectMutation.isError && (
            <p className="text-sm text-destructive">
              {errorMessage(rejectMutation.error, tl({ zh: "拒绝失败，请重试", en: "Rejection failed. Please retry." }))}
            </p>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setRejectOpen(false)}
              disabled={rejectMutation.isPending}
            >
              {tl({ zh: "取消", en: "Cancel" })}
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
              {rejectMutation.isPending
                ? tl({ zh: "提交中…", en: "Submitting…" })
                : tl({ zh: "确认拒绝", en: "Confirm rejection" })}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{tl({ zh: "删除实验", en: "Delete experiment" })}</DialogTitle>
            <DialogDescription>
              {tl({
                zh: "该操作不可撤销，将永久删除实验及其试验记录。请确认是否继续。",
                en: "This action cannot be undone and permanently deletes the experiment and its trial records. Please confirm to continue.",
              })}
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-md border border-border bg-muted/30 p-3">
            <p className="text-xs text-muted-foreground">{tl({ zh: "目标实验", en: "Target experiment" })}</p>
            <p className="mt-1 font-mono text-sm">{selectedId ?? "—"}</p>
          </div>
          {deleteMutation.isError && (
            <p className="text-sm text-destructive">
              {errorMessage(deleteMutation.error, tl({ zh: "删除失败，请重试", en: "Deletion failed. Please retry." }))}
            </p>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setDeleteOpen(false)}
              disabled={deleteMutation.isPending}
            >
              {tl({ zh: "取消", en: "Cancel" })}
            </Button>
            <Button
              variant="destructive"
              disabled={deleteMutation.isPending || !selectedId}
              onClick={() => selectedId && deleteMutation.mutate(selectedId)}
            >
              {deleteMutation.isPending
                ? tl({ zh: "删除中…", en: "Deleting…" })
                : tl({ zh: "确认删除", en: "Confirm delete" })}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
