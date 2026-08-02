import { WorkflowIndicator } from "@/components/research/ResearchHint";
import { type ReactNode, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Activity,
  ArrowLeft,
  BarChart3,
  FileText,
  Gauge,
  Layers,
  LineChart as LineChartIcon,
  Target,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
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
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { StatCard } from "@/components/ui/stat-card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import {
  researchRunApi,
  type ResearchRunSummary,
} from "@/lib/research";
import {
  simulationApi,
  type SimulationPosition,
  type SimulationSession,
} from "@/lib/simulation";
import {
  cn,
  formatCurrency,
  formatNumber,
  formatPercent,
  timeAgo,
} from "@/lib/utils";

type ReportSource = "run" | "simulation";

interface EquityPoint {
  timestamp: string;
  equity: number;
}

interface ReportMetrics {
  total_return: number | null;
  annual_return: number | null;
  sharpe_ratio: number | null;
  max_drawdown: number | null;
  win_rate: number | null;
  total_trades: number;
}

interface ExtraMetric {
  label: string;
  value: number;
  format: "percent" | "number";
}

const SOURCE_OPTIONS: { value: ReportSource; label: string }[] = [
  { value: "run", label: "研究运行" },
  { value: "simulation", label: "模拟会话" },
];

const SIM_REPORT_STATUSES = ["stopped", "archived"];

const EXTRA_METRIC_DEFS: {
  key: string;
  label: string;
  format: "percent" | "number";
}[] = [
  { key: "profit_factor", label: "盈亏比", format: "number" },
  { key: "sortino_ratio", label: "Sortino", format: "number" },
  { key: "calmar_ratio", label: "Calmar", format: "number" },
  { key: "volatility", label: "年化波动率", format: "percent" },
  { key: "avg_win", label: "平均盈利", format: "percent" },
  { key: "avg_loss", label: "平均亏损", format: "percent" },
  { key: "max_runup", label: "最大涨幅", format: "percent" },
  { key: "longest_win_streak", label: "最长连胜", format: "number" },
  { key: "longest_loss_streak", label: "最长连亏", format: "number" },
];

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

function pnlColor(v: number | null | undefined): string {
  if (v === null || v === undefined) return "text-muted-foreground";
  if (v > 0) return "text-success";
  if (v < 0) return "text-destructive";
  return "text-muted-foreground";
}

function runVersion(run: ResearchRunSummary): number | null {
  if (typeof run.strategy_version === "number") return run.strategy_version;
  const value = run.manifest?.strategy_spec_version;
  return typeof value === "number" ? value : null;
}

function runCapital(run: ResearchRunSummary): number | null {
  if (typeof run.initial_capital === "number") return run.initial_capital;
  const value = Number(run.manifest?.initial_capital);
  return Number.isFinite(value) ? value : null;
}

function shortDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

function getMetric(
  result: Record<string, unknown> | undefined,
  key: string,
  defaultVal = 0,
): number {
  if (!result) return defaultVal;
  const direct = result[key];
  if (typeof direct === "number") return direct;
  const metrics = result["metrics"];
  if (metrics && typeof metrics === "object") {
    const nested = (metrics as Record<string, unknown>)[key];
    if (typeof nested === "number") return nested;
  }
  return defaultVal;
}

function hasMetric(
  result: Record<string, unknown> | undefined,
  key: string,
): boolean {
  if (!result) return false;
  if (typeof result[key] === "number") return true;
  const metrics = result["metrics"];
  if (metrics && typeof metrics === "object") {
    return typeof (metrics as Record<string, unknown>)[key] === "number";
  }
  return false;
}

function getEquityCurve(
  result: Record<string, unknown> | undefined,
): EquityPoint[] {
  if (!result) return [];
  const candidate =
    result["equity_curve"] ?? result["equity"] ?? result["curve"];
  if (!Array.isArray(candidate)) return [];
  const out: EquityPoint[] = [];
  for (const p of candidate) {
    if (
      p &&
      typeof p === "object" &&
      typeof (p as Record<string, unknown>)["timestamp"] === "string" &&
      typeof (p as Record<string, unknown>)["equity"] === "number"
    ) {
      const rec = p as { timestamp: string; equity: number };
      out.push({ timestamp: rec.timestamp, equity: rec.equity });
    }
  }
  return out;
}

function computeDrawdownSeries(curve: EquityPoint[]): {
  series: { timestamp: string; drawdown: number }[];
  maxDrawdown: number;
} {
  const series: { timestamp: string; drawdown: number }[] = [];
  let peak = -Infinity;
  let maxDrawdown = 0;
  for (const pt of curve) {
    if (pt.equity > peak) peak = pt.equity;
    const dd = peak > 0 ? (peak - pt.equity) / peak : 0;
    if (dd > maxDrawdown) maxDrawdown = dd;
    series.push({ timestamp: pt.timestamp, drawdown: -dd });
  }
  return { series, maxDrawdown };
}

interface ChartTooltipProps {
  active?: boolean;
  payload?: { name?: string | number; value?: number | string; color?: string }[];
  label?: string | number;
}

function EquityTooltip({ active, payload, label }: ChartTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;
  return (
    <div className="rounded-md border border-border bg-popover px-3 py-2 text-xs shadow-md">
      <p className="text-muted-foreground">
        {typeof label === "string" ? shortDate(label) : label}
      </p>
      {payload.map((p, i) => {
        const raw = p.value;
        const num = typeof raw === "number" ? raw : Number(raw);
        return (
          <p
            key={i}
            className="font-mono tabular-nums text-foreground"
            style={p.color ? { color: p.color } : undefined}
          >
            {p.name}: {Number.isFinite(num) ? formatCurrency(num) : "—"}
          </p>
        );
      })}
    </div>
  );
}

function DrawdownTooltip({ active, payload, label }: ChartTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;
  return (
    <div className="rounded-md border border-border bg-popover px-3 py-2 text-xs shadow-md">
      <p className="text-muted-foreground">
        {typeof label === "string" ? shortDate(label) : label}
      </p>
      {payload.map((p, i) => {
        const raw = p.value;
        const num = typeof raw === "number" ? raw : Number(raw);
        return (
          <p
            key={i}
            className="font-mono tabular-nums text-destructive"
          >
            回撤: {Number.isFinite(num) ? formatPercent(num) : "—"}
          </p>
        );
      })}
    </div>
  );
}

function EquityChart({ data }: { data: EquityPoint[] }) {
  if (data.length === 0) {
    return (
      <EmptyState
        icon={<LineChartIcon className="h-8 w-8" />}
        title="暂无权益数据"
        description="该运行尚未生成权益曲线。"
      />
    );
  }
  return (
    <div className="h-[300px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart
          data={data}
          margin={{ top: 8, right: 16, bottom: 4, left: 8 }}
        >
          <CartesianGrid
            stroke="hsl(var(--border))"
            strokeDasharray="3 3"
            vertical={false}
          />
          <XAxis
            dataKey="timestamp"
            tickFormatter={shortDate}
            stroke="hsl(var(--border))"
            tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
            minTickGap={24}
          />
          <YAxis
            stroke="hsl(var(--border))"
            tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
            tickFormatter={(v: number) => formatCurrency(v, 0)}
            width={88}
            domain={["auto", "auto"]}
          />
          <Tooltip content={<EquityTooltip />} />
          <Line
            type="monotone"
            dataKey="equity"
            name="权益"
            stroke="hsl(var(--primary))"
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 3 }}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function DrawdownChart({
  data,
}: {
  data: { timestamp: string; drawdown: number }[];
}) {
  if (data.length === 0) {
    return (
      <EmptyState
        icon={<TrendingDown className="h-8 w-8" />}
        title="暂无回撤数据"
        description="权益点数不足，无法计算回撤序列。"
      />
    );
  }
  return (
    <div className="h-[220px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart
          data={data}
          margin={{ top: 8, right: 16, bottom: 4, left: 8 }}
        >
          <defs>
            <linearGradient id="drawdownFill" x1="0" y1="0" x2="0" y2="1">
              <stop
                offset="0%"
                stopColor="hsl(var(--destructive))"
                stopOpacity={0.05}
              />
              <stop
                offset="100%"
                stopColor="hsl(var(--destructive))"
                stopOpacity={0.35}
              />
            </linearGradient>
          </defs>
          <CartesianGrid
            stroke="hsl(var(--border))"
            strokeDasharray="3 3"
            vertical={false}
          />
          <XAxis
            dataKey="timestamp"
            tickFormatter={shortDate}
            stroke="hsl(var(--border))"
            tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
            minTickGap={24}
          />
          <YAxis
            stroke="hsl(var(--border))"
            tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
            tickFormatter={(v: number) => formatPercent(v, 1)}
            width={72}
            domain={["auto", 0]}
          />
          <Tooltip content={<DrawdownTooltip />} />
          <Area
            type="monotone"
            dataKey="drawdown"
            name="回撤"
            stroke="hsl(var(--destructive))"
            strokeWidth={1.5}
            fill="url(#drawdownFill)"
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function MetricsGrid({ metrics }: { metrics: ReportMetrics }) {
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
      <StatCard
        label="总收益"
        icon={TrendingUp}
        value={
          <span className={cn("font-mono tabular-nums", pnlColor(metrics.total_return))}>
            {formatPercent(metrics.total_return)}
          </span>
        }
      />
      <StatCard
        label="年化收益"
        icon={TrendingUp}
        value={
          <span className={cn("font-mono tabular-nums", pnlColor(metrics.annual_return))}>
            {formatPercent(metrics.annual_return)}
          </span>
        }
      />
      <StatCard
        label="夏普比率"
        icon={Gauge}
        value={
          <span className={cn("font-mono tabular-nums", pnlColor(metrics.sharpe_ratio))}>
            {formatNumber(metrics.sharpe_ratio, 3)}
          </span>
        }
      />
      <StatCard
        label="最大回撤"
        icon={TrendingDown}
        value={
          <span className="font-mono tabular-nums text-destructive">
            {formatPercent(metrics.max_drawdown === null ? null : -metrics.max_drawdown)}
          </span>
        }
        hint={metrics.max_drawdown !== null && metrics.max_drawdown > 0 ? "峰值至谷值" : undefined}
      />
      <StatCard
        label="胜率"
        icon={Target}
        value={
          <span className="font-mono tabular-nums">
            {formatPercent(metrics.win_rate)}
          </span>
        }
      />
      <StatCard
        label="总成交笔数"
        icon={BarChart3}
        value={
          <span className="font-mono tabular-nums">
            {formatNumber(metrics.total_trades, 0)}
          </span>
        }
      />
    </div>
  );
}

function TradeSummary({ extras }: { extras: ExtraMetric[] }) {
  if (extras.length === 0) {
    return (
      <EmptyState
        icon={<Layers className="h-8 w-8" />}
        title="无额外交易明细"
        description="该运行结果未提供更多交易统计指标。"
      />
    );
  }
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-4">
      {extras.map((e) => (
        <div
          key={e.label}
          className="rounded-lg border border-border bg-card p-3"
        >
          <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
            {e.label}
          </p>
          <p
            className={cn(
              "mt-1 font-mono text-base tabular-nums",
              e.format === "percent" && e.value !== 0
                ? pnlColor(e.value)
                : "text-foreground",
            )}
          >
            {e.format === "percent"
              ? formatPercent(e.value)
              : formatNumber(e.value, 3)}
          </p>
        </div>
      ))}
    </div>
  );
}

function PositionsSnapshotTable({ rows }: { rows: SimulationPosition[] }) {
  if (rows.length === 0) {
    return (
      <EmptyState
        title="暂无持仓快照"
        description="该模拟会话当前没有任何持仓。"
      />
    );
  }
  return (
    <div className="rounded-lg border border-border">
      <Table>
        <TableHeader className="sticky top-0 bg-card">
          <TableRow>
            <TableHead>标的</TableHead>
            <TableHead>方向</TableHead>
            <TableHead className="text-right">数量</TableHead>
            <TableHead className="text-right">成本</TableHead>
            <TableHead className="text-right">市价</TableHead>
            <TableHead className="text-right">市值</TableHead>
            <TableHead className="text-right">浮动盈亏</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((p) => (
            <TableRow key={`${p.symbol}-${p.side}`}>
              <TableCell className="font-mono">{p.symbol}</TableCell>
              <TableCell>
                <Badge
                  variant={p.side.toLowerCase() === "buy" ? "success" : "destructive"}
                  className="font-mono uppercase"
                >
                  {p.side}
                </Badge>
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {formatNumber(p.quantity, 0)}
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {formatCurrency(p.avg_cost)}
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {formatCurrency(p.market_price)}
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {formatCurrency(p.market_value)}
              </TableCell>
              <TableCell
                className={cn(
                  "text-right font-mono tabular-nums",
                  pnlColor(p.unrealized_pnl),
                )}
              >
                {formatCurrency(p.unrealized_pnl)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

interface ReportContentProps {
  metrics: ReportMetrics;
  equityCurve: EquityPoint[];
  extras: ExtraMetric[];
  sourceHref: string;
  sourceLabel: string;
  sourceId: string;
  meta?: ReactNode;
  extra?: ReactNode;
}

function ReportContent({
  metrics,
  equityCurve,
  extras,
  sourceHref,
  sourceLabel,
  sourceId,
  meta,
  extra,
}: ReportContentProps) {
  const { series, maxDrawdown } = useMemo(
    () => computeDrawdownSeries(equityCurve),
    [equityCurve],
  );
  const realizedMaxDrawdown =
    maxDrawdown > 0 ? maxDrawdown : metrics.max_drawdown;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 space-y-1.5">
              <CardTitle className="flex items-center gap-2 text-base">
                <FileText className="h-4 w-4 text-primary" />
                <span className="font-mono text-sm">{sourceId}</span>
              </CardTitle>
              {meta}
            </div>
            <Button asChild variant="outline" size="sm">
              <Link to={sourceHref}>
                <ArrowLeft className="h-4 w-4" />
                查看原始{sourceLabel}
              </Link>
            </Button>
          </div>
        </CardHeader>
      </Card>

      <div>
        <h3 className="mb-2 text-sm font-semibold text-foreground">
          绩效指标
        </h3>
        <MetricsGrid metrics={metrics} />
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">权益曲线</CardTitle>
        </CardHeader>
        <CardContent>
          <EquityChart data={equityCurve} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">回撤分析</CardTitle>
            <div className="text-right">
              <p className="text-xs text-muted-foreground">最大回撤</p>
              <p className="font-mono text-sm tabular-nums text-destructive">
                {formatPercent(realizedMaxDrawdown === null ? null : -realizedMaxDrawdown)}
              </p>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <DrawdownChart data={series} />
        </CardContent>
      </Card>

      <div>
        <h3 className="mb-2 text-sm font-semibold text-foreground">
          交易明细摘要
        </h3>
        <TradeSummary extras={extras} />
      </div>

      {extra && (
        <>
          <Separator />
          <div>
            <h3 className="mb-2 text-sm font-semibold text-foreground">
              持仓快照
            </h3>
            {extra}
          </div>
        </>
      )}
    </div>
  );
}

function RunReportView({ runId }: { runId: string }) {
  const detailQuery = useQuery({
    queryKey: ["reports", "run-detail", runId],
    queryFn: () => researchRunApi.get(runId),
  });

  if (detailQuery.isLoading) return <LoadingState rows={6} />;
  if (detailQuery.isError) {
    return (
      <ErrorState
        message={errorMessage(detailQuery.error, "无法加载研究运行结果")}
        onRetry={() => detailQuery.refetch()}
      />
    );
  }

  const detail = detailQuery.data;
  if (!detail) return null;
  const result = detail.result;

  const metrics: ReportMetrics = {
    total_return: getMetric(result, "total_return"),
    annual_return: getMetric(result, "annual_return"),
    sharpe_ratio: getMetric(result, "sharpe_ratio"),
    max_drawdown: getMetric(result, "max_drawdown"),
    win_rate: getMetric(result, "win_rate"),
    total_trades: getMetric(result, "total_trades"),
  };
  const equityCurve = getEquityCurve(result);
  const extras: ExtraMetric[] = EXTRA_METRIC_DEFS.filter((d) =>
    hasMetric(result, d.key),
  ).map((d) => ({
    label: d.label,
    value: getMetric(result, d.key),
    format: d.format,
  }));

  return (
    <ReportContent
      metrics={metrics}
      equityCurve={equityCurve}
      extras={extras}
      sourceHref="/research/runs"
      sourceLabel="研究运行"
      sourceId={detail.run_id}
      meta={
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <StatusBadge status={detail.status} />
          <span className="font-mono">{detail.strategy_kind}</span>
          <span>·</span>
          <span className="font-mono">
            {detail.strategy_id}@v{runVersion(detail) ?? "—"}
          </span>
          <span>·</span>
          <span className="tabular-nums">
            初始资金 ¥{formatCurrency(runCapital(detail), 0)}
          </span>
          {detail.completed_at && (
            <>
              <span>·</span>
              <span className="tabular-nums">
                完成 {timeAgo(detail.completed_at)}
              </span>
            </>
          )}
        </div>
      }
    />
  );
}

function SimulationReportView({
  session,
}: {
  session: SimulationSession;
}) {
  const reportQuery = useQuery({
    queryKey: ["reports", "sim-report", session.session_id],
    queryFn: () => simulationApi.report(session.session_id),
  });
  const positionsQuery = useQuery({
    queryKey: ["reports", "sim-positions", session.session_id],
    queryFn: () => simulationApi.positions(session.session_id),
  });

  if (reportQuery.isLoading) return <LoadingState rows={6} />;
  if (reportQuery.isError) {
    return (
      <ErrorState
        message={errorMessage(reportQuery.error, "无法加载模拟会话报告")}
        onRetry={() => reportQuery.refetch()}
      />
    );
  }

  const report = reportQuery.data;
  if (!report) return null;

  const metrics: ReportMetrics = {
    total_return: report.total_return,
    annual_return: report.annual_return,
    sharpe_ratio: report.sharpe_ratio,
    max_drawdown: report.max_drawdown,
    win_rate: report.win_rate,
    total_trades: report.total_trades,
  };
  const equityCurve: EquityPoint[] = report.equity_curve ?? [];
  const positions = positionsQuery.data ?? [];

  return (
    <ReportContent
      metrics={metrics}
      equityCurve={equityCurve}
      extras={[]}
      sourceHref="/research/simulation"
      sourceLabel="模拟会话"
      sourceId={session.session_id}
      meta={
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <StatusBadge status={session.status} />
          <Badge variant="info" className="font-mono">
            {session.source_mode}
          </Badge>
          <span className="font-mono">
            {session.strategy_id}@v{session.strategy_version}
          </span>
          {session.source_run_id && (
            <>
              <span>·</span>
              <span className="font-mono">来源 {session.source_run_id}</span>
            </>
          )}
          <span>·</span>
          <span className="tabular-nums">
            创建 {timeAgo(session.created_at)}
          </span>
        </div>
      }
      extra={
        positionsQuery.isLoading ? (
          <LoadingState rows={3} />
        ) : positionsQuery.isError ? (
          <ErrorState
            message={errorMessage(positionsQuery.error, "无法加载持仓快照")}
            onRetry={() => positionsQuery.refetch()}
          />
        ) : (
          <PositionsSnapshotTable rows={positions} />
        )
      }
    />
  );
}

export default function Reports() {
  const [source, setSource] = useState<ReportSource>("run");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(
    null,
  );

  const runsQuery = useQuery({
    queryKey: ["reports", "runs"],
    queryFn: () => researchRunApi.list({ status: ["completed"], limit: 100 }),
  });
  const sessionsQuery = useQuery({
    queryKey: ["reports", "sessions"],
    queryFn: () => simulationApi.listSessions({ limit: 50 }),
  });

  const completedRuns = useMemo(() => {
    const all = runsQuery.data ?? [];
    return all.filter((r) => r.status === "completed");
  }, [runsQuery.data]);

  const reportableSessions = useMemo(() => {
    const all = sessionsQuery.data ?? [];
    return all.filter((s) => SIM_REPORT_STATUSES.includes(s.status));
  }, [sessionsQuery.data]);

  const handleSourceChange = (v: string) => {
    setSource(v as ReportSource);
    setSelectedRunId(null);
    setSelectedSessionId(null);
  };

  const selectedSession = reportableSessions.find(
    (s) => s.session_id === selectedSessionId,
  );

  return (
    <div>
      <PageHeader
        title="研究报告"
        description="聚合展示运行结果、绩效归因与风险概览"
        breadcrumbs={[
          { label: "研究", href: "/research" },
          { label: "研究报告" },
        ]}
      />
      <WorkflowIndicator currentPath="/research/reports" />

      <Card className="mb-4">
        <CardHeader className="pb-3">
          <CardTitle className="text-base">数据源</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap items-end gap-4">
            <div className="space-y-1.5">
              <p className="text-xs font-medium text-muted-foreground">
                来源类型
              </p>
              <Select value={source} onValueChange={handleSourceChange}>
                <SelectTrigger className="h-9 w-[180px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SOURCE_OPTIONS.map((opt) => (
                    <SelectItem key={opt.value} value={opt.value}>
                      {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {source === "run" ? (
              <div className="min-w-[260px] flex-1 space-y-1.5">
                <p className="text-xs font-medium text-muted-foreground">
                  研究运行（已完成）
                </p>
                <Select
                  value={selectedRunId ?? undefined}
                  onValueChange={setSelectedRunId}
                >
                  <SelectTrigger className="h-9 w-full">
                    <SelectValue placeholder="选择一个已完成的研究运行" />
                  </SelectTrigger>
                  <SelectContent>
                    {completedRuns.length === 0 ? (
                      <SelectItem value="__none__" disabled>
                        暂无已完成的运行
                      </SelectItem>
                    ) : (
                      completedRuns.map((r: ResearchRunSummary) => (
                        <SelectItem key={r.run_id} value={r.run_id}>
                          <span className="font-mono">{r.strategy_id}</span>
                          <span className="text-muted-foreground">
                            · v{runVersion(r) ?? "—"} · {r.run_id.slice(0, 10)}…
                          </span>
                        </SelectItem>
                      ))
                    )}
                  </SelectContent>
                </Select>
              </div>
            ) : (
              <div className="min-w-[260px] flex-1 space-y-1.5">
                <p className="text-xs font-medium text-muted-foreground">
                  模拟会话（已停止 / 已归档）
                </p>
                <Select
                  value={selectedSessionId ?? undefined}
                  onValueChange={setSelectedSessionId}
                >
                  <SelectTrigger className="h-9 w-full">
                    <SelectValue placeholder="选择一个已停止或已归档的模拟会话" />
                  </SelectTrigger>
                  <SelectContent>
                    {reportableSessions.length === 0 ? (
                      <SelectItem value="__none__" disabled>
                        暂无可报告的会话
                      </SelectItem>
                    ) : (
                      reportableSessions.map((s) => (
                        <SelectItem key={s.session_id} value={s.session_id}>
                          <span className="font-mono">{s.strategy_id}</span>
                          <span className="text-muted-foreground">
                            · {s.status} · {s.session_id.slice(0, 10)}…
                          </span>
                        </SelectItem>
                      ))
                    )}
                  </SelectContent>
                </Select>
              </div>
            )}
          </div>

          {source === "run" && runsQuery.isError && (
            <p className="mt-3 text-sm text-destructive">
              {errorMessage(runsQuery.error, "无法加载研究运行列表")}
            </p>
          )}
          {source === "simulation" && sessionsQuery.isError && (
            <p className="mt-3 text-sm text-destructive">
              {errorMessage(sessionsQuery.error, "无法加载模拟会话列表")}
            </p>
          )}
        </CardContent>
      </Card>

      {source === "run" ? (
        selectedRunId ? (
          <RunReportView runId={selectedRunId} />
        ) : (
          <EmptyState
            icon={<FileText className="h-8 w-8" />}
            title="请选择一个已完成的研究运行"
            description={completedRuns.length === 0
              ? "还没有已完成运行。先在「研究运行」登记任务，并由离线 worker 完成后再生成报告。"
              : "选中后将展示绩效指标、权益曲线、回撤分析与交易明细摘要。"}
            action={completedRuns.length === 0 ? <Button asChild variant="outline" size="sm"><Link to="/research/runs">去研究运行</Link></Button> : undefined}
          />
        )
      ) : selectedSession ? (
        <SimulationReportView session={selectedSession} />
      ) : (
        <EmptyState
          icon={<Activity className="h-8 w-8" />}
          title="请选择一个模拟会话"
          description={reportableSessions.length === 0
            ? "还没有已停止或已归档会话。先完成模拟会话，再停止或归档后生成报告。"
            : "仅展示已停止或已归档的会话报告，选中后可查看绩效、权益曲线与持仓快照。"}
          action={reportableSessions.length === 0 ? <Button asChild variant="outline" size="sm"><Link to="/research/simulation">去模拟盘</Link></Button> : undefined}
        />
      )}
    </div>
  );
}
