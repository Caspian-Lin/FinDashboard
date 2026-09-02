import { WorkflowIndicator, NextStepCTA } from "@/components/research/ResearchHint";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { useT } from "@/i18n";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Archive,
  FlaskConical,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Square,
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
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import {
  simulationApi,
  type SimulationAccount,
  type SimulationFill,
  type SimulationLedgerEntry,
  type SimulationOrder,
  type SimulationPosition,
  type SimulationProcessResult,
  type SimulationReport,
  type SimulationSession,
} from "@/lib/simulation";
import {
  cn,
  formatCurrency,
  formatDateTime,
  formatNumber,
  formatPercent,
  timeAgo,
} from "@/lib/utils";

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

function pnlColor(v: number | null | undefined): string {
  if (v === null || v === undefined) return "text-muted-foreground";
  if (v > 0) return "text-success";
  if (v < 0) return "text-destructive";
  return "text-muted-foreground";
}

function SideBadge({ side }: { side: string }) {
  const isBuy = side.toLowerCase() === "buy";
  return (
    <Badge
      variant={isBuy ? "success" : "destructive"}
      className="font-mono uppercase"
    >
      {side}
    </Badge>
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

const SIMULATION_TARGET_RULES = [
  { key: "equity_etf", label: { zh: "股票 ETF（100 份/手）", en: "Stock ETF (100 shares/lot)" }, instrumentType: "etf" },
  { key: "a_share_stock", label: { zh: "A 股股票（100 股/手）", en: "A-share stock (100 shares/lot)" }, instrumentType: "stock" },
  { key: "cross_border_etf", label: { zh: "跨境 ETF（T+0）", en: "Cross-border ETF (T+0)" }, instrumentType: "etf" },
  { key: "bond_etf", label: { zh: "债券 ETF（10 份/手）", en: "Bond ETF (10 shares/lot)" }, instrumentType: "etf" },
  { key: "money_market_etf", label: { zh: "货币 ETF（T+0）", en: "Money market ETF (T+0)" }, instrumentType: "etf" },
] as const;

function MetricCard({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: string;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className={cn("mt-1 font-mono text-lg tabular-nums", accent)}>
          {value}
        </p>
      </CardContent>
    </Card>
  );
}

function DataTableShell({
  isLoading,
  isError,
  error,
  onRetry,
  isEmpty,
  emptyTitle,
  emptyDescription,
  children,
}: {
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  onRetry: () => void;
  isEmpty: boolean;
  emptyTitle: string;
  emptyDescription?: string;
  children: ReactNode;
}) {
  const { tl } = useT();
  if (isLoading) return <LoadingState rows={4} />;
  if (isError)
    return (
      <ErrorState
        message={errorMessage(error, tl({ zh: "数据加载失败", en: "Failed to load data" }))}
        onRetry={onRetry}
      />
    );
  if (isEmpty)
    return (
      <EmptyState
        icon={<Activity className="h-8 w-8" />}
        title={emptyTitle}
        description={emptyDescription}
      />
    );
  return <>{children}</>;
}

function SessionDetail({
  session,
  onClear,
}: {
  session: SimulationSession;
  onClear: () => void;
}) {
  const { tl } = useT();
  const queryClient = useQueryClient();
  const [actionActor, setActionActor] = useState("console");
  const [tab, setTab] = useState("positions");
  const [decisionOpen, setDecisionOpen] = useState(false);
  const [marketEventOpen, setMarketEventOpen] = useState(false);
  const [decisionId, setDecisionId] = useState("");
  const [sourceDecisionId, setSourceDecisionId] = useState("");
  const [signalTraceId, setSignalTraceId] = useState("");
  const [targetSymbol, setTargetSymbol] = useState("510300.SH");
  const [targetRuleKey, setTargetRuleKey] = useState("equity_etf");
  const [targetQuantity, setTargetQuantity] = useState("0");
  const [targetReason, setTargetReason] = useState("");
  const [decisionResult, setDecisionResult] = useState<{
    orderCount: number;
    duplicate: boolean;
  } | null>(null);
  const [marketEventResult, setMarketEventResult] =
    useState<SimulationProcessResult | null>(null);

  const detailQuery = useQuery({
    queryKey: ["simulation", "session", session.session_id],
    queryFn: () => simulationApi.sessionDetail(session.session_id),
    initialData: session,
  });

  const positionsQuery = useQuery({
    queryKey: ["simulation", "positions", session.session_id],
    queryFn: () => simulationApi.positions(session.session_id),
  });
  const ordersQuery = useQuery({
    queryKey: ["simulation", "orders", session.session_id],
    queryFn: () => simulationApi.orders(session.session_id),
  });
  const fillsQuery = useQuery({
    queryKey: ["simulation", "fills", session.session_id],
    queryFn: () => simulationApi.fills(session.session_id),
  });
  const ledgerQuery = useQuery({
    queryKey: ["simulation", "ledger", session.session_id],
    queryFn: () => simulationApi.ledger(session.session_id),
  });
  const reportQuery = useQuery({
    queryKey: ["simulation", "report", session.session_id],
    queryFn: () => simulationApi.report(session.session_id),
    enabled: tab === "report",
  });

  const invalidateSession = () => {
    for (const queryKey of [
      ["simulation", "sessions"],
      ["simulation", "session", session.session_id],
      ["simulation", "positions", session.session_id],
      ["simulation", "orders", session.session_id],
      ["simulation", "fills", session.session_id],
      ["simulation", "ledger", session.session_id],
      ["simulation", "report", session.session_id],
    ]) {
      void queryClient.invalidateQueries({ queryKey });
    }
  };

  const startMutation = useMutation({
    mutationFn: () => simulationApi.startSession(session.session_id, actionActor),
    onSuccess: invalidateSession,
  });
  const pauseMutation = useMutation({
    mutationFn: () => simulationApi.pauseSession(session.session_id, actionActor),
    onSuccess: invalidateSession,
  });
  const stopMutation = useMutation({
    mutationFn: () => simulationApi.stopSession(session.session_id, actionActor),
    onSuccess: invalidateSession,
  });
  const archiveMutation = useMutation({
    mutationFn: () =>
      simulationApi.archiveSession(session.session_id, actionActor),
    onSuccess: invalidateSession,
  });

  const detail = detailQuery.data ?? session;
  const status = detail.status;
  const actorInvalid = actionActor.trim() === "";
  const sourceRunId = detail.validation_run_id ?? session.validation_run_id ?? "";
  const targetRule =
    SIMULATION_TARGET_RULES.find((item) => item.key === targetRuleKey) ??
    SIMULATION_TARGET_RULES[0];
  const targetQuantityValue = Number(targetQuantity);
  const decisionValid =
    status === "running" &&
    /^RR-/.test(sourceRunId) &&
    decisionId.trim() !== "" &&
    sourceDecisionId.trim() !== "" &&
    signalTraceId.trim() !== "" &&
    targetSymbol.trim() !== "" &&
    targetReason.trim() !== "" &&
    targetQuantity.trim() !== "" &&
    Number.isFinite(targetQuantityValue) &&
    targetQuantityValue >= 0 &&
    !actorInvalid;

  const decisionMutation = useMutation({
    mutationFn: () =>
      simulationApi.submitDecision(session.session_id, {
        decision_id: decisionId.trim(),
        source_run_id: sourceRunId,
        source_decision_id: sourceDecisionId.trim(),
        actor: actionActor.trim(),
        targets: [
          {
            symbol: targetSymbol.trim(),
            market: "a_share",
            instrument_type: targetRule.instrumentType,
            asset_rule_key: targetRule.key,
            position_side: "long",
            target_quantity: targetQuantityValue,
            signal_trace_id: signalTraceId.trim(),
            reason: targetReason.trim(),
            order_type: "market",
            time_in_force: "GFD",
          },
        ],
      }),
    onSuccess: (result) => {
      setDecisionResult({
        orderCount: result.orders.length,
        duplicate: result.duplicate,
      });
      setDecisionOpen(false);
      setDecisionId("");
      setSourceDecisionId("");
      setSignalTraceId("");
      setTargetReason("");
      invalidateSession();
    },
  });

  const openDecisionDialog = () => {
    if (!decisionId.trim()) {
      setDecisionId(`SIM-DEC-${Date.now()}`);
    }
    setDecisionOpen(true);
  };

  const positions = positionsQuery.data ?? [];
  const orders = ordersQuery.data ?? [];
  const fills = fillsQuery.data ?? [];
  const ledger = ledgerQuery.data ?? [];
  const report = reportQuery.data;

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-base">
              <FlaskConical className="h-4 w-4 text-primary" />
              <span className="font-mono text-sm">{detail.session_id}</span>
            </CardTitle>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <StatusBadge status={status} />
              <span className="font-mono">
                {detail.strategy_id}@v{detail.strategy_version}
              </span>
              <span>·</span>
              <Badge variant="info" className="font-mono">
                {detail.source_mode}
              </Badge>
            </div>
          </div>
          <Button
            variant="ghost"
            size="icon"
            onClick={onClear}
            aria-label={tl({ zh: "取消选择", en: "Clear selection" })}
          >
            <RefreshCw className="h-4 w-4 rotate-180" />
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        <ScrollArea className="max-h-[760px] pr-3">
          <div className="space-y-5">
            <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-4">
              <InfoItem label={tl({ zh: "会话 ID", en: "Session ID" })}>
                <span className="font-mono text-xs">{detail.session_id}</span>
              </InfoItem>
              <InfoItem label={tl({ zh: "策略", en: "Strategy" })}>
                <span className="font-mono text-xs">{detail.strategy_id}</span>
              </InfoItem>
              <InfoItem label={tl({ zh: "版本", en: "Version" })}>
                <span className="font-mono">v{detail.strategy_version}</span>
              </InfoItem>
              <InfoItem label={tl({ zh: "状态", en: "Status" })}>
                <StatusBadge status={status} />
              </InfoItem>
              <InfoItem label={tl({ zh: "数据源模式", en: "Data source mode" })}>
                <Badge variant="info" className="font-mono">
                  {detail.source_mode}
                </Badge>
              </InfoItem>
              <InfoItem label={tl({ zh: "来源运行", en: "Source run" })}>
                <span className="font-mono text-xs">
                  {detail.source_run_id ?? "—"}
                </span>
              </InfoItem>
              <InfoItem label={tl({ zh: "晋级状态", en: "Promotion status" })}>
                <Badge variant="secondary" className="font-mono">
                  {detail.promotion_status}
                </Badge>
              </InfoItem>
              <InfoItem label={tl({ zh: "时钟倍速", en: "Clock speed" })}>
                <span className="font-mono tabular-nums">
                  {formatNumber(detail.clock_speed, 1)}x
                </span>
              </InfoItem>
              <InfoItem label={tl({ zh: "恢复次数", en: "Recovery count" })}>
                <span className="font-mono tabular-nums">
                  {detail.recovery_count}
                </span>
              </InfoItem>
              <InfoItem label={tl({ zh: "创建时间", en: "Created at" })}>
                <span className="tabular-nums">
                  {formatDateTime(detail.created_at)}
                </span>
              </InfoItem>
            </div>

            {detail.data_release_id && (
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <span>{tl({ zh: "数据发布:", en: "Data release:" })}</span>
                <span className="font-mono text-foreground">
                  {detail.data_release_id}
                </span>
              </div>
            )}

            <Separator />

            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <Label htmlFor="action-actor" className="text-xs text-muted-foreground">
                  {tl({ zh: "操作人", en: "Actor" })}
                </Label>
                <Input
                  id="action-actor"
                  value={actionActor}
                  onChange={(e) => setActionActor(e.target.value)}
                  placeholder={tl({ zh: "例如：console", en: "e.g. console" })}
                  className="h-8 w-40 font-mono text-xs"
                />
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {status === "created" && (
                  <Button
                    size="sm"
                    disabled={actorInvalid || startMutation.isPending}
                    onClick={() => startMutation.mutate()}
                  >
                    <Play className="h-4 w-4" />
                    {startMutation.isPending ? tl({ zh: "启动中…", en: "Starting…" }) : tl({ zh: "启动", en: "Start" })}
                  </Button>
                )}
                {status === "running" && (
                  <>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={actorInvalid || pauseMutation.isPending}
                      onClick={() => pauseMutation.mutate()}
                    >
                      <Pause className="h-4 w-4" />
                      {pauseMutation.isPending ? tl({ zh: "暂停中…", en: "Pausing…" }) : tl({ zh: "暂停", en: "Pause" })}
                    </Button>
                    <Button
                      size="sm"
                      variant="destructive"
                      disabled={actorInvalid || stopMutation.isPending}
                      onClick={() => stopMutation.mutate()}
                    >
                      <Square className="h-4 w-4" />
                      {stopMutation.isPending ? tl({ zh: "停止中…", en: "Stopping…" }) : tl({ zh: "停止", en: "Stop" })}
                    </Button>
                  </>
                )}
                {status === "paused" && (
                  <>
                    <Button
                      size="sm"
                      disabled={actorInvalid || startMutation.isPending}
                      onClick={() => startMutation.mutate()}
                    >
                      <Play className="h-4 w-4" />
                      {startMutation.isPending ? tl({ zh: "启动中…", en: "Starting…" }) : tl({ zh: "启动", en: "Start" })}
                    </Button>
                    <Button
                      size="sm"
                      variant="destructive"
                      disabled={actorInvalid || stopMutation.isPending}
                      onClick={() => stopMutation.mutate()}
                    >
                      <Square className="h-4 w-4" />
                      {stopMutation.isPending ? tl({ zh: "停止中…", en: "Stopping…" }) : tl({ zh: "停止", en: "Stop" })}
                    </Button>
                  </>
                )}
                {status === "stopped" && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={actorInvalid || archiveMutation.isPending}
                    onClick={() => archiveMutation.mutate()}
                  >
                    <Archive className="h-4 w-4" />
                    {archiveMutation.isPending ? tl({ zh: "归档中…", en: "Archiving…" }) : tl({ zh: "归档", en: "Archive" })}
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  disabled={status !== "running" || actorInvalid}
                  onClick={openDecisionDialog}
                >
                  <Plus className="h-4 w-4" />
                  {tl({ zh: "提交目标仓位", en: "Submit target position" })}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={status !== "running"}
                  onClick={() => setMarketEventOpen(true)}
                >
                  <Play className="h-4 w-4" />
                  {tl({ zh: "推进一根行情", en: "Advance one bar" })}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    positionsQuery.refetch();
                    ordersQuery.refetch();
                    fillsQuery.refetch();
                    ledgerQuery.refetch();
                    if (tab === "report") reportQuery.refetch();
                  }}
                >
                  <RefreshCw className="h-4 w-4" />
                  {tl({ zh: "刷新数据", en: "Refresh data" })}
                </Button>
              </div>
              {startMutation.isError && (
                <p className="text-sm text-destructive">
                  {errorMessage(startMutation.error, tl({ zh: "启动失败", en: "Failed to start" }))}
                </p>
              )}
              {pauseMutation.isError && (
                <p className="text-sm text-destructive">
                  {errorMessage(pauseMutation.error, tl({ zh: "暂停失败", en: "Failed to pause" }))}
                </p>
              )}
              {stopMutation.isError && (
                <p className="text-sm text-destructive">
                  {errorMessage(stopMutation.error, tl({ zh: "停止失败", en: "Failed to stop" }))}
                </p>
              )}
              {archiveMutation.isError && (
                <p className="text-sm text-destructive">
                  {errorMessage(archiveMutation.error, tl({ zh: "归档失败", en: "Failed to archive" }))}
                </p>
              )}
              {status !== "running" && (
                <p className="text-xs text-muted-foreground">
                  {tl({ zh: "只有 running 会话可以接收目标仓位决策；当前请先点击“启动”。", en: "Only running sessions can receive target position decisions; click “Start” first." })}
                </p>
              )}
              {!sourceRunId && (
                <Alert variant="warning">
                  <AlertTitle>{tl({ zh: "会话缺少机器验证来源", en: "Session lacks a machine-validation source" })}</AlertTitle>
                  <AlertDescription>
                    {tl({ zh: "此会话没有绑定 ", en: "This session is not bound to a completed " })}
                    <code>RR-</code>
                    {tl({ zh: " 完成运行，无法提交模拟决策。请返回研究运行页面，使用已完成运行和冻结数据发布重新创建会话。", en: " run, so simulation decisions cannot be submitted. Go back to the research runs page and recreate the session with a completed run and a frozen data release." })}
                  </AlertDescription>
                </Alert>
              )}
              {decisionMutation.isError && (
                <Alert variant="destructive">
                  <AlertTitle>{tl({ zh: "目标仓位未提交", en: "Target position not submitted" })}</AlertTitle>
                  <AlertDescription>
                    {errorMessage(decisionMutation.error, tl({ zh: "请检查 RR- 来源、来源决策和信号 trace 是否属于同一研究运行", en: "Check that the RR- source, source decision and signal trace belong to the same research run" }))}
                  </AlertDescription>
                </Alert>
              )}
              {decisionResult && (
                <Alert variant="success">
                  <AlertTitle>{tl({ zh: "目标仓位已记录", en: "Target position recorded" })}</AlertTitle>
                  <AlertDescription>
                    {decisionResult.duplicate
                      ? tl({ zh: "检测到相同 decision_id，已幂等返回原结果。", en: "Duplicate decision_id detected; the original result was returned idempotently." })
                      : tl({ zh: `已生成 ${decisionResult.orderCount} 个模拟订单；订单仍需行情回放后才会产生成交。`, en: `Generated ${decisionResult.orderCount} simulated orders; fills are only produced after market event replay.` })}
                  </AlertDescription>
                </Alert>
              )}
              {marketEventResult && (
                <Alert variant="success">
                  <AlertTitle>{tl({ zh: "行情事件已处理", en: "Market event processed" })}</AlertTitle>
                  <AlertDescription>
                    {marketEventResult.duplicate
                      ? tl({ zh: "检测到相同 source_event_id，已幂等返回原结果。", en: "Duplicate source_event_id detected; the original result was returned idempotently." })
                      : tl({ zh: `已处理 ${marketEventResult.source_event_id}；成交 ${marketEventResult.fill_ids.length} 笔，拒单 ${marketEventResult.rejected_order_ids.length} 笔。`, en: `Processed ${marketEventResult.source_event_id}; ${marketEventResult.fill_ids.length} fills, ${marketEventResult.rejected_order_ids.length} rejected orders.` })}
                  </AlertDescription>
                </Alert>
              )}
            </div>

            <Separator />

            <Tabs value={tab} onValueChange={setTab}>
              <TabsList>
                <TabsTrigger value="positions">{tl({ zh: "持仓", en: "Positions" })}</TabsTrigger>
                <TabsTrigger value="orders">{tl({ zh: "订单", en: "Orders" })}</TabsTrigger>
                <TabsTrigger value="fills">{tl({ zh: "成交", en: "Fills" })}</TabsTrigger>
                <TabsTrigger value="ledger">{tl({ zh: "账本", en: "Ledger" })}</TabsTrigger>
                <TabsTrigger value="report">{tl({ zh: "报告", en: "Report" })}</TabsTrigger>
              </TabsList>

              <TabsContent value="positions" className="mt-4">
                <DataTableShell
                  isLoading={positionsQuery.isLoading}
                  isError={positionsQuery.isError}
                  error={positionsQuery.error}
                  onRetry={() => positionsQuery.refetch()}
                  isEmpty={positions.length === 0}
                  emptyTitle={tl({ zh: "暂无持仓", en: "No positions" })}
                  emptyDescription={tl({ zh: "该会话当前没有任何持仓记录。", en: "This session has no position records yet." })}
                >
                  <PositionsTable rows={positions} />
                </DataTableShell>
              </TabsContent>

              <TabsContent value="orders" className="mt-4">
                <DataTableShell
                  isLoading={ordersQuery.isLoading}
                  isError={ordersQuery.isError}
                  error={ordersQuery.error}
                  onRetry={() => ordersQuery.refetch()}
                  isEmpty={orders.length === 0}
                  emptyTitle={tl({ zh: "暂无订单", en: "No orders" })}
                  emptyDescription={tl({ zh: "该会话尚未生成任何模拟订单。", en: "No simulated orders have been generated in this session yet." })}
                >
                  <OrdersTable rows={orders} />
                </DataTableShell>
              </TabsContent>

              <TabsContent value="fills" className="mt-4">
                <DataTableShell
                  isLoading={fillsQuery.isLoading}
                  isError={fillsQuery.isError}
                  error={fillsQuery.error}
                  onRetry={() => fillsQuery.refetch()}
                  isEmpty={fills.length === 0}
                  emptyTitle={tl({ zh: "暂无成交", en: "No fills" })}
                  emptyDescription={tl({ zh: "该会话尚未发生任何模拟成交。", en: "No simulated fills have occurred in this session yet." })}
                >
                  <FillsTable rows={fills} />
                </DataTableShell>
              </TabsContent>

              <TabsContent value="ledger" className="mt-4">
                <DataTableShell
                  isLoading={ledgerQuery.isLoading}
                  isError={ledgerQuery.isError}
                  error={ledgerQuery.error}
                  onRetry={() => ledgerQuery.refetch()}
                  isEmpty={ledger.length === 0}
                  emptyTitle={tl({ zh: "暂无账本记录", en: "No ledger entries" })}
                  emptyDescription={tl({ zh: "该会话尚未产生任何资金账本变动。", en: "No cash ledger changes have been recorded in this session yet." })}
                >
                  <LedgerTable rows={ledger} />
                </DataTableShell>
              </TabsContent>

              <TabsContent value="report" className="mt-4">
                {reportQuery.isLoading ? (
                  <LoadingState rows={4} />
                ) : reportQuery.isError ? (
                  <ErrorState
                    message={errorMessage(reportQuery.error, tl({ zh: "报告加载失败", en: "Failed to load report" }))}
                    onRetry={() => reportQuery.refetch()}
                  />
                ) : report ? (
                  <ReportView report={report} />
                ) : null}
              </TabsContent>
            </Tabs>
          </div>
        </ScrollArea>
        <Dialog open={decisionOpen} onOpenChange={setDecisionOpen}>
          <DialogContent className="max-w-2xl">
            <DialogHeader>
              <DialogTitle>{tl({ zh: "提交结构化目标仓位", en: "Submit structured target position" })}</DialogTitle>
              <DialogDescription>
                {tl({ zh: "该入口只写入模拟盘决策并生成模拟订单意图，不连接券商，也不会写入实盘订单、成交或持仓。", en: "This entry only writes simulation decisions and generates simulated order intents; it never connects to a broker and never writes live orders, fills or positions." })}
              </DialogDescription>
            </DialogHeader>
            <Alert variant="info">
              <AlertDescription>
                {tl({ zh: "服务端会校验来源决策和信号 trace 必须存在于同一个已完成的 ", en: "The server validates that the source decision and the signal trace must exist in the same completed " })}
                <code>RR-</code>
                {tl({ zh: " 研究运行；页面不允许绕过这项血缘校验。", en: " research run; this lineage check cannot be bypassed from the page." })}
              </AlertDescription>
            </Alert>
            <div className="space-y-3">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="decision-id">{tl({ zh: "决策 ID", en: "Decision ID" })}</Label>
                  <Input
                    id="decision-id"
                    value={decisionId}
                    onChange={(event) => setDecisionId(event.target.value)}
                    className="font-mono text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="decision-source-run">{tl({ zh: "来源 RR-运行", en: "Source RR- run" })}</Label>
                  <Input
                    id="decision-source-run"
                    value={sourceRunId}
                    readOnly
                    placeholder={tl({ zh: "当前会话未绑定 RR-运行", en: "No RR- run bound to this session" })}
                    className="font-mono text-xs"
                  />
                </div>
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="decision-source-id">{tl({ zh: "来源决策 ID", en: "Source decision ID" })}</Label>
                  <Input
                    id="decision-source-id"
                    value={sourceDecisionId}
                    onChange={(event) => setSourceDecisionId(event.target.value)}
                    placeholder={tl({ zh: "研究运行 signals 阶段的 decision_id", en: "decision_id from the research run's signals stage" })}
                    className="font-mono text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="decision-trace">{tl({ zh: "信号 trace ID", en: "Signal trace ID" })}</Label>
                  <Input
                    id="decision-trace"
                    value={signalTraceId}
                    onChange={(event) => setSignalTraceId(event.target.value)}
                    placeholder={tl({ zh: "必须属于上述 RR-运行", en: "Must belong to the RR- run above" })}
                    className="font-mono text-xs"
                  />
                </div>
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div className="space-y-2">
                  <Label htmlFor="decision-symbol">{tl({ zh: "标的", en: "Symbol" })}</Label>
                  <Input
                    id="decision-symbol"
                    value={targetSymbol}
                    onChange={(event) => setTargetSymbol(event.target.value)}
                    placeholder="510300.SH"
                    className="font-mono"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="decision-rule">{tl({ zh: "资产规则", en: "Asset rule" })}</Label>
                  <Select value={targetRuleKey} onValueChange={setTargetRuleKey}>
                    <SelectTrigger id="decision-rule">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {SIMULATION_TARGET_RULES.map((item) => (
                        <SelectItem key={item.key} value={item.key}>
                          {tl(item.label)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="decision-quantity">{tl({ zh: "目标数量", en: "Target quantity" })}</Label>
                  <Input
                    id="decision-quantity"
                    type="number"
                    min={0}
                    step={100}
                    value={targetQuantity}
                    onChange={(event) => setTargetQuantity(event.target.value)}
                    className="tabular-nums"
                  />
                </div>
              </div>
              <div className="space-y-2">
                <Label htmlFor="decision-reason">{tl({ zh: "决策原因", en: "Decision reason" })}</Label>
                <Input
                  id="decision-reason"
                  value={targetReason}
                  onChange={(event) => setTargetReason(event.target.value)}
                  placeholder={tl({ zh: "例如：均线金叉，目标仓位由 0 调整为 1000 份", en: "e.g. MA golden cross, target position adjusted from 0 to 1000 shares" })}
                />
              </div>
              <p className="text-xs text-muted-foreground">
                {tl({ zh: `当前固定为 A 股多头市价单、GFD；资产规则 ${targetRule.key} 会决定手数、T+1、费用和风控参数。`, en: `Currently fixed to A-share long market orders with GFD; asset rule ${targetRule.key} determines lot size, T+1, fees and risk parameters.` })}
              </p>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setDecisionOpen(false)} disabled={decisionMutation.isPending}>
                {tl({ zh: "取消", en: "Cancel" })}
              </Button>
              <Button onClick={() => decisionMutation.mutate()} disabled={!decisionValid || decisionMutation.isPending}>
                {decisionMutation.isPending ? tl({ zh: "提交中…", en: "Submitting…" }) : tl({ zh: "提交目标仓位", en: "Submit target position" })}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
        <ReplayBarDialog
          session={detail}
          open={marketEventOpen}
          onOpenChange={setMarketEventOpen}
          onProcessed={(result) => {
            setMarketEventResult(result);
            invalidateSession();
          }}
        />
      </CardContent>
    </Card>
  );
}

function PositionsTable({ rows }: { rows: SimulationPosition[] }) {
  const { tl } = useT();
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
            <TableHead className="text-right">{tl({ zh: "浮动盈亏", en: "Unrealized P&L" })}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((p) => (
            <TableRow key={`${p.symbol}-${p.side}`}>
              <TableCell className="font-mono">{p.symbol}</TableCell>
              <TableCell>
                <SideBadge side={p.side} />
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

function OrdersTable({ rows }: { rows: SimulationOrder[] }) {
  const { tl } = useT();
  return (
    <div className="rounded-lg border border-border">
      <Table>
        <TableHeader className="sticky top-0 bg-card">
          <TableRow>
            <TableHead>{tl({ zh: "标的", en: "Symbol" })}</TableHead>
            <TableHead>{tl({ zh: "方向", en: "Side" })}</TableHead>
            <TableHead>{tl({ zh: "类型", en: "Type" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "委托量", en: "Quantity" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "已成交", en: "Filled" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "委托价", en: "Price" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "成交均价", en: "Avg fill price" })}</TableHead>
            <TableHead>{tl({ zh: "状态", en: "Status" })}</TableHead>
            <TableHead>{tl({ zh: "时间", en: "Time" })}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((o) => (
            <TableRow key={o.order_id}>
              <TableCell className="font-mono">{o.symbol}</TableCell>
              <TableCell>
                <SideBadge side={o.side} />
              </TableCell>
              <TableCell>
                <Badge variant="outline" className="font-mono">
                  {o.order_type}
                </Badge>
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {formatNumber(o.quantity, 0)}
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {formatNumber(o.filled_quantity, 0)}
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {o.price !== undefined ? formatCurrency(o.price) : "—"}
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {o.avg_fill_price !== undefined
                  ? formatCurrency(o.avg_fill_price)
                  : "—"}
              </TableCell>
              <TableCell>
                <StatusBadge status={o.status} />
              </TableCell>
              <TableCell className="tabular-nums text-muted-foreground">
                {formatDateTime(o.created_at)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function FillsTable({ rows }: { rows: SimulationFill[] }) {
  const { tl } = useT();
  return (
    <div className="rounded-lg border border-border">
      <Table>
        <TableHeader className="sticky top-0 bg-card">
          <TableRow>
            <TableHead>{tl({ zh: "标的", en: "Symbol" })}</TableHead>
            <TableHead>{tl({ zh: "方向", en: "Side" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "数量", en: "Quantity" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "成交价", en: "Fill price" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "佣金", en: "Commission" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "税金", en: "Tax" })}</TableHead>
            <TableHead>{tl({ zh: "时间", en: "Time" })}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((f) => (
            <TableRow key={f.fill_id}>
              <TableCell className="font-mono">{f.symbol}</TableCell>
              <TableCell>
                <SideBadge side={f.side} />
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {formatNumber(f.quantity, 0)}
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {formatCurrency(f.price)}
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums text-muted-foreground">
                {formatCurrency(f.commission)}
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums text-muted-foreground">
                {formatCurrency(f.tax)}
              </TableCell>
              <TableCell className="tabular-nums text-muted-foreground">
                {formatDateTime(f.timestamp)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function LedgerTable({ rows }: { rows: SimulationLedgerEntry[] }) {
  const { tl } = useT();
  return (
    <div className="rounded-lg border border-border">
      <Table>
        <TableHeader className="sticky top-0 bg-card">
          <TableRow>
            <TableHead>{tl({ zh: "事件", en: "Event" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "资金变动", en: "Cash change" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "变动后资金", en: "Cash after" })}</TableHead>
            <TableHead>{tl({ zh: "说明", en: "Description" })}</TableHead>
            <TableHead>{tl({ zh: "时间", en: "Time" })}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((e) => (
            <TableRow key={e.entry_id}>
              <TableCell>
                <Badge variant="info" className="font-mono">
                  {e.event_type}
                </Badge>
              </TableCell>
              <TableCell
                className={cn(
                  "text-right font-mono tabular-nums",
                  pnlColor(e.cash_delta),
                )}
              >
                {e.cash_delta > 0 ? "+" : ""}
                {formatCurrency(e.cash_delta)}
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {formatCurrency(e.cash_after)}
              </TableCell>
              <TableCell className="text-muted-foreground">
                {e.description || "—"}
              </TableCell>
              <TableCell className="tabular-nums text-muted-foreground">
                {formatDateTime(e.timestamp)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function ReportView({ report }: { report: SimulationReport }) {
  const { tl } = useT();
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <MetricCard
          label={tl({ zh: "总收益", en: "Total return" })}
          value={formatPercent(report.total_return)}
          accent={pnlColor(report.total_return)}
        />
        <MetricCard
          label={tl({ zh: "年化收益", en: "Annualized return" })}
          value={formatPercent(report.annual_return)}
          accent={pnlColor(report.annual_return)}
        />
        <MetricCard
          label={tl({ zh: "夏普比率", en: "Sharpe ratio" })}
          value={formatNumber(report.sharpe_ratio, 3)}
          accent={pnlColor(report.sharpe_ratio)}
        />
        <MetricCard
          label={tl({ zh: "最大回撤", en: "Max drawdown" })}
          value={formatPercent(report.max_drawdown)}
          accent="text-destructive"
        />
        <MetricCard
          label={tl({ zh: "胜率", en: "Win rate" })}
          value={formatPercent(report.win_rate)}
        />
        <MetricCard
          label={tl({ zh: "总成交笔数", en: "Total trades" })}
          value={formatNumber(report.total_trades, 0)}
        />
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">{tl({ zh: "权益曲线", en: "Equity curve" })}</CardTitle>
        </CardHeader>
        <CardContent>
          {report.equity_curve.length > 0 ? (
            <div className="rounded-md border border-border">
              <Table>
                <TableHeader className="sticky top-0 bg-card">
                  <TableRow>
                    <TableHead>{tl({ zh: "时间", en: "Time" })}</TableHead>
                    <TableHead className="text-right">{tl({ zh: "权益", en: "Equity" })}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {report.equity_curve.map((pt, i) => (
                    <TableRow key={`${pt.timestamp}-${i}`}>
                      <TableCell className="tabular-nums text-muted-foreground">
                        {formatDateTime(pt.timestamp)}
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {formatCurrency(pt.equity)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : (
            <EmptyState
              title={tl({ zh: "暂无权益数据", en: "No equity data" })}
              description={tl({ zh: "该会话尚未生成权益曲线。", en: "This session has not generated an equity curve yet." })}
            />
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function CreateAccountDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const { tl } = useT();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [initialCash, setInitialCash] = useState("1000000");
  const [actor, setActor] = useState("console");

  const [currency, setCurrency] = useState("CNY");

  const mutation = useMutation({
    mutationFn: () =>
      simulationApi.createAccount({
        name: name.trim(),
        initial_cash: Number(initialCash),
        actor: actor.trim(),
        currency: currency.trim() || undefined,
      }),
    onSuccess: (acct) => {
      void queryClient.invalidateQueries({
        queryKey: ["simulation", "accounts"],
      });
      onOpenChange(false);
      setName("");
      setInitialCash("1000000");
      return acct;
    },
  });

  const valid =
    name.trim() !== "" &&
    actor.trim() !== "" &&
    initialCash.trim() !== "" &&
    !Number.isNaN(Number(initialCash));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{tl({ zh: "新建模拟账户", en: "New simulation account" })}</DialogTitle>
          <DialogDescription>
            {tl({ zh: "模拟账户与实盘账户完全隔离，仅用于纸面交易。初始资金仅作模拟记账用途。", en: "Simulation accounts are fully isolated from the live account and are for paper trading only. Initial capital is used for simulated bookkeeping only." })}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-2">
            <Label htmlFor="acct-name">{tl({ zh: "账户名称", en: "Account name" })}</Label>
            <Input
              id="acct-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={tl({ zh: "例如：均线策略模拟盘", en: "e.g. MA strategy simulation" })}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-2">
              <Label htmlFor="acct-cash">{tl({ zh: "初始资金", en: "Initial capital" })}</Label>
              <Input
                id="acct-cash"
                type="number"
                value={initialCash}
                onChange={(e) => setInitialCash(e.target.value)}
                className="tabular-nums"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="acct-currency">{tl({ zh: "币种", en: "Currency" })}</Label>
              <Input
                id="acct-currency"
                value={currency}
                onChange={(e) => setCurrency(e.target.value)}
                className="font-mono"
              />
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="acct-actor">{tl({ zh: "操作人", en: "Actor" })}</Label>
            <Input
              id="acct-actor"
              value={actor}
              onChange={(e) => setActor(e.target.value)}
              placeholder={tl({ zh: "例如：analyst@finboard", en: "e.g. analyst@finboard" })}
              className="font-mono"
            />
          </div>
        </div>
        {mutation.isError && (
          <p className="text-sm text-destructive">
            {errorMessage(mutation.error, tl({ zh: "创建账户失败", en: "Failed to create account" }))}
          </p>
        )}
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={mutation.isPending}
          >
            {tl({ zh: "取消", en: "Cancel" })}
          </Button>
          <Button
            disabled={mutation.isPending || !valid}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? tl({ zh: "创建中…", en: "Creating…" }) : tl({ zh: "创建账户", en: "Create account" })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function CreateSessionDialog({
  open,
  onOpenChange,
  accounts,
  defaultAccountId,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  accounts: SimulationAccount[];
  defaultAccountId: string | null;
}) {
  const { tl } = useT();
  const queryClient = useQueryClient();
  const [accountId, setAccountId] = useState<string>(defaultAccountId ?? "");
  const [strategyId, setStrategyId] = useState("");
  const [strategyVersion, setStrategyVersion] = useState("1");
  const [sourceMode, setSourceMode] = useState("historical_replay");
  const [validationRunId, setValidationRunId] = useState("");
  const [dataReleaseId, setDataReleaseId] = useState("");
  const [actor, setActor] = useState("console");

  useEffect(() => {
    if (open && defaultAccountId) setAccountId(defaultAccountId);
  }, [defaultAccountId, open]);

  const mutation = useMutation({
    mutationFn: () =>
      simulationApi.createSession({
        simulation_account_id: accountId,
        strategy_id: strategyId.trim(),
        strategy_version: Number(strategyVersion),
        validation_run_id: validationRunId.trim(),
        data_release_id: dataReleaseId.trim(),
        source_mode: sourceMode,
        actor: actor.trim(),
      }),
    onSuccess: (sess) => {
      void queryClient.invalidateQueries({
        queryKey: ["simulation", "sessions"],
      });
      onOpenChange(false);
      setStrategyId("");
      setStrategyVersion("1");
      setValidationRunId("");
      setDataReleaseId("");
      return sess;
    },
  });

  const valid =
    accountId !== "" &&
    strategyId.trim() !== "" &&
    /^RR-/.test(validationRunId.trim()) &&
    dataReleaseId.trim() !== "" &&
    actor.trim() !== "" &&
    strategyVersion.trim() !== "" &&
    !Number.isNaN(Number(strategyVersion));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{tl({ zh: "新建模拟会话", en: "New simulation session" })}</DialogTitle>
          <DialogDescription>
            {tl({ zh: "会话基于已发布策略、已完成的机器验证运行和数据发布版本创建。订单只能由结构化目标仓位决策生成，不会直接创建订单。", en: "Sessions are created from a published strategy, a completed machine-validation run and a data release version. Orders can only be generated by structured target position decisions, never created directly." })}
          </DialogDescription>
        </DialogHeader>
        <Alert variant="info">
          <AlertDescription>
            {tl({ zh: "需要先在「研究运行」中获得 ", en: "First obtain an " })}
            <code>RR-</code>
            {tl({ zh: " 运行 ID，并填写该运行冻结的数据发布 ID；这两个值用于隔离和审计，不能省略。", en: " run ID from Research Runs and fill in the data release ID frozen by that run; both values are required for isolation and audit and cannot be omitted." })}
          </AlertDescription>
        </Alert>
        <div className="space-y-3">
          <div className="space-y-2">
            <Label htmlFor="sess-account">{tl({ zh: "模拟账户", en: "Simulation account" })}</Label>
            <Select value={accountId} onValueChange={setAccountId}>
              <SelectTrigger id="sess-account">
                <SelectValue placeholder={tl({ zh: "选择模拟账户", en: "Select a simulation account" })} />
              </SelectTrigger>
              <SelectContent>
                {accounts.map((a) => (
                  <SelectItem key={a.account_id} value={a.account_id}>
                    {a.name}{" "}
                    <span className="font-mono text-xs text-muted-foreground">
                      ({a.account_id.slice(0, 8)}…)
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-2">
              <Label htmlFor="sess-strategy">{tl({ zh: "策略 ID", en: "Strategy ID" })}</Label>
              <Input
                id="sess-strategy"
                value={strategyId}
                onChange={(e) => setStrategyId(e.target.value)}
                placeholder={tl({ zh: "例如：ma-cross-v1", en: "e.g. ma-cross-v1" })}
                className="font-mono"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="sess-version">{tl({ zh: "策略版本", en: "Strategy version" })}</Label>
              <Input
                id="sess-version"
                type="number"
                value={strategyVersion}
                onChange={(e) => setStrategyVersion(e.target.value)}
                className="tabular-nums"
              />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-2">
              <Label htmlFor="sess-mode">{tl({ zh: "数据源模式", en: "Data source mode" })}</Label>
              <Select value={sourceMode} onValueChange={setSourceMode}>
                <SelectTrigger id="sess-mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="historical_replay">{tl({ zh: "historical_replay（历史回放）", en: "historical_replay (Historical replay)" })}</SelectItem>
                  <SelectItem value="readonly_market">{tl({ zh: "readonly_market（只读行情）", en: "readonly_market (Read-only market data)" })}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="sess-run">{tl({ zh: "验证运行 ID（必填）", en: "Validation run ID (required)" })}</Label>
              <Input
                id="sess-run"
                value={validationRunId}
                onChange={(e) => setValidationRunId(e.target.value)}
                placeholder={tl({ zh: "RR-...（已完成）", en: "RR-... (completed)" })}
                className="font-mono"
              />
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="sess-release">{tl({ zh: "数据发布 ID（必填）", en: "Data release ID (required)" })}</Label>
            <Input
              id="sess-release"
              value={dataReleaseId}
              onChange={(e) => setDataReleaseId(e.target.value)}
                placeholder={tl({ zh: "例如：multi-asset-bars-20260801-v1", en: "e.g. multi-asset-bars-20260801-v1" })}
              className="font-mono"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="sess-actor">{tl({ zh: "操作人", en: "Actor" })}</Label>
            <Input
              id="sess-actor"
              value={actor}
              onChange={(e) => setActor(e.target.value)}
              placeholder={tl({ zh: "例如：analyst@finboard", en: "e.g. analyst@finboard" })}
              className="font-mono"
            />
          </div>
        </div>
        {mutation.isError && (
          <p className="text-sm text-destructive">
            {errorMessage(mutation.error, tl({ zh: "创建会话失败", en: "Failed to create session" }))}
          </p>
        )}
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={mutation.isPending}
          >
            {tl({ zh: "取消", en: "Cancel" })}
          </Button>
          <Button
            disabled={mutation.isPending || !valid}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? tl({ zh: "创建中…", en: "Creating…" }) : tl({ zh: "创建会话", en: "Create session" })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function defaultReplayTimestamp(): string {
  return new Date().toISOString().slice(0, 16);
}

function ReplayBarDialog({
  session,
  open,
  onOpenChange,
  onProcessed,
}: {
  session: SimulationSession;
  open: boolean;
  onOpenChange: (value: boolean) => void;
  onProcessed: (result: SimulationProcessResult) => void;
}) {
  const { tl } = useT();
  const [sourceEventId, setSourceEventId] = useState("");
  const [symbol, setSymbol] = useState("510300.SH");
  const [timestamp, setTimestamp] = useState("");
  const [openPrice, setOpenPrice] = useState("4.000");
  const [highPrice, setHighPrice] = useState("4.050");
  const [lowPrice, setLowPrice] = useState("3.950");
  const [closePrice, setClosePrice] = useState("4.020");
  const [volume, setVolume] = useState("1000000");
  const [amount, setAmount] = useState("4020000");
  const [actor, setActor] = useState("market-replay");

  useEffect(() => {
    if (!open) return;
    if (!sourceEventId) setSourceEventId(`manual-bar-${Date.now()}`);
    if (!timestamp) setTimestamp(defaultReplayTimestamp());
  }, [open, sourceEventId, timestamp]);

  const prices = [openPrice, highPrice, lowPrice, closePrice].map(Number);
  const [openValue, highValue, lowValue, closeValue] = prices;
  const valid =
    sourceEventId.trim() !== "" &&
    symbol.trim() !== "" &&
    timestamp.trim() !== "" &&
    actor.trim() !== "" &&
    prices.every((value) => Number.isFinite(value) && value > 0) &&
    highValue >= Math.max(openValue, closeValue, lowValue) &&
    lowValue <= Math.min(openValue, closeValue, highValue) &&
    Number.isFinite(Number(volume)) &&
    Number(volume) >= 0 &&
    Number.isFinite(Number(amount)) &&
    Number(amount) >= 0;

  const mutation = useMutation({
    mutationFn: () =>
      simulationApi.processMarketEvent(session.session_id, {
        source_event_id: sourceEventId.trim(),
        symbol: symbol.trim(),
        market: "a_share",
        period: "1d",
        timestamp: timestamp.trim(),
        open: openValue,
        high: highValue,
        low: lowValue,
        close: closeValue,
        volume: Number(volume),
        amount: Number(amount),
        actor: actor.trim(),
      }),
    onSuccess: (result) => {
      onProcessed(result);
      onOpenChange(false);
      setSourceEventId("");
      setTimestamp("");
    },
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{tl({ zh: "推进一根行情", en: "Advance one bar" })}</DialogTitle>
          <DialogDescription>
            {tl({ zh: "仅用于历史回放/纸面撮合。先处理一根行情建立价格，再提交目标仓位；在 next-bar-only 规则下，下一根行情才会尝试成交。", en: "For historical replay / paper matching only. Process one bar to establish prices, then submit target positions; under the next-bar-only rule, fills are attempted on the next bar." })}
          </DialogDescription>
        </DialogHeader>
        <Alert variant="info">
          <AlertDescription>
            {tl({ zh: "同一个 ", en: "Submitting the same " })}
            <code>source_event_id</code>
            {tl({ zh: " 重复提交是幂等的；行情事件不会连接实盘行情或触发真实订单。", en: " twice is idempotent; market events do not connect to live market data or trigger real orders." })}
          </AlertDescription>
        </Alert>
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="bar-source-event">{tl({ zh: "行情事件 ID", en: "Market event ID" })}</Label>
              <Input
                id="bar-source-event"
                value={sourceEventId}
                onChange={(event) => setSourceEventId(event.target.value)}
                className="font-mono text-xs"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="bar-timestamp">{tl({ zh: "时间", en: "Time" })}</Label>
              <Input
                id="bar-timestamp"
                type="datetime-local"
                value={timestamp}
                onChange={(event) => setTimestamp(event.target.value)}
              />
            </div>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="bar-symbol">{tl({ zh: "标的", en: "Symbol" })}</Label>
              <Input
                id="bar-symbol"
                value={symbol}
                onChange={(event) => setSymbol(event.target.value)}
                placeholder="510300.SH"
                className="font-mono"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="bar-actor">{tl({ zh: "操作人", en: "Actor" })}</Label>
              <Input
                id="bar-actor"
                value={actor}
                onChange={(event) => setActor(event.target.value)}
                className="font-mono text-xs"
              />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[
              ["bar-open", tl({ zh: "开盘", en: "Open" }), openPrice, setOpenPrice],
              ["bar-high", tl({ zh: "最高", en: "High" }), highPrice, setHighPrice],
              ["bar-low", tl({ zh: "最低", en: "Low" }), lowPrice, setLowPrice],
              ["bar-close", tl({ zh: "收盘", en: "Close" }), closePrice, setClosePrice],
            ].map(([id, label, value, setter]) => (
              <div key={id as string} className="space-y-2">
                <Label htmlFor={id as string}>{label as string}</Label>
                <Input
                  id={id as string}
                  type="number"
                  min={0}
                  step="0.001"
                  value={value as string}
                  onChange={(event) => (setter as (value: string) => void)(event.target.value)}
                  className="tabular-nums"
                />
              </div>
            ))}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-2">
              <Label htmlFor="bar-volume">{tl({ zh: "成交量", en: "Volume" })}</Label>
              <Input
                id="bar-volume"
                type="number"
                min={0}
                value={volume}
                onChange={(event) => setVolume(event.target.value)}
                className="tabular-nums"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="bar-amount">{tl({ zh: "成交额", en: "Amount" })}</Label>
              <Input
                id="bar-amount"
                type="number"
                min={0}
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
                className="tabular-nums"
              />
            </div>
          </div>
        </div>
        {mutation.isError && (
          <Alert variant="destructive">
            <AlertTitle>{tl({ zh: "行情事件未处理", en: "Market event not processed" })}</AlertTitle>
            <AlertDescription>
              {errorMessage(mutation.error, tl({ zh: "请检查会话状态、时间顺序和 OHLC 数值", en: "Check the session status, time ordering and OHLC values" }))}
            </AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={mutation.isPending}>
            {tl({ zh: "取消", en: "Cancel" })}
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={!valid || mutation.isPending}>
            {mutation.isPending ? tl({ zh: "处理中…", en: "Processing…" }) : tl({ zh: "处理行情", en: "Process bar" })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function Simulation() {
  const { tl, lang } = useT();
  const queryClient = useQueryClient();
  const [selectedAccountId, setSelectedAccountId] = useState<string | null>(
    null,
  );
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(
    null,
  );
  const [accountDialogOpen, setAccountDialogOpen] = useState(false);
  const [sessionDialogOpen, setSessionDialogOpen] = useState(false);

  const accountsQuery = useQuery({
    queryKey: ["simulation", "accounts"],
    queryFn: () => simulationApi.listAccounts(),
  });

  const sessionsQuery = useQuery({
    queryKey: ["simulation", "sessions", selectedAccountId],
    queryFn: () =>
      simulationApi.listSessions({ account_id: selectedAccountId as string }),
    enabled: selectedAccountId !== null,
  });

  const accounts = useMemo(() => accountsQuery.data ?? [], [accountsQuery.data]);
  const sessions = sessionsQuery.data ?? [];

  useEffect(() => {
    if (selectedAccountId === null && accounts.length > 0) {
      setSelectedAccountId(accounts[0].account_id);
    }
  }, [accounts, selectedAccountId]);

  const selectedAccount = accounts.find(
    (a) => a.account_id === selectedAccountId,
  );
  const selectedSession = sessions.find(
    (s) => s.session_id === selectedSessionId,
  );

  const handleSelectAccount = (id: string) => {
    setSelectedAccountId(id);
    setSelectedSessionId(null);
  };

  const invalidateSessions = () => {
    void queryClient.invalidateQueries({
      queryKey: ["simulation", "sessions", selectedAccountId],
    });
  };

  return (
    <div>
      <PageHeader
        title={tl({ zh: "模拟盘", en: "Simulation" })}
        description={tl({ zh: "产品模拟盘（纸面交易）：基于结构化目标仓位决策的隔离模拟运行", en: "Product simulation (paper trading): isolated simulation runs driven by structured target position decisions" })}
        breadcrumbs={[
          { label: tl({ zh: "研究", en: "Research" }), href: "/research" },
          { label: tl({ zh: "模拟盘", en: "Simulation" }) },
        ]}
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={() => accountsQuery.refetch()}
            disabled={accountsQuery.isFetching}
          >
            <RefreshCw
              className={cn(
                "h-4 w-4",
                accountsQuery.isFetching && "animate-spin",
              )}
            />
            {tl({ zh: "刷新账户", en: "Refresh accounts" })}
          </Button>
        }
      />
      <WorkflowIndicator currentPath="/research/simulation" />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
        <div className="space-y-4 lg:col-span-1">
          <Alert variant="info">
            <AlertTitle>{tl({ zh: "实盘隔离", en: "Live-trading isolation" })}</AlertTitle>
            <AlertDescription>
              {tl({ zh: "模拟盘与实盘完全隔离。订单只能由结构化目标仓位决策生成，禁止直接创建订单。", en: "The simulation is fully isolated from live trading. Orders can only be generated by structured target position decisions; creating orders directly is forbidden." })}
            </AlertDescription>
          </Alert>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">{tl({ zh: "模拟账户", en: "Simulation accounts" })}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {accountsQuery.isLoading ? (
                <LoadingState rows={2} />
              ) : accountsQuery.isError ? (
                <ErrorState
                  message={errorMessage(accountsQuery.error, tl({ zh: "无法加载账户", en: "Failed to load accounts" }))}
                  onRetry={() => accountsQuery.refetch()}
                />
              ) : accounts.length > 0 ? (
                <>
                  <Select
                    value={selectedAccountId ?? undefined}
                    onValueChange={handleSelectAccount}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder={tl({ zh: "选择模拟账户", en: "Select a simulation account" })} />
                    </SelectTrigger>
                    <SelectContent>
                      {accounts.map((a) => (
                        <SelectItem key={a.account_id} value={a.account_id}>
                          <span className="truncate">{a.name}</span>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  {selectedAccount && (
                    <div className="grid grid-cols-2 gap-2 rounded-md border border-border bg-muted/30 p-3 text-xs">
                      <div>
                        <p className="text-muted-foreground">{tl({ zh: "可用资金", en: "Available cash" })}</p>
                        <p className="mt-0.5 font-mono tabular-nums">
                          ¥{formatCurrency(selectedAccount.cash, 0)}
                        </p>
                      </div>
                      <div>
                        <p className="text-muted-foreground">{tl({ zh: "总权益", en: "Total equity" })}</p>
                        <p className="mt-0.5 font-mono tabular-nums">
                          ¥{formatCurrency(selectedAccount.equity, 0)}
                        </p>
                      </div>
                      <div>
                        <p className="text-muted-foreground">{tl({ zh: "冻结资金", en: "Frozen cash" })}</p>
                        <p className="mt-0.5 font-mono tabular-nums text-muted-foreground">
                          ¥{formatCurrency(selectedAccount.frozen_cash, 0)}
                        </p>
                      </div>
                      <div>
                        <p className="text-muted-foreground">{tl({ zh: "状态", en: "Status" })}</p>
                        <p className="mt-0.5">
                          <StatusBadge status={selectedAccount.status} />
                        </p>
                      </div>
                    </div>
                  )}
                </>
              ) : (
                <EmptyState
                  title={tl({ zh: "暂无模拟账户", en: "No simulation accounts" })}
                  description={tl({ zh: "点击下方按钮创建第一个模拟账户。", en: "Click the button below to create your first simulation account." })}
                />
              )}
              <Button
                variant="outline"
                size="sm"
                className="w-full"
                onClick={() => setAccountDialogOpen(true)}
              >
                <Plus className="h-4 w-4" />
                {tl({ zh: "新建账户", en: "New account" })}
              </Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base">{tl({ zh: "会话列表", en: "Sessions" })}</CardTitle>
                {selectedAccountId && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => invalidateSessions()}
                    disabled={sessionsQuery.isFetching}
                  >
                    <RefreshCw
                      className={cn(
                        "h-3.5 w-3.5",
                        sessionsQuery.isFetching && "animate-spin",
                      )}
                    />
                  </Button>
                )}
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              {!selectedAccountId ? (
                <EmptyState
                  icon={<FlaskConical className="h-8 w-8" />}
                  title={tl({ zh: "请先选择账户", en: "Select an account first" })}
                  description={tl({ zh: "选择模拟账户后将展示该账户下的全部模拟会话。", en: "Once a simulation account is selected, all simulation sessions under it are shown." })}
                />
              ) : sessionsQuery.isLoading ? (
                <LoadingState rows={4} />
              ) : sessionsQuery.isError ? (
                <ErrorState
                  message={errorMessage(sessionsQuery.error, tl({ zh: "无法加载会话", en: "Failed to load sessions" }))}
                  onRetry={() => sessionsQuery.refetch()}
                />
              ) : sessions.length > 0 ? (
                <ScrollArea className="max-h-[520px] pr-3">
                  <div className="space-y-2">
                    {sessions.map((s) => (
                      <button
                        key={s.session_id}
                        type="button"
                        onClick={() => setSelectedSessionId(s.session_id)}
                        className={cn(
                          "w-full rounded-md border border-border p-3 text-left transition-colors hover:bg-accent",
                          selectedSessionId === s.session_id &&
                            "border-primary bg-accent ring-1 ring-primary/40",
                        )}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <StatusBadge status={s.status} />
                          <Badge variant="info" className="font-mono">
                            {s.source_mode}
                          </Badge>
                        </div>
                        <p className="mt-2 truncate font-mono text-xs text-foreground">
                          {s.session_id}
                        </p>
                        <div className="mt-1.5 flex items-center justify-between gap-2 text-xs text-muted-foreground">
                          <span className="truncate font-mono">
                            {s.strategy_id}
                          </span>
                          <span>{timeAgo(s.created_at, lang)}</span>
                        </div>
                      </button>
                    ))}
                  </div>
                </ScrollArea>
              ) : (
                <EmptyState
                  icon={<FlaskConical className="h-8 w-8" />}
                  title={tl({ zh: "暂无会话", en: "No sessions" })}
                  description={tl({ zh: "该账户下还没有模拟会话，可点击下方按钮创建。", en: "No simulation sessions under this account yet; create one with the button below." })}
                />
              )}
              <Button
                variant="outline"
                size="sm"
                className="w-full"
                onClick={() => setSessionDialogOpen(true)}
                disabled={accounts.length === 0}
              >
                <Plus className="h-4 w-4" />
                {tl({ zh: "新建会话", en: "New session" })}
              </Button>
            </CardContent>
          </Card>
        </div>

        <div className="lg:col-span-3">
          {selectedSession ? (
            <SessionDetail
              session={selectedSession}
              onClear={() => setSelectedSessionId(null)}
            />
          ) : (
            <EmptyState
              icon={<FlaskConical className="h-8 w-8" />}
              title={tl({ zh: "请从左侧选择一个模拟会话", en: "Select a simulation session on the left" })}
              description={tl({ zh: "选中会话后将展示持仓、订单、成交、账本与绩效报告，并支持启动 / 暂停 / 停止 / 归档操作。", en: "Once selected, positions, orders, fills, the ledger and performance reports are shown, with start / pause / stop / archive actions." })}
            />
          )}
        </div>
      </div>

      <CreateAccountDialog
        open={accountDialogOpen}
        onOpenChange={setAccountDialogOpen}
      />
      <CreateSessionDialog
        open={sessionDialogOpen}
        onOpenChange={setSessionDialogOpen}
        accounts={accounts}
        defaultAccountId={selectedAccountId}
      />
      <NextStepCTA
        nextPath="/research/reports"
        nextLabel={tl({ zh: "研究报告", en: "Research Reports" })}
        description={tl({ zh: "查看模拟交易的完整绩效报告和归因分析", en: "View the full performance reports and attribution analysis of the simulated trading" })}
      />
    </div>
  );
}
