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
            信号与约束
          </CardTitle>
          <CardDescription>
            输入标的信号得分与组合约束,生成目标权重分配与风险报告。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>信号输入</Label>
              <Button variant="outline" size="sm" onClick={addSignal}>
                <Plus className="h-4 w-4" />
                添加信号
              </Button>
            </div>
            <div className="space-y-2">
              {signals.map((s, i) => (
                <div
                  key={i}
                  className="flex items-center gap-2"
                >
                  <Input
                    placeholder="标的代码"
                    value={s.symbol}
                    onChange={(e) =>
                      updateSignal(i, { symbol: e.target.value })
                    }
                    className="font-mono"
                  />
                  <Input
                    type="number"
                    placeholder="得分"
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
                      <SelectItem value="long">多头</SelectItem>
                      <SelectItem value="short">空头</SelectItem>
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
              <Label>分配方法</Label>
              <Select
                value={method}
                onValueChange={(v) => setMethod(v as AllocationMethod)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="equal_weight">等权重</SelectItem>
                  <SelectItem value="inverse_volatility">反波动率</SelectItem>
                  <SelectItem value="erc">风险平价 (ERC)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>单资产权重上限</Label>
              <Input
                type="number"
                placeholder="如 0.2"
                value={maxWeight}
                onChange={(e) => setMaxWeight(e.target.value)}
                className="tabular-nums"
              />
            </div>
            <div className="space-y-1.5">
              <Label>最低现金缓冲</Label>
              <Input
                type="number"
                placeholder="如 0.05"
                value={minCash}
                onChange={(e) => setMinCash(e.target.value)}
                className="tabular-nums"
              />
            </div>
            <div className="space-y-1.5">
              <Label>最大杠杆</Label>
              <Input
                type="number"
                placeholder="如 1.0"
                value={maxLev}
                onChange={(e) => setMaxLev(e.target.value)}
                className="tabular-nums"
              />
            </div>
          </div>

          <div className="flex items-center justify-between rounded-md border border-border px-3 py-2.5">
            <div>
              <Label>仅做多</Label>
              <p className="text-xs text-muted-foreground">
                关闭后允许空头信号产生负权重
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
              {mutate.isPending ? "计算中…" : "计算目标权重"}
            </Button>
            {parsedSignals.length === 0 && (
              <span className="text-xs text-muted-foreground">
                请至少填写一行有效的标的与得分
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {mutate.isError && (
        <ErrorState
          title="分配计算失败"
          message={
            mutate.error instanceof Error
              ? mutate.error.message
              : "无法计算目标权重分配"
          }
        />
      )}

      {data && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">目标权重分布</CardTitle>
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
                  title="无有效权重"
                  description="所有权重均为零或负值。"
                />
              )}

              <div className="rounded-md border border-border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>标的</TableHead>
                      <TableHead className="text-right">权重</TableHead>
                      <TableHead className="text-right">信号得分</TableHead>
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
              <CardTitle className="text-base">风险报告</CardTitle>
            </CardHeader>
            <CardContent>
              {risk ? (
                <div className="space-y-1">
                  <Metric
                    label="组合波动率"
                    value={fmtPct(risk.portfolio_volatility)}
                  />
                  <Metric
                    label="夏普比率"
                    value={formatNumber(risk.sharpe_ratio, 3)}
                  />
                  <Metric
                    label="分散化比率"
                    value={formatNumber(risk.diversification_ratio, 3)}
                  />
                  <Separator className="my-2" />
                  <Metric label="VaR(95%)" value={fmtPct(risk.var_95)} />
                  <Metric label="CVaR(95%)" value={fmtPct(risk.cvar_95)} />
                  <Metric
                    label="最大回撤"
                    value={fmtPct(risk.max_drawdown)}
                  />
                  {risk.risk_contributions &&
                    risk.risk_contributions.length > 0 && (
                      <>
                        <Separator className="my-2" />
                        <p className="pt-1 text-xs font-medium text-muted-foreground">
                          边际风险贡献
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
                  title="无风险报告"
                  description="本次分配未返回风险指标。"
                />
              )}
            </CardContent>
          </Card>

          {data.adjustments && data.adjustments.length > 0 && (
            <Card className="lg:col-span-2">
              <CardHeader className="pb-3">
                <CardTitle className="text-base">约束调整明细</CardTitle>
                <CardDescription>
                  约束求解器对原始权重的调整记录。
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="rounded-md border border-border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>标的</TableHead>
                        <TableHead className="text-right">调整前</TableHead>
                        <TableHead className="text-right">调整后</TableHead>
                        <TableHead>原因</TableHead>
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
            离散交易求解参数
          </CardTitle>
          <CardDescription>
            将连续目标权重转化为离散整手交易,估算资金占用与交易成本。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          {!hasWeights && (
            <div className="rounded-md border border-warning/30 bg-warning/5 p-3 text-sm text-warning">
              尚无目标权重,请先在「目标权重分配」完成计算。
            </div>
          )}

          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            <div className="space-y-1.5">
              <Label>资金额度</Label>
              <Input
                type="number"
                value={capital}
                onChange={(e) => setCapital(e.target.value)}
                className="tabular-nums"
              />
            </div>
            <div className="space-y-1.5">
              <Label>佣金率</Label>
              <Input
                type="number"
                value={commissionRate}
                onChange={(e) => setCommissionRate(e.target.value)}
                className="tabular-nums"
              />
            </div>
            <div className="space-y-1.5">
              <Label>印花税率</Label>
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
              <Label>标的手数与价格</Label>
              <Button
                variant="outline"
                size="sm"
                onClick={applyWeightsToLots}
                disabled={!hasWeights}
              >
                <ArrowRight className="h-4 w-4" />
                从分配结果导入
              </Button>
            </div>
            <div className="space-y-2">
              {lots.map((l, i) => (
                <div key={i} className="flex items-center gap-2">
                  <Input
                    placeholder="标的代码"
                    value={l.symbol}
                    onChange={(e) => updateLot(i, { symbol: e.target.value })}
                    className="font-mono"
                  />
                  <Input
                    type="number"
                    placeholder="手数 (lot)"
                    value={l.lot_size}
                    onChange={(e) =>
                      updateLot(i, { lot_size: e.target.value })
                    }
                    className="w-28 tabular-nums"
                  />
                  <Input
                    type="number"
                    placeholder="单价"
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
              添加标的
            </Button>
          </div>

          <div className="flex items-center gap-3">
            <Button
              onClick={handleCompute}
              disabled={!hasWeights || parsedLots.length === 0 || mutate.isPending}
            >
              <Calculator className="h-4 w-4" />
              {mutate.isPending ? "求解中…" : "求解离散交易"}
            </Button>
            {!hasWeights && (
              <span className="text-xs text-muted-foreground">
                需先完成目标权重分配
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {mutate.isError && (
        <ErrorState
          title="离散求解失败"
          message={
            mutate.error instanceof Error
              ? mutate.error.message
              : "无法求解离散交易"
          }
        />
      )}

      {data && (
        <>
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">交易明细</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="rounded-md border border-border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>标的</TableHead>
                      <TableHead>方向</TableHead>
                      <TableHead className="text-right">手数</TableHead>
                      <TableHead className="text-right">股数</TableHead>
                      <TableHead className="text-right">金额</TableHead>
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
                            {t.side === "buy" ? "买入" : "卖出"}
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
                <p className="text-xs text-muted-foreground">期初资金</p>
                <p className="mt-1 font-mono text-lg tabular-nums">
                  {formatCurrency(data.cash_before)}
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">期末资金</p>
                <p className="mt-1 font-mono text-lg tabular-nums">
                  {formatCurrency(data.cash_after)}
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">保证金占用</p>
                <p className="mt-1 font-mono text-lg tabular-nums">
                  {formatCurrency(data.margin_required)}
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">预估佣金</p>
                <p className="mt-1 font-mono text-lg tabular-nums">
                  {formatCurrency(data.est_commission)}
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">预估印花税</p>
                <p className="mt-1 font-mono text-lg tabular-nums">
                  {formatCurrency(data.est_tax)}
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">预估滑点</p>
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
            资金可行性评估
          </CardTitle>
          <CardDescription>
            在不同资金档位下评估目标组合的可达成性与容量压力。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          {!hasWeights && (
            <div className="rounded-md border border-warning/30 bg-warning/5 p-3 text-sm text-warning">
              尚无目标权重,请先在「目标权重分配」完成计算。
            </div>
          )}

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>标的与手数价格 (可手动调整)</Label>
              <Button
                variant="outline"
                size="sm"
                onClick={applyContext}
                disabled={sharedLots.filter((l) => l.symbol.trim()).length === 0}
              >
                <RefreshCw className="h-4 w-4" />
                应用离散求解上下文
              </Button>
            </div>
            <div className="space-y-2">
              {lotRows.map((l, i) => (
                <div key={i} className="flex items-center gap-2">
                  <Input
                    placeholder="标的代码"
                    value={l.symbol}
                    onChange={(e) => updateLot(i, { symbol: e.target.value })}
                    className="font-mono"
                  />
                  <Input
                    type="number"
                    placeholder="手数"
                    value={l.lot_size}
                    onChange={(e) => updateLot(i, { lot_size: e.target.value })}
                    className="w-28 tabular-nums"
                  />
                  <Input
                    type="number"
                    placeholder="单价"
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
              添加标的
            </Button>
          </div>

          <div className="flex items-center gap-3">
            <Button
              onClick={handleCompute}
              disabled={!hasWeights || parsedLots.length === 0 || mutate.isPending}
            >
              <ShieldCheck className="h-4 w-4" />
              {mutate.isPending ? "评估中…" : "评估可行性"}
            </Button>
            {parsedLots.length === 0 && (
              <span className="text-xs text-muted-foreground">
                请补充标的手数与价格
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {mutate.isError && (
        <ErrorState
          title="可行性评估失败"
          message={
            mutate.error instanceof Error
              ? mutate.error.message
              : "无法评估资金可行性"
          }
        />
      )}

      {mutate.isSuccess && tiers.length === 0 && (
        <EmptyState
          icon={<Layers className="h-8 w-8" />}
          title="无可行性档位"
          description="未返回任何资金档位结果。"
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
            {tier.feasible ? "可行" : "不可行"}
          </StatusBadge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <Metric label="跟踪误差" value={formatPercent(tier.tracking_error)} />
        <Metric
          label="保证金占用"
          value={formatCurrency(tier.margin_required)}
        />

        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-sm text-muted-foreground">容量压力</span>
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
          <span className="text-sm text-muted-foreground">无法成交标的</span>
          {tier.unfillable_symbols.length > 0 ? (
            <div className="flex flex-wrap gap-1">
              {tier.unfillable_symbols.map((s) => (
                <Badge key={s} variant="destructive" className="font-mono">
                  {s}
                </Badge>
              ))}
            </div>
          ) : (
            <p className="text-sm text-success">无</p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export default function PortfolioRisk() {
  const [allocateResult, setAllocateResult] =
    useState<AllocateResponse | null>(null);
  const [lots, setLots] = useState<LotRow[]>([
    { symbol: "", lot_size: "", price: "" },
  ]);
  const [capital, setCapital] = useState("100000");

  return (
    <div>
      <PageHeader
        title="组合与风险"
        description="目标权重分配、离散交易求解与资金可行性分析"
        breadcrumbs={[
          { label: "研究", href: "/research" },
          { label: "组合与风险" },
        ]}
      />
      <WorkflowIndicator currentPath="/research/portfolio" />

      <Tabs defaultValue="allocate">
        <TabsList>
          <TabsTrigger value="allocate">
            <PieChartIcon className="h-4 w-4" />
            目标权重分配
          </TabsTrigger>
          <TabsTrigger value="sizing">
            <Coins className="h-4 w-4" />
            离散交易求解
          </TabsTrigger>
          <TabsTrigger value="feasibility">
            <Layers className="h-4 w-4" />
            资金可行性
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
        nextLabel="模拟盘"
        description="用纸面撮合验证策略在真实交易环境下的表现"
      />
    </div>
  );
}
