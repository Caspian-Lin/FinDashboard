import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Activity, FileText } from "lucide-react";
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
import { cn, formatCurrency, formatNumber, timeAgo } from "@/lib/utils";
import { useT, type LocalizedText } from "@/i18n";
import {
  errorMessage,
  pnlColor,
  runVersion,
  shortDate,
  ReportContent,
  RunReportView,
  type EquityPoint,
  type ReportMetrics,
} from "@/components/research/report/RunReportView";

type ReportSource = "run" | "simulation";

const SOURCE_OPTIONS: { value: ReportSource; label: LocalizedText }[] = [
  { value: "run", label: { zh: "研究运行", en: "Research runs" } },
  { value: "simulation", label: { zh: "模拟会话", en: "Simulation sessions" } },
];

const SIM_REPORT_STATUSES = ["stopped", "archived"];

function PositionsSnapshotTable({ rows }: { rows: SimulationPosition[] }) {
  const { tl } = useT();
  if (rows.length === 0) {
    return (
      <EmptyState
        title={tl({ zh: "暂无持仓快照", en: "No position snapshot" })}
        description={tl({ zh: "该模拟会话当前没有任何持仓。", en: "This simulation session currently has no positions." })}
      />
    );
  }
  return (
    <div className="rounded-lg border border-border">
      <Table>
        <TableHeader className="sticky top-0 bg-card">
          <TableRow>
            <TableHead>{tl({ zh: "标的", en: "Symbol" })}</TableHead>
            <TableHead>{tl({ zh: "方向", en: "Side" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "数量", en: "Quantity" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "成本", en: "Avg cost" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "市价", en: "Market price" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "市值", en: "Market value" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "浮动盈亏", en: "Unrealized PnL" })}</TableHead>
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

function SimulationReportView({
  session,
}: {
  session: SimulationSession;
}) {
  const { tl, lang } = useT();
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
        message={errorMessage(reportQuery.error, tl({ zh: "无法加载模拟会话报告", en: "Failed to load simulation session report" }))}
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
      sourceLabel={tl({ zh: "模拟会话", en: "simulation session" })}
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
              <span className="font-mono">{tl({ zh: "来源", en: "From" })} {session.source_run_id}</span>
            </>
          )}
          <span>·</span>
          <span className="tabular-nums">
            {tl({ zh: "创建", en: "Created" })} {timeAgo(session.created_at, lang)}
          </span>
        </div>
      }
      extra={
        positionsQuery.isLoading ? (
          <LoadingState rows={3} />
        ) : positionsQuery.isError ? (
          <ErrorState
            message={errorMessage(positionsQuery.error, tl({ zh: "无法加载持仓快照", en: "Failed to load position snapshot" }))}
            onRetry={() => positionsQuery.refetch()}
          />
        ) : (
          <PositionsSnapshotTable rows={positions} />
        )
      }
    />
  );
}

// 研究运行报告改为复用共享 RunReportView(亦嵌入研究运行详情「绩效报告」页签),
// 本页保留聚合入口定位:选择来源 + 模拟会话报告。
export default function Reports() {
  const { tl } = useT();
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
        title={tl({ zh: "研究报告", en: "Research report" })}
        description={tl({ zh: "聚合展示运行结果、绩效归因与风险概览", en: "Aggregated run results, performance attribution and risk overview" })}
      />

      <Card className="mb-4">
        <CardHeader className="pb-3">
          <CardTitle className="text-base">{tl({ zh: "数据源", en: "Data source" })}</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap items-end gap-4">
            <div className="space-y-1.5">
              <p className="text-xs font-medium text-muted-foreground">
                {tl({ zh: "来源类型", en: "Source type" })}
              </p>
              <Select value={source} onValueChange={handleSourceChange}>
                <SelectTrigger className="h-9 w-[180px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SOURCE_OPTIONS.map((opt) => (
                    <SelectItem key={opt.value} value={opt.value}>
                      {tl(opt.label)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {source === "run" ? (
              <div className="min-w-[260px] flex-1 space-y-1.5">
                <p className="text-xs font-medium text-muted-foreground">
                  {tl({ zh: "研究运行（已完成）", en: "Research runs (completed)" })}
                </p>
                <Select
                  value={selectedRunId ?? undefined}
                  onValueChange={setSelectedRunId}
                >
                  <SelectTrigger className="h-9 w-full">
                    <SelectValue placeholder={tl({ zh: "选择一个已完成的研究运行", en: "Select a completed research run" })} />
                  </SelectTrigger>
                  <SelectContent>
                    {completedRuns.length === 0 ? (
                      <SelectItem value="__none__" disabled>
                        {tl({ zh: "暂无已完成的运行", en: "No completed runs yet" })}
                      </SelectItem>
                    ) : (
                      completedRuns.map((r: ResearchRunSummary) => (
                        <SelectItem key={r.run_id} value={r.run_id}>
                          <span className="font-mono">{r.strategy_id}</span>
                          <span className="text-muted-foreground">
                            · v{runVersion(r) ?? "—"} · {tl({ zh: "完成", en: "done" })}{" "}
                            {shortDate(r.completed_at ?? r.created_at)} ·{" "}
                            {r.run_id.slice(0, 10)}…
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
                  {tl({ zh: "模拟会话（已停止 / 已归档）", en: "Simulation sessions (stopped / archived)" })}
                </p>
                <Select
                  value={selectedSessionId ?? undefined}
                  onValueChange={setSelectedSessionId}
                >
                  <SelectTrigger className="h-9 w-full">
                    <SelectValue placeholder={tl({ zh: "选择一个已停止或已归档的模拟会话", en: "Select a stopped or archived simulation session" })} />
                  </SelectTrigger>
                  <SelectContent>
                    {reportableSessions.length === 0 ? (
                      <SelectItem value="__none__" disabled>
                        {tl({ zh: "暂无可报告的会话", en: "No reportable sessions yet" })}
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
              {errorMessage(runsQuery.error, tl({ zh: "无法加载研究运行列表", en: "Failed to load research run list" }))}
            </p>
          )}
          {source === "simulation" && sessionsQuery.isError && (
            <p className="mt-3 text-sm text-destructive">
              {errorMessage(sessionsQuery.error, tl({ zh: "无法加载模拟会话列表", en: "Failed to load simulation session list" }))}
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
            title={tl({ zh: "请选择一个已完成的研究运行", en: "Select a completed research run" })}
            description={completedRuns.length === 0
              ? tl({ zh: "还没有已完成运行。先在「研究运行」登记任务，并由离线 worker 完成后再生成报告。", en: "No completed runs yet. Register a task under \"Research runs\" first; a report is generated after the offline worker completes it." })
              : tl({ zh: "选中后将展示绩效指标、权益曲线、回撤分析与交易明细摘要。", en: "Once selected, performance metrics, equity curve, drawdown analysis and trade summary are shown." })}
            action={completedRuns.length === 0 ? <Button asChild variant="outline" size="sm"><Link to="/research/runs">{tl({ zh: "去研究运行", en: "Go to research runs" })}</Link></Button> : undefined}
          />
        )
      ) : selectedSession ? (
        <SimulationReportView session={selectedSession} />
      ) : (
        <EmptyState
          icon={<Activity className="h-8 w-8" />}
          title={tl({ zh: "请选择一个模拟会话", en: "Select a simulation session" })}
          description={reportableSessions.length === 0
            ? tl({ zh: "还没有已停止或已归档会话。先完成模拟会话，再停止或归档后生成报告。", en: "No stopped or archived sessions yet. Complete a simulation session, then stop or archive it to generate a report." })
            : tl({ zh: "仅展示已停止或已归档的会话报告，选中后可查看绩效、权益曲线与持仓快照。", en: "Only stopped or archived session reports are shown; select one to view performance, equity curve and position snapshot." })}
          action={reportableSessions.length === 0 ? <Button asChild variant="outline" size="sm"><Link to="/research/simulation">{tl({ zh: "去模拟盘", en: "Go to simulation" })}</Link></Button> : undefined}
        />
      )}
    </div>
  );
}
