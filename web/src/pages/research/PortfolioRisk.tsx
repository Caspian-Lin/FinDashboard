import { WorkflowIndicator, NextStepCTA } from "@/components/research/ResearchHint";
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  PieChart,
  Pie,
  Cell,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  Legend,
} from "recharts";
import {
  Plus,
  Trash2,
  Calculator,
  Coins,
  Layers,
  PieChart as PieChartIcon,
  ShieldCheck,
  ArrowRight,
  RefreshCw,
} from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Separator } from "@/components/ui/separator";
import { Progress } from "@/components/ui/progress";
import {
  Tabs,
  TabsList,
  TabsTrigger,
  TabsContent,
} from "@/components/ui/tabs";
import {
  Select,
  SelectTrigger,
  SelectContent,
  SelectItem,
  SelectValue,
} from "@/components/ui/select";
import { EmptyState, ErrorState } from "@/components/ui/states";
import {
  portfolioApi,
  type AllocationMethod,
  type AllocateRequest,
  type AllocateResponse,
  type SizingRequest,
  type TierFeasibility,
} from "@/lib/portfolio";
import { cn, formatCurrency, formatPercent, formatNumber } from "@/lib/utils";
import { useT } from "@/i18n";

type SignalRow = { symbol: string; score: string; direction: "long" | "short" };
type LotRow = { symbol: string; lot_size: string; price: string };

// 图表调色板统一走主题 token(--chart-1..8),随深浅主题切换
const PALETTE = [
  "hsl(var(--chart-1))",
  "hsl(var(--chart-2))",
  "hsl(var(--chart-3))",
  "hsl(var(--chart-4))",
  "hsl(var(--chart-5))",
  "hsl(var(--chart-6))",
  "hsl(var(--chart-7))",
  "hsl(var(--chart-8))",
];

const fmtPct = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || Number.isNaN(v)
    ? "—"
    : `${(v * 100).toFixed(digits)}%`;

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between py-1.5">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="font-mono text-sm tabular-nums text-foreground">
        {value}
      </span>
    </div>
  );
}

function parseNum(v: string): number | undefined {
  const t = v.trim();
  if (t === "") return undefined;
  const n = Number(t);
  return Number.isNaN(n) ? undefined : n;
}

function riskRowColor(ratio: number): string {
  if (ratio >= 0.8) return "bg-destructive";
  if (ratio >= 0.5) return "bg-warning";
  return "bg-success";
}

function AllocationTab({
  onAllocated,
}: {
  onAllocated: (r: AllocateResponse) => void;
}) {
  const { tl } = useT();
  const [signals, setSignals] = useState<SignalRow[]>([
    { symbol: "", score: "", direction: "long" },
  ]);
  const [method, setMethod] = useState<AllocationMethod>("equal_weight");
  const [maxWeight, setMaxWeight] = useState("");
  const [minCash, setMinCash] = useState("");
  const [maxLev, setMaxLev] = useState("");
  const [longOnly, setLongOnly] = useState(true);

  const mutate = useMutation({
    mutationFn: (body: AllocateRequest) => portfolioApi.allocate(body),
    onSuccess: onAllocated,
  });

  const parsedSignals = signals
    .filter(
      (s) =>
        s.symbol.trim() !== "" &&
        s.score.trim() !== "" &&
        !Number.isNaN(Number(s.score)),
    )
    .map((s) => ({
      symbol: s.symbol.trim(),
      score: Number(s.score),
      direction: s.direction,
    }));

  const updateSignal = (i: number, patch: Partial<SignalRow>) =>
    setSignals((prev) =>
      prev.map((s, idx) => (idx === i ? { ...s, ...patch } : s)),
    );
  const addSignal = () =>
    setSignals((prev) => [
      ...prev,
      { symbol: "", score: "", direction: "long" },
    ]);
  const removeSignal = (i: number) =>
    setSignals((prev) => prev.filter((_, idx) => idx !== i));

  const handleCompute = () => {
    if (parsedSignals.length === 0) return;
    const body: AllocateRequest = {
      signals: parsedSignals,
      method,
      long_only: longOnly,
      max_weight_per_asset: parseNum(maxWeight),
      min_cash_buffer: parseNum(minCash),
      max_leverage: parseNum(maxLev),
    };
    mutate.mutate(body);
  };

  const data = mutate.data;
  const pieData = (data?.weights ?? [])
    .filter((w) => w.weight > 0)
    .map((w) => ({ symbol: w.symbol, weight: w.weight }));
  const risk = data?.risk;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <PieChartIcon className="h-4 w-4 text-primary" />
            {tl({ zh: "信号与约束", en: "Signals & Constraints" })}
          </CardTitle>
          <CardDescription>
            {tl({
              zh: "输入标的信号得分与组合约束,生成目标权重分配与风险报告。",
              en: "Enter instrument signal scores and portfolio constraints to generate target weight allocation and a risk report.",
            })}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>{tl({ zh: "信号输入", en: "Signal input" })}</Label>
              <Button variant="outline" size="sm" onClick={addSignal}>
                <Plus className="h-4 w-4" />
                {tl({ zh: "添加信号", en: "Add signal" })}
              </Button>
            </div>
            <div className="space-y-2">
              {signals.map((s, i) => (
                <div
                  key={i}
                  className="flex items-center gap-2"
                >
                  <Input
                    placeholder={tl({ zh: "标的代码", en: "Symbol" })}
                    value={s.symbol}
                    onChange={(e) =>
                      updateSignal(i, { symbol: e.target.value })
                    }
                    className="font-mono"
                  />
                  <Input
                    type="number"
                    placeholder={tl({ zh: "得分", en: "Score" })}
                    value={s.score}
                    onChange={(e) =>
                      updateSignal(i, { score: e.target.value })
                    }
                    className="w-28 tabular-nums"
                  />
                  <Select
                    value={s.direction}
                    onValueChange={(v) =>
                      updateSignal(i, {
                        direction: v === "short" ? "short" : "long",
                      })
                    }
                  >
                    <SelectTrigger className="w-28">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="long">
                        {tl({ zh: "多头", en: "Long" })}
                      </SelectItem>
                      <SelectItem value="short">
                        {tl({ zh: "空头", en: "Short" })}
                      </SelectItem>
                    </SelectContent>
                  </Select>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => removeSignal(i)}
                    disabled={signals.length === 1}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              ))}
            </div>
          </div>

          <Separator />

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
            <div className="space-y-1.5">
              <Label>{tl({ zh: "分配方法", en: "Allocation method" })}</Label>
              <Select
                value={method}
                onValueChange={(v) => setMethod(v as AllocationMethod)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="equal_weight">
                    {tl({ zh: "等权重", en: "Equal weight" })}
                  </SelectItem>
                  <SelectItem value="inverse_volatility">
                    {tl({ zh: "反波动率", en: "Inverse volatility" })}
                  </SelectItem>
                  <SelectItem value="erc">
                    {tl({ zh: "风险平价 (ERC)", en: "Risk parity (ERC)" })}
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>{tl({ zh: "单资产权重上限", en: "Max weight per asset" })}</Label>
              <Input
                type="number"
                placeholder={tl({ zh: "如 0.2", en: "e.g. 0.2" })}
                value={maxWeight}
                onChange={(e) => setMaxWeight(e.target.value)}
                className="tabular-nums"
              />
            </div>
            <div className="space-y-1.5">
              <Label>{tl({ zh: "最低现金缓冲", en: "Min cash buffer" })}</Label>
              <Input
                type="number"
                placeholder={tl({ zh: "如 0.05", en: "e.g. 0.05" })}
                value={minCash}
                onChange={(e) => setMinCash(e.target.value)}
                className="tabular-nums"
              />
            </div>
            <div className="space-y-1.5">
              <Label>{tl({ zh: "最大杠杆", en: "Max leverage" })}</Label>
              <Input
                type="number"
                placeholder={tl({ zh: "如 1.0", en: "e.g. 1.0" })}
                value={maxLev}
                onChange={(e) => setMaxLev(e.target.value)}
                className="tabular-nums"
              />
            </div>
          </div>

          <div className="flex items-center justify-between rounded-md border border-border px-3 py-2.5">
            <div>
              <Label>{tl({ zh: "仅做多", en: "Long-only" })}</Label>
              <p className="text-xs text-muted-foreground">
                {tl({
                  zh: "关闭后允许空头信号产生负权重",
                  en: "When off, short signals may produce negative weights",
                })}
              </p>
            </div>
            <Switch checked={longOnly} onCheckedChange={setLongOnly} />
          </div>

          <div className="flex items-center gap-3">
            <Button
              onClick={handleCompute}
              disabled={parsedSignals.length === 0 || mutate.isPending}
            >
              <Calculator className="h-4 w-4" />
              {mutate.isPending
                ? tl({ zh: "计算中…", en: "Computing…" })
                : tl({ zh: "计算目标权重", en: "Compute target weights" })}
            </Button>
            {parsedSignals.length === 0 && (
              <span className="text-xs text-muted-foreground">
                {tl({
                  zh: "请至少填写一行有效的标的与得分",
                  en: "Enter at least one valid symbol and score",
                })}
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {mutate.isError && (
        <ErrorState
          title={tl({ zh: "分配计算失败", en: "Allocation failed" })}
          message={
            mutate.error instanceof Error
              ? mutate.error.message
              : tl({
                  zh: "无法计算目标权重分配",
                  en: "Unable to compute target weight allocation",
                })
          }
        />
      )}

      {data && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">
                {tl({ zh: "目标权重分布", en: "Target weight distribution" })}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {pieData.length > 0 ? (
                <div className="rounded-md bg-muted p-3">
                  <ResponsiveContainer width="100%" height={260}>
                    <PieChart>
                      <Pie
                        data={pieData}
                        dataKey="weight"
                        nameKey="symbol"
                        cx="50%"
                        cy="50%"
                        outerRadius={92}
                        innerRadius={48}
                        paddingAngle={2}
                        stroke="hsl(var(--background))"
                        strokeWidth={1}
                      >
                        {pieData.map((entry, idx) => (
                          <Cell
                            key={entry.symbol}
                            fill={PALETTE[idx % PALETTE.length]}
                          />
                        ))}
                      </Pie>
                      <RechartsTooltip
                        contentStyle={{
                          backgroundColor: "hsl(var(--popover))",
                          border: "1px solid hsl(var(--border))",
                          borderRadius: 8,
                          color: "hsl(var(--popover-foreground))",
                          fontSize: 12,
                        }}
                        itemStyle={{ color: "hsl(var(--popover-foreground))" }}
                        labelStyle={{ color: "hsl(var(--muted-foreground))" }}
                        formatter={(value, name) => [
                          fmtPct(Number(value)),
                          String(name),
                        ]}
                      />
                      <Legend
                        wrapperStyle={{ fontSize: 12, color: "hsl(var(--muted-foreground))" }}
                        formatter={(value) => (
                          <span style={{ color: "hsl(var(--foreground))" }}>
                            {String(value)}
                          </span>
                        )}
                      />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <EmptyState
                  icon={<PieChartIcon className="h-8 w-8" />}
                  title={tl({ zh: "无有效权重", en: "No valid weights" })}
                  description={tl({
                    zh: "所有权重均为零或负值。",
                    en: "All weights are zero or negative.",
                  })}
                />
              )}

              <div className="rounded-md border border-border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{tl({ zh: "标的", en: "Symbol" })}</TableHead>
                      <TableHead className="text-right">
                        {tl({ zh: "权重", en: "Weight" })}
                      </TableHead>
                      <TableHead className="text-right">
                        {tl({ zh: "信号得分", en: "Signal score" })}
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.weights.map((w) => (
                      <TableRow key={w.symbol}>
                        <TableCell className="font-mono">{w.symbol}</TableCell>
                        <TableCell className="text-right font-mono tabular-nums">
                          {fmtPct(w.weight)}
                        </TableCell>
                        <TableCell className="text-right font-mono tabular-nums text-muted-foreground">
                          {formatNumber(w.signal_score, 2)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">
                {tl({ zh: "风险报告", en: "Risk report" })}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {risk ? (
                <div className="space-y-1">
                  <Metric
                    label={tl({ zh: "组合波动率", en: "Portfolio volatility" })}
                    value={fmtPct(risk.portfolio_volatility)}
                  />
                  <Metric
                    label={tl({ zh: "夏普比率", en: "Sharpe ratio" })}
                    value={formatNumber(risk.sharpe_ratio, 3)}
                  />
                  <Metric
                    label={tl({ zh: "分散化比率", en: "Diversification ratio" })}
                    value={formatNumber(risk.diversification_ratio, 3)}
                  />
                  <Separator className="my-2" />
                  <Metric label="VaR(95%)" value={fmtPct(risk.var_95)} />
                  <Metric label="CVaR(95%)" value={fmtPct(risk.cvar_95)} />
                  <Metric
                    label={tl({ zh: "最大回撤", en: "Max drawdown" })}
                    value={fmtPct(risk.max_drawdown)}
                  />
                  {risk.risk_contributions &&
                    risk.risk_contributions.length > 0 && (
                      <>
                        <Separator className="my-2" />
                        <p className="pt-1 text-xs font-medium text-muted-foreground">
                          {tl({ zh: "边际风险贡献", en: "Marginal risk contribution" })}
                        </p>
                        <div className="space-y-1 pt-1">
                          {risk.risk_contributions.map((rc) => (
                            <div
                              key={rc.symbol}
                              className="flex items-center justify-between"
                            >
                              <span className="font-mono text-sm">
                                {rc.symbol}
                              </span>
                              <span className="font-mono text-sm tabular-nums text-muted-foreground">
                                {fmtPct(rc.contribution)}
                              </span>
                            </div>
                          ))}
                        </div>
                      </>
                    )}
                </div>
              ) : (
                <EmptyState
                  icon={<ShieldCheck className="h-8 w-8" />}
                  title={tl({ zh: "无风险报告", en: "No risk report" })}
                  description={tl({
                    zh: "本次分配未返回风险指标。",
                    en: "This allocation returned no risk metrics.",
                  })}
                />
              )}
            </CardContent>
          </Card>

          {data.adjustments && data.adjustments.length > 0 && (
            <Card className="lg:col-span-2">
              <CardHeader className="pb-3">
                <CardTitle className="text-base">
                  {tl({ zh: "约束调整明细", en: "Constraint adjustments" })}
                </CardTitle>
                <CardDescription>
                  {tl({
                    zh: "约束求解器对原始权重的调整记录。",
                    en: "Adjustments applied by the constraint solver to the raw weights.",
                  })}
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="rounded-md border border-border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>{tl({ zh: "标的", en: "Symbol" })}</TableHead>
                        <TableHead className="text-right">
                          {tl({ zh: "调整前", en: "Before" })}
                        </TableHead>
                        <TableHead className="text-right">
                          {tl({ zh: "调整后", en: "After" })}
                        </TableHead>
                        <TableHead>{tl({ zh: "原因", en: "Reason" })}</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {data.adjustments.map((adj, i) => (
                        <TableRow key={`${adj.symbol}-${i}`}>
                          <TableCell className="font-mono">
                            {adj.symbol}
                          </TableCell>
                          <TableCell className="text-right font-mono tabular-nums">
                            {fmtPct(adj.from)}
                          </TableCell>
                          <TableCell className="text-right font-mono tabular-nums">
                            {fmtPct(adj.to)}
                          </TableCell>
                          <TableCell className="text-muted-foreground">
                            {adj.reason}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </CardContent>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}

function SizingTab({
  allocateResult,
  lots,
  setLots,
  capital,
  setCapital,
}: {
  allocateResult: AllocateResponse | null;
  lots: LotRow[];
  setLots: React.Dispatch<React.SetStateAction<LotRow[]>>;
  capital: string;
  setCapital: React.Dispatch<React.SetStateAction<string>>;
}) {
  const { tl } = useT();
  const [commissionRate, setCommissionRate] = useState("0.0003");
  const [stampTaxRate, setStampTaxRate] = useState("0.001");

  const mutate = useMutation({
    mutationFn: (body: SizingRequest) => portfolioApi.sizing(body),
  });

  const updateLot = (i: number, patch: Partial<LotRow>) =>
    setLots((prev) =>
      prev.map((l, idx) => (idx === i ? { ...l, ...patch } : l)),
    );
  const addLot = () =>
    setLots((prev) => [...prev, { symbol: "", lot_size: "", price: "" }]);
  const removeLot = (i: number) =>
    setLots((prev) => prev.filter((_, idx) => idx !== i));

  const applyWeightsToLots = () => {
    const ws = allocateResult?.weights ?? [];
    if (ws.length === 0) return;
    setLots(
      ws.map((w) => ({
        symbol: w.symbol,
        lot_size: "100",
        price: "",
      })),
    );
  };

  const weights = (allocateResult?.weights ?? []).map((w) => ({
    symbol: w.symbol,
    weight: w.weight,
  }));
  const parsedLots = lots
    .filter(
      (l) =>
        l.symbol.trim() !== "" &&
        l.lot_size.trim() !== "" &&
        l.price.trim() !== "" &&
        !Number.isNaN(Number(l.lot_size)) &&
        !Number.isNaN(Number(l.price)),
    )
    .map((l) => ({
      symbol: l.symbol.trim(),
      lot_size: Number(l.lot_size),
      price: Number(l.price),
    }));
  const prices: Record<string, number> = {};
  for (const l of parsedLots) prices[l.symbol] = l.price;

  const handleCompute = () => {
    if (weights.length === 0 || parsedLots.length === 0) return;
    mutate.mutate({
      weights,
      capital: Number(capital),
      lot_info: parsedLots,
      prices,
      commission_rate: parseNum(commissionRate),
      stamp_tax_rate: parseNum(stampTaxRate),
    });
  };

  const hasWeights = weights.length > 0;
  const data = mutate.data;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Coins className="h-4 w-4 text-primary" />
            {tl({ zh: "离散交易求解参数", en: "Discrete trade solver parameters" })}
          </CardTitle>
          <CardDescription>
            {tl({
              zh: "将连续目标权重转化为离散整手交易,估算资金占用与交易成本。",
              en: "Convert continuous target weights into discrete round-lot trades, estimating capital usage and transaction costs.",
            })}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          {!hasWeights && (
            <div className="rounded-md border border-warning/30 bg-warning/5 p-3 text-sm text-warning">
              {tl({
                zh: "尚无目标权重,请先在「目标权重分配」完成计算。",
                en: "No target weights yet. Complete the calculation in “Target Weight Allocation” first.",
              })}
            </div>
          )}

          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            <div className="space-y-1.5">
              <Label>{tl({ zh: "资金额度", en: "Capital" })}</Label>
              <Input
                type="number"
                value={capital}
                onChange={(e) => setCapital(e.target.value)}
                className="tabular-nums"
              />
            </div>
            <div className="space-y-1.5">
              <Label>{tl({ zh: "佣金率", en: "Commission rate" })}</Label>
              <Input
                type="number"
                value={commissionRate}
                onChange={(e) => setCommissionRate(e.target.value)}
                className="tabular-nums"
              />
            </div>
            <div className="space-y-1.5">
              <Label>{tl({ zh: "印花税率", en: "Stamp tax rate" })}</Label>
              <Input
                type="number"
                value={stampTaxRate}
                onChange={(e) => setStampTaxRate(e.target.value)}
                className="tabular-nums"
              />
            </div>
          </div>

          <Separator />

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>{tl({ zh: "标的手数与价格", en: "Symbol lots & prices" })}</Label>
              <Button
                variant="outline"
                size="sm"
                onClick={applyWeightsToLots}
                disabled={!hasWeights}
              >
                <ArrowRight className="h-4 w-4" />
                {tl({ zh: "从分配结果导入", en: "Import from allocation" })}
              </Button>
            </div>
            <div className="space-y-2">
              {lots.map((l, i) => (
                <div key={i} className="flex items-center gap-2">
                  <Input
                    placeholder={tl({ zh: "标的代码", en: "Symbol" })}
                    value={l.symbol}
                    onChange={(e) => updateLot(i, { symbol: e.target.value })}
                    className="font-mono"
                  />
                  <Input
                    type="number"
                    placeholder={tl({ zh: "手数 (lot)", en: "Lots" })}
                    value={l.lot_size}
                    onChange={(e) =>
                      updateLot(i, { lot_size: e.target.value })
                    }
                    className="w-28 tabular-nums"
                  />
                  <Input
                    type="number"
                    placeholder={tl({ zh: "单价", en: "Price" })}
                    value={l.price}
                    onChange={(e) => updateLot(i, { price: e.target.value })}
                    className="w-32 tabular-nums"
                  />
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => removeLot(i)}
                    disabled={lots.length === 1}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              ))}
            </div>
            <Button variant="outline" size="sm" onClick={addLot}>
              <Plus className="h-4 w-4" />
              {tl({ zh: "添加标的", en: "Add symbol" })}
            </Button>
          </div>

          <div className="flex items-center gap-3">
            <Button
              onClick={handleCompute}
              disabled={!hasWeights || parsedLots.length === 0 || mutate.isPending}
            >
              <Calculator className="h-4 w-4" />
              {mutate.isPending
                ? tl({ zh: "求解中…", en: "Solving…" })
                : tl({ zh: "求解离散交易", en: "Solve discrete trades" })}
            </Button>
            {!hasWeights && (
              <span className="text-xs text-muted-foreground">
                {tl({
                  zh: "需先完成目标权重分配",
                  en: "Complete target weight allocation first",
                })}
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {mutate.isError && (
        <ErrorState
          title={tl({ zh: "离散求解失败", en: "Discrete solve failed" })}
          message={
            mutate.error instanceof Error
              ? mutate.error.message
              : tl({
                  zh: "无法求解离散交易",
                  en: "Unable to solve discrete trades",
                })
          }
        />
      )}

      {data && (
        <>
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">
                {tl({ zh: "交易明细", en: "Trade details" })}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="rounded-md border border-border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{tl({ zh: "标的", en: "Symbol" })}</TableHead>
                      <TableHead>{tl({ zh: "方向", en: "Side" })}</TableHead>
                      <TableHead className="text-right">
                        {tl({ zh: "手数", en: "Lots" })}
                      </TableHead>
                      <TableHead className="text-right">
                        {tl({ zh: "股数", en: "Shares" })}
                      </TableHead>
                      <TableHead className="text-right">
                        {tl({ zh: "金额", en: "Amount" })}
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.trades.map((t) => (
                      <TableRow key={t.symbol}>
                        <TableCell className="font-mono">{t.symbol}</TableCell>
                        <TableCell>
                          <Badge
                            variant={t.side === "buy" ? "success" : "destructive"}
                            className="font-mono uppercase"
                          >
                            {t.side === "buy"
                              ? tl({ zh: "买入", en: "Buy" })
                              : tl({ zh: "卖出", en: "Sell" })}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-right font-mono tabular-nums">
                          {formatNumber(t.lots, 0)}
                        </TableCell>
                        <TableCell className="text-right font-mono tabular-nums">
                          {formatNumber(t.shares, 0)}
                        </TableCell>
                        <TableCell className="text-right font-mono tabular-nums">
                          {formatCurrency(t.notional)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </CardContent>
          </Card>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <Card>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">
                  {tl({ zh: "期初资金", en: "Initial capital" })}
                </p>
                <p className="mt-1 font-mono text-lg tabular-nums">
                  {formatCurrency(data.cash_before)}
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">
                  {tl({ zh: "期末资金", en: "Final capital" })}
                </p>
                <p className="mt-1 font-mono text-lg tabular-nums">
                  {formatCurrency(data.cash_after)}
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">
                  {tl({ zh: "保证金占用", en: "Margin required" })}
                </p>
                <p className="mt-1 font-mono text-lg tabular-nums">
                  {formatCurrency(data.margin_required)}
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">
                  {tl({ zh: "预估佣金", en: "Est. commission" })}
                </p>
                <p className="mt-1 font-mono text-lg tabular-nums">
                  {formatCurrency(data.est_commission)}
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">
                  {tl({ zh: "预估印花税", en: "Est. stamp tax" })}
                </p>
                <p className="mt-1 font-mono text-lg tabular-nums">
                  {formatCurrency(data.est_tax)}
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">
                  {tl({ zh: "预估滑点", en: "Est. slippage" })}
                </p>
                <p className="mt-1 font-mono text-lg tabular-nums">
                  {formatCurrency(data.est_slippage)}
                </p>
              </CardContent>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

function FeasibilityTab({
  allocateResult,
  sharedLots,
}: {
  allocateResult: AllocateResponse | null;
  sharedLots: LotRow[];
}) {
  const { tl } = useT();
  const [lotRows, setLotRows] = useState<LotRow[]>([]);

  const mutate = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      portfolioApi.feasibility(body),
  });

  const weights = (allocateResult?.weights ?? []).map((w) => ({
    symbol: w.symbol,
    weight: w.weight,
  }));
  const parsedLots = lotRows
    .filter(
      (l) =>
        l.symbol.trim() !== "" &&
        l.lot_size.trim() !== "" &&
        l.price.trim() !== "" &&
        !Number.isNaN(Number(l.lot_size)) &&
        !Number.isNaN(Number(l.price)),
    )
    .map((l) => ({
      symbol: l.symbol.trim(),
      lot_size: Number(l.lot_size),
      price: Number(l.price),
    }));
  const prices: Record<string, number> = {};
  for (const l of parsedLots) prices[l.symbol] = l.price;

  const updateLot = (i: number, patch: Partial<LotRow>) =>
    setLotRows((prev) =>
      prev.map((l, idx) => (idx === i ? { ...l, ...patch } : l)),
    );
  const addLot = () =>
    setLotRows((prev) => [...prev, { symbol: "", lot_size: "", price: "" }]);
  const removeLot = (i: number) =>
    setLotRows((prev) => prev.filter((_, idx) => idx !== i));

  const applyContext = () => {
    setLotRows(
      sharedLots
        .filter((l) => l.symbol.trim() !== "")
        .map((l) => ({ ...l })),
    );
  };

  const handleCompute = () => {
    if (weights.length === 0 || parsedLots.length === 0) return;
    mutate.mutate({ weights, lot_info: parsedLots, prices });
  };

  const hasWeights = weights.length > 0;
  const tiers = mutate.data ?? [];

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Layers className="h-4 w-4 text-primary" />
            {tl({ zh: "资金可行性评估", en: "Capital feasibility assessment" })}
          </CardTitle>
          <CardDescription>
            {tl({
              zh: "在不同资金档位下评估目标组合的可达成性与容量压力。",
              en: "Assess attainability and capacity pressure of the target portfolio across capital tiers.",
            })}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          {!hasWeights && (
            <div className="rounded-md border border-warning/30 bg-warning/5 p-3 text-sm text-warning">
              {tl({
                zh: "尚无目标权重,请先在「目标权重分配」完成计算。",
                en: "No target weights yet. Complete the calculation in “Target Weight Allocation” first.",
              })}
            </div>
          )}

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>
                {tl({ zh: "标的与手数价格 (可手动调整)", en: "Symbol lots & prices (editable)" })}
              </Label>
              <Button
                variant="outline"
                size="sm"
                onClick={applyContext}
                disabled={sharedLots.filter((l) => l.symbol.trim()).length === 0}
              >
                <RefreshCw className="h-4 w-4" />
                {tl({ zh: "应用离散求解上下文", en: "Apply sizing context" })}
              </Button>
            </div>
            <div className="space-y-2">
              {lotRows.map((l, i) => (
                <div key={i} className="flex items-center gap-2">
                  <Input
                    placeholder={tl({ zh: "标的代码", en: "Symbol" })}
                    value={l.symbol}
                    onChange={(e) => updateLot(i, { symbol: e.target.value })}
                    className="font-mono"
                  />
                  <Input
                    type="number"
                    placeholder={tl({ zh: "手数", en: "Lots" })}
                    value={l.lot_size}
                    onChange={(e) => updateLot(i, { lot_size: e.target.value })}
                    className="w-28 tabular-nums"
                  />
                  <Input
                    type="number"
                    placeholder={tl({ zh: "单价", en: "Price" })}
                    value={l.price}
                    onChange={(e) => updateLot(i, { price: e.target.value })}
                    className="w-32 tabular-nums"
                  />
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => removeLot(i)}
                    disabled={lotRows.length === 1}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              ))}
            </div>
            <Button variant="outline" size="sm" onClick={addLot}>
              <Plus className="h-4 w-4" />
              {tl({ zh: "添加标的", en: "Add symbol" })}
            </Button>
          </div>

          <div className="flex items-center gap-3">
            <Button
              onClick={handleCompute}
              disabled={!hasWeights || parsedLots.length === 0 || mutate.isPending}
            >
              <ShieldCheck className="h-4 w-4" />
              {mutate.isPending
                ? tl({ zh: "评估中…", en: "Evaluating…" })
                : tl({ zh: "评估可行性", en: "Evaluate feasibility" })}
            </Button>
            {parsedLots.length === 0 && (
              <span className="text-xs text-muted-foreground">
                {tl({
                  zh: "请补充标的手数与价格",
                  en: "Provide symbol lots and prices",
                })}
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {mutate.isError && (
        <ErrorState
          title={tl({ zh: "可行性评估失败", en: "Feasibility assessment failed" })}
          message={
            mutate.error instanceof Error
              ? mutate.error.message
              : tl({
                  zh: "无法评估资金可行性",
                  en: "Unable to assess capital feasibility",
                })
          }
        />
      )}

      {mutate.isSuccess && tiers.length === 0 && (
        <EmptyState
          icon={<Layers className="h-8 w-8" />}
          title={tl({ zh: "无可行性档位", en: "No feasible tiers" })}
          description={tl({
            zh: "未返回任何资金档位结果。",
            en: "No capital tier results were returned.",
          })}
        />
      )}

      {tiers.length > 0 && (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {tiers.map((t) => (
            <FeasibilityCard key={t.tier} tier={t} />
          ))}
        </div>
      )}
    </div>
  );
}

function FeasibilityCard({ tier }: { tier: TierFeasibility }) {
  const { tl } = useT();
  const pressure = Math.min(Math.max(tier.capacity_pressure, 0), 1);
  return (
    <Card className={cn(!tier.feasible && "border-destructive/40")}>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="text-base">{tier.tier}</CardTitle>
            <p className="mt-0.5 font-mono text-lg tabular-nums">
              {formatCurrency(tier.capital, 0)}
            </p>
          </div>
          <StatusBadge status={tier.feasible ? "ok" : "failed"}>
            {tier.feasible
              ? tl({ zh: "可行", en: "Feasible" })
              : tl({ zh: "不可行", en: "Infeasible" })}
          </StatusBadge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <Metric
          label={tl({ zh: "跟踪误差", en: "Tracking error" })}
          value={formatPercent(tier.tracking_error)}
        />
        <Metric
          label={tl({ zh: "保证金占用", en: "Margin required" })}
          value={formatCurrency(tier.margin_required)}
        />

        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-sm text-muted-foreground">
              {tl({ zh: "容量压力", en: "Capacity pressure" })}
            </span>
            <span className="font-mono text-sm tabular-nums">
              {fmtPct(pressure)}
            </span>
          </div>
          <Progress
            value={pressure * 100}
            indicatorClassName={riskRowColor(pressure)}
          />
        </div>

        <Separator />

        <div className="space-y-1.5">
          <span className="text-sm text-muted-foreground">
            {tl({ zh: "无法成交标的", en: "Unfillable symbols" })}
          </span>
          {tier.unfillable_symbols.length > 0 ? (
            <div className="flex flex-wrap gap-1">
              {tier.unfillable_symbols.map((s) => (
                <Badge key={s} variant="destructive" className="font-mono">
                  {s}
                </Badge>
              ))}
            </div>
          ) : (
            <p className="text-sm text-success">{tl({ zh: "无", en: "None" })}</p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export default function PortfolioRisk() {
  const { tl } = useT();
  const [allocateResult, setAllocateResult] =
    useState<AllocateResponse | null>(null);
  const [lots, setLots] = useState<LotRow[]>([
    { symbol: "", lot_size: "", price: "" },
  ]);
  const [capital, setCapital] = useState("100000");

  return (
    <div>
      <PageHeader
        title={tl({ zh: "组合与风险", en: "Portfolio & Risk" })}
        description={tl({
          zh: "目标权重分配、离散交易求解与资金可行性分析",
          en: "Target weight allocation, discrete trade solving and capital feasibility analysis",
        })}
        breadcrumbs={[
          { label: tl({ zh: "研究", en: "Research" }), href: "/research" },
          { label: tl({ zh: "组合与风险", en: "Portfolio & Risk" }) },
        ]}
      />
      <WorkflowIndicator currentPath="/research/portfolio" />

      <Tabs defaultValue="allocate">
        <TabsList>
          <TabsTrigger value="allocate">
            <PieChartIcon className="h-4 w-4" />
            {tl({ zh: "目标权重分配", en: "Target Weight Allocation" })}
          </TabsTrigger>
          <TabsTrigger value="sizing">
            <Coins className="h-4 w-4" />
            {tl({ zh: "离散交易求解", en: "Discrete Trade Solver" })}
          </TabsTrigger>
          <TabsTrigger value="feasibility">
            <Layers className="h-4 w-4" />
            {tl({ zh: "资金可行性", en: "Capital Feasibility" })}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="allocate">
          <AllocationTab onAllocated={setAllocateResult} />
        </TabsContent>
        <TabsContent value="sizing">
          <SizingTab
            allocateResult={allocateResult}
            lots={lots}
            setLots={setLots}
            capital={capital}
            setCapital={setCapital}
          />
        </TabsContent>
        <TabsContent value="feasibility">
          <FeasibilityTab
            allocateResult={allocateResult}
            sharedLots={lots}
          />
        </TabsContent>
      </Tabs>
      <NextStepCTA
        nextPath="/research/simulation"
        nextLabel={{ zh: "模拟盘", en: "Paper Trading" }}
        description={{
          zh: "用纸面撮合验证策略在真实交易环境下的表现",
          en: "Validate strategy performance in a realistic trading environment via paper matching.",
        }}
      />
    </div>
  );
}
