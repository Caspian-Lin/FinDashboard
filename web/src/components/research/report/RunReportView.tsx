/**
 * 共享报告套件(从 pages/research/Reports.tsx 抽出):
 * 类型 / 指标工具 / 权益与回撤图表 / 指标与成交摘要渲染件 / ReportContent,
 * 以及按 runId 自取数据的 RunReportView(Reports 聚合页与研究运行详情页签共用)。
 */
import { type ReactNode, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  ArrowLeft,
  BarChart3,
  Download,
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
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { StatCard } from "@/components/ui/stat-card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import {
  researchRunApi,
  type ResearchRunSummary,
} from "@/lib/research";
import {
  cn,
  formatCurrency,
  formatNumber,
  formatPercent,
  timeAgo,
} from "@/lib/utils";
import { useT, type LocalizedText } from "@/i18n";

export interface EquityPoint {
  timestamp: string;
  equity: number;
}

export interface ReportMetrics {
  total_return: number | null;
  annual_return: number | null;
  sharpe_ratio: number | null;
  max_drawdown: number | null;
  win_rate: number | null;
  total_trades: number;
}

export interface ExtraMetric {
  label: string;
  value: number;
  format: "percent" | "number";
}

const EXTRA_METRIC_DEFS: {
  key: string;
  label: LocalizedText;
  format: "percent" | "number";
}[] = [
  { key: "profit_factor", label: { zh: "盈亏比", en: "Profit factor" }, format: "number" },
  { key: "sortino_ratio", label: { zh: "Sortino", en: "Sortino" }, format: "number" },
  { key: "calmar_ratio", label: { zh: "Calmar", en: "Calmar" }, format: "number" },
  { key: "volatility", label: { zh: "年化波动率", en: "Annualized volatility" }, format: "percent" },
  { key: "avg_win", label: { zh: "平均盈利", en: "Average win" }, format: "percent" },
  { key: "avg_loss", label: { zh: "平均亏损", en: "Average loss" }, format: "percent" },
  { key: "max_runup", label: { zh: "最大涨幅", en: "Max runup" }, format: "percent" },
  { key: "longest_win_streak", label: { zh: "最长连胜", en: "Longest win streak" }, format: "number" },
  { key: "longest_loss_streak", label: { zh: "最长连亏", en: "Longest loss streak" }, format: "number" },
];

export function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

export function pnlColor(v: number | null | undefined): string {
  if (v === null || v === undefined) return "text-muted-foreground";
  if (v > 0) return "text-success";
  if (v < 0) return "text-destructive";
  return "text-muted-foreground";
}

export function runVersion(run: ResearchRunSummary): number | null {
  if (typeof run.strategy_version === "number") return run.strategy_version;
  const frozenVersion = run.manifest?.strategy_version;
  if (typeof frozenVersion === "number") return frozenVersion;
  const legacyVersion = run.manifest?.strategy_spec_version;
  return typeof legacyVersion === "number" ? legacyVersion : null;
}

export function runCapital(run: ResearchRunSummary): number | null {
  if (typeof run.initial_capital === "number") return run.initial_capital;
  const value = Number(run.manifest?.initial_capital);
  return Number.isFinite(value) ? value : null;
}

export function shortDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

function fullDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
}

function monthYearDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit" });
}

const DAY_MS = 24 * 3600 * 1000;

/**
 * 序列跨度超过一年时,横轴刻度必须带年份:纯「月/日」刻度会把 11 年的
 * 2015/03 → 2026/09 读成「03/10 → 09/04」的倒序半年区间(用户走查实测误读)。
 */
export function spansOverAYear(timestamps: string[]): boolean {
  if (timestamps.length < 2) return false;
  const first = new Date(timestamps[0]).getTime();
  const last = new Date(timestamps[timestamps.length - 1]).getTime();
  if (!Number.isFinite(first) || !Number.isFinite(last)) return false;
  return last - first > 366 * DAY_MS;
}

export function getMetric(
  result: Record<string, unknown> | undefined,
  key: string | string[],
  defaultVal?: number,
): number;
export function getMetric(
  result: Record<string, unknown> | undefined,
  key: string | string[],
  defaultVal: null,
): number | null;
export function getMetric(
  result: Record<string, unknown> | undefined,
  key: string | string[],
  defaultVal: number | null = 0,
): number | null {
  if (!result) return defaultVal;
  // 研究运行 result 的字段名与报告指标不同源:总收益/年化/成交笔数
  // 在后端叫 strategy_return / annualized_return / fill_count,按序取别名。
  const keys = typeof key === "string" ? [key] : key;
  for (const k of keys) {
    const direct = result[k];
    if (typeof direct === "number") return direct;
    const metrics = result["metrics"];
    if (metrics && typeof metrics === "object") {
      const nested = (metrics as Record<string, unknown>)[k];
      if (typeof nested === "number") return nested;
    }
  }
  return defaultVal;
}

export function hasMetric(
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

export function getEquityCurve(
  result: Record<string, unknown> | undefined,
): EquityPoint[] {
  if (!result) return [];
  const candidate =
    result["equity_curve"] ?? result["equity"] ?? result["curve"];
  if (!Array.isArray(candidate)) return [];
  const out: EquityPoint[] = [];
  for (const p of candidate) {
    if (!p || typeof p !== "object") continue;
    const rec = p as Record<string, unknown>;
    // 研究运行报告的权益点序列化为 {trade_date, equity:"<Decimal 字符串>"}
    // (contracts.EquityPoint asdict + Decimal→str),与回测/模拟的
    // {timestamp, equity:number} 形态并存,两种都接受。
    const timestamp = rec["timestamp"] ?? rec["trade_date"];
    const rawEquity = rec["equity"];
    const equity =
      typeof rawEquity === "number"
        ? rawEquity
        : typeof rawEquity === "string"
          ? Number(rawEquity)
          : NaN;
    if (typeof timestamp === "string" && Number.isFinite(equity)) {
      out.push({ timestamp, equity });
    }
  }
  return out;
}

export function computeDrawdownSeries(curve: EquityPoint[]): {
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
    <div className="rounded-md border border-border bg-popover px-3 py-2 text-xs">
      <p className="text-muted-foreground">
        {typeof label === "string" ? fullDate(label) : label}
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
  const { tl } = useT();
  if (!active || !payload || payload.length === 0) return null;
  return (
    <div className="rounded-md border border-border bg-popover px-3 py-2 text-xs">
      <p className="text-muted-foreground">
        {typeof label === "string" ? fullDate(label) : label}
      </p>
      {payload.map((p, i) => {
        const raw = p.value;
        const num = typeof raw === "number" ? raw : Number(raw);
        return (
          <p
            key={i}
            className="font-mono tabular-nums text-destructive"
          >
            {tl({ zh: "回撤", en: "Drawdown" })}: {Number.isFinite(num) ? formatPercent(num) : "—"}
          </p>
        );
      })}
    </div>
  );
}

export function EquityChart({ data }: { data: EquityPoint[] }) {
  const { tl } = useT();
  const tickFormatter = useMemo(
    () => (spansOverAYear(data.map((p) => p.timestamp)) ? monthYearDate : shortDate),
    [data],
  );
  if (data.length === 0) {
    return (
      <EmptyState
        icon={<LineChartIcon className="h-8 w-8" />}
        title={tl({ zh: "暂无权益数据", en: "No equity data" })}
        description={tl({ zh: "该运行尚未生成权益曲线。", en: "This run has not generated an equity curve yet." })}
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
            tickFormatter={tickFormatter}
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
            name={tl({ zh: "权益", en: "Equity" })}
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

export function DrawdownChart({
  data,
}: {
  data: { timestamp: string; drawdown: number }[];
}) {
  const { tl } = useT();
  const tickFormatter = useMemo(
    () => (spansOverAYear(data.map((p) => p.timestamp)) ? monthYearDate : shortDate),
    [data],
  );
  if (data.length === 0) {
    return (
      <EmptyState
        icon={<TrendingDown className="h-8 w-8" />}
        title={tl({ zh: "暂无回撤数据", en: "No drawdown data" })}
        description={tl({ zh: "权益点数不足，无法计算回撤序列。", en: "Not enough equity points to compute the drawdown series." })}
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
            tickFormatter={tickFormatter}
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
            name={tl({ zh: "回撤", en: "Drawdown" })}
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

export function MetricsGrid({ metrics }: { metrics: ReportMetrics }) {
  const { tl } = useT();
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
      <StatCard
        label={tl({ zh: "总收益", en: "Total return" })}
        icon={TrendingUp}
        value={
          <span className={cn("font-mono tabular-nums", pnlColor(metrics.total_return))}>
            {formatPercent(metrics.total_return)}
          </span>
        }
      />
      <StatCard
        label={tl({ zh: "年化收益", en: "Annual return" })}
        icon={TrendingUp}
        value={
          <span className={cn("font-mono tabular-nums", pnlColor(metrics.annual_return))}>
            {formatPercent(metrics.annual_return)}
          </span>
        }
      />
      <StatCard
        label={tl({ zh: "夏普比率", en: "Sharpe ratio" })}
        icon={Gauge}
        value={
          <span className={cn("font-mono tabular-nums", pnlColor(metrics.sharpe_ratio))}>
            {formatNumber(metrics.sharpe_ratio, 3)}
          </span>
        }
      />
      <StatCard
        label={tl({ zh: "最大回撤", en: "Max drawdown" })}
        icon={TrendingDown}
        value={
          <span className="font-mono tabular-nums text-destructive">
            {formatPercent(metrics.max_drawdown === null ? null : -metrics.max_drawdown)}
          </span>
        }
        hint={metrics.max_drawdown !== null && metrics.max_drawdown > 0 ? tl({ zh: "峰值至谷值", en: "Peak to trough" }) : undefined}
      />
      <StatCard
        label={tl({ zh: "胜率", en: "Win rate" })}
        icon={Target}
        value={
          <span className="font-mono tabular-nums">
            {formatPercent(metrics.win_rate)}
          </span>
        }
      />
      <StatCard
        label={tl({ zh: "总成交笔数", en: "Total trades" })}
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

export function TradeSummary({ extras }: { extras: ExtraMetric[] }) {
  const { tl } = useT();
  if (extras.length === 0) {
    return (
      <EmptyState
        icon={<Layers className="h-8 w-8" />}
        title={tl({ zh: "无额外交易明细", en: "No extra trade metrics" })}
        description={tl({ zh: "该运行结果未提供更多交易统计指标。", en: "This run result provides no additional trade statistics." })}
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

export interface ReportContentProps {
  metrics: ReportMetrics;
  equityCurve: EquityPoint[];
  extras: ExtraMetric[];
  sourceHref: string;
  sourceLabel: string;
  sourceId: string;
  meta?: ReactNode;
  extra?: ReactNode;
  /** 报告导出下载基础路径(#157;仅 run/backtest 报告提供,模拟盘无导出)。 */
  exportBasePath?: string;
}

export function ReportContent({
  metrics,
  equityCurve,
  extras,
  sourceHref,
  sourceLabel,
  sourceId,
  meta,
  extra,
  exportBasePath,
}: ReportContentProps) {
  const { tl } = useT();
  const { series, maxDrawdown } = useMemo(
    () => computeDrawdownSeries(equityCurve),
    [equityCurve],
  );
  const realizedMaxDrawdown =
    maxDrawdown > 0 ? maxDrawdown : metrics.max_drawdown;
  // 运行区间:权益曲线首末点即策略起止交易日,直接可见省得从刻度反推。
  const curveInterval = useMemo(() => {
    if (equityCurve.length < 2) return null;
    return {
      range: `${fullDate(equityCurve[0].timestamp)} ~ ${fullDate(
        equityCurve[equityCurve.length - 1].timestamp,
      )}`,
      points: formatNumber(equityCurve.length, 0),
    };
  }, [equityCurve]);

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
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              {exportBasePath && (
                <>
                  <Button asChild variant="outline" size="sm">
                    <a
                      href={`${exportBasePath}?format=csv`}
                      download
                      title={tl({ zh: "导出 CSV(元信息 + 指标 + 权益曲线 + 成交,Excel 兼容)", en: "Export CSV (metadata + metrics + equity curve + trades, Excel compatible)" })}
                    >
                      <Download className="h-4 w-4" />
                      CSV
                    </a>
                  </Button>
                  <Button asChild variant="outline" size="sm">
                    <a
                      href={`${exportBasePath}?format=markdown`}
                      download
                      title={tl({ zh: "导出 Markdown 报告", en: "Export Markdown report" })}
                    >
                      <Download className="h-4 w-4" />
                      Markdown
                    </a>
                  </Button>
                </>
              )}
              <Button asChild variant="outline" size="sm">
                <Link to={sourceHref}>
                  <ArrowLeft className="h-4 w-4" />
                  {tl({ zh: "查看原始", en: "View " })}{sourceLabel}
                </Link>
              </Button>
            </div>
          </div>
        </CardHeader>
      </Card>

      <div>
        <h3 className="mb-2 text-sm font-semibold text-foreground">
          {tl({ zh: "绩效指标", en: "Performance metrics" })}
        </h3>
        <MetricsGrid metrics={metrics} />
      </div>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle className="text-base">{tl({ zh: "权益曲线", en: "Equity curve" })}</CardTitle>
            {curveInterval && (
              <p className="text-xs tabular-nums text-muted-foreground">
                {curveInterval.range}
                {" · "}
                {tl({
                  zh: `${curveInterval.points} 交易日`,
                  en: `${curveInterval.points} trading days`,
                })}
              </p>
            )}
          </div>
        </CardHeader>
        <CardContent>
          <EquityChart data={equityCurve} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">{tl({ zh: "回撤分析", en: "Drawdown analysis" })}</CardTitle>
            <div className="text-right">
              <p className="text-xs text-muted-foreground">{tl({ zh: "最大回撤", en: "Max drawdown" })}</p>
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
          {tl({ zh: "交易明细摘要", en: "Trade summary" })}
        </h3>
        <TradeSummary extras={extras} />
      </div>

      {extra && (
        <>
          <Separator />
          <div>
            <h3 className="mb-2 text-sm font-semibold text-foreground">
              {tl({ zh: "持仓快照", en: "Position snapshot" })}
            </h3>
            {extra}
          </div>
        </>
      )}
    </div>
  );
}

/**
 * 按研究运行 ID 渲染完整绩效报告;数据由组件内部自取
 * (与 Reports 聚合页共用同一 query key,缓存互通)。
 */
export function RunReportView({ runId }: { runId: string }) {
  const { tl, lang } = useT();
  const detailQuery = useQuery({
    queryKey: ["reports", "run-detail", runId],
    queryFn: () => researchRunApi.get(runId),
  });

  if (detailQuery.isLoading) return <LoadingState rows={6} />;
  if (detailQuery.isError) {
    return (
      <ErrorState
        message={errorMessage(detailQuery.error, tl({ zh: "无法加载研究运行结果", en: "Failed to load research run result" }))}
        onRetry={() => detailQuery.refetch()}
      />
    );
  }

  const detail = detailQuery.data;
  if (!detail) return null;
  const result = detail.result;

  const metrics: ReportMetrics = {
    total_return: getMetric(result, ["total_return", "strategy_return"]),
    annual_return: getMetric(result, ["annual_return", "annualized_return"]),
    sharpe_ratio: getMetric(result, "sharpe_ratio"),
    max_drawdown: getMetric(result, "max_drawdown"),
    // 研究运行报告没有胜率字段,缺数据显示「—」而非误导读成 0.00%。
    win_rate: getMetric(result, "win_rate", null),
    total_trades: getMetric(result, ["total_trades", "fill_count"]),
  };
  const equityCurve = getEquityCurve(result);
  const extras: ExtraMetric[] = EXTRA_METRIC_DEFS.filter((d) =>
    hasMetric(result, d.key),
  ).map((d) => ({
    label: tl(d.label),
    value: getMetric(result, d.key),
    format: d.format,
  }));

  return (
    <ReportContent
      metrics={metrics}
      equityCurve={equityCurve}
      extras={extras}
      sourceHref={`/research/runs?run=${encodeURIComponent(detail.run_id)}`}
      sourceLabel={tl({ zh: "研究运行", en: "research run" })}
      sourceId={detail.run_id}
      exportBasePath={`/api/research/runs/${encodeURIComponent(detail.run_id)}/report/export`}
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
            {tl({ zh: "初始资金", en: "Initial capital" })} ¥{formatCurrency(runCapital(detail), 0)}
          </span>
          {detail.completed_at && (
            <>
              <span>·</span>
              <span className="tabular-nums">
                {tl({ zh: "完成", en: "Completed" })} {timeAgo(detail.completed_at, lang)}
              </span>
            </>
          )}
        </div>
      }
    />
  );
}
