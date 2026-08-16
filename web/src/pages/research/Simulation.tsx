import { WorkflowIndicator, NextStepCTA } from "@/components/research/ResearchHint";
import { type ReactNode, useEffect, useMemo, useState } from "react";
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
  { key: "equity_etf", label: "股票 ETF（100 份/手）", instrumentType: "etf" },
  { key: "a_share_stock", label: "A 股股票（100 股/手）", instrumentType: "stock" },
  { key: "cross_border_etf", label: "跨境 ETF（T+0）", instrumentType: "etf" },
  { key: "bond_etf", label: "债券 ETF（10 份/手）", instrumentType: "etf" },
  { key: "money_market_etf", label: "货币 ETF（T+0）", instrumentType: "etf" },
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
  if (isLoading) return <LoadingState rows={4} />;
  if (isError)
    return (
      <ErrorState
        message={errorMessage(error, "数据加载失败")}
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
            aria-label="取消选择"
          >
            <RefreshCw className="h-4 w-4 rotate-180" />
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        <ScrollArea className="max-h-[760px] pr-3">
          <div className="space-y-5">
            <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-4">
              <InfoItem label="会话 ID">
                <span className="font-mono text-xs">{detail.session_id}</span>
              </InfoItem>
              <InfoItem label="策略">
                <span className="font-mono text-xs">{detail.strategy_id}</span>
              </InfoItem>
              <InfoItem label="版本">
                <span className="font-mono">v{detail.strategy_version}</span>
              </InfoItem>
              <InfoItem label="状态">
                <StatusBadge status={status} />
              </InfoItem>
              <InfoItem label="数据源模式">
                <Badge variant="info" className="font-mono">
                  {detail.source_mode}
                </Badge>
              </InfoItem>
              <InfoItem label="来源运行">
                <span className="font-mono text-xs">
                  {detail.source_run_id ?? "—"}
                </span>
              </InfoItem>
              <InfoItem label="晋级状态">
                <Badge variant="secondary" className="font-mono">
                  {detail.promotion_status}
                </Badge>
              </InfoItem>
              <InfoItem label="时钟倍速">
                <span className="font-mono tabular-nums">
                  {formatNumber(detail.clock_speed, 1)}x
                </span>
              </InfoItem>
              <InfoItem label="恢复次数">
                <span className="font-mono tabular-nums">
                  {detail.recovery_count}
                </span>
              </InfoItem>
              <InfoItem label="创建时间">
                <span className="tabular-nums">
                  {formatDateTime(detail.created_at)}
                </span>
              </InfoItem>
            </div>

            {detail.data_release_id && (
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <span>数据发布:</span>
                <span className="font-mono text-foreground">
                  {detail.data_release_id}
                </span>
              </div>
            )}

            <Separator />

            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <Label htmlFor="action-actor" className="text-xs text-muted-foreground">
                  操作人
                </Label>
                <Input
                  id="action-actor"
                  value={actionActor}
                  onChange={(e) => setActionActor(e.target.value)}
                  placeholder="例如：console"
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
                    {startMutation.isPending ? "启动中…" : "启动"}
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
                      {pauseMutation.isPending ? "暂停中…" : "暂停"}
                    </Button>
                    <Button
                      size="sm"
                      variant="destructive"
                      disabled={actorInvalid || stopMutation.isPending}
                      onClick={() => stopMutation.mutate()}
                    >
                      <Square className="h-4 w-4" />
                      {stopMutation.isPending ? "停止中…" : "停止"}
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
                      {startMutation.isPending ? "启动中…" : "启动"}
                    </Button>
                    <Button
                      size="sm"
                      variant="destructive"
                      disabled={actorInvalid || stopMutation.isPending}
                      onClick={() => stopMutation.mutate()}
                    >
                      <Square className="h-4 w-4" />
                      {stopMutation.isPending ? "停止中…" : "停止"}
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
                    {archiveMutation.isPending ? "归档中…" : "归档"}
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  disabled={status !== "running" || actorInvalid}
                  onClick={openDecisionDialog}
                >
                  <Plus className="h-4 w-4" />
                  提交目标仓位
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={status !== "running"}
                  onClick={() => setMarketEventOpen(true)}
                >
                  <Play className="h-4 w-4" />
                  推进一根行情
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
                  刷新数据
                </Button>
              </div>
              {startMutation.isError && (
                <p className="text-sm text-destructive">
                  {errorMessage(startMutation.error, "启动失败")}
                </p>
              )}
              {pauseMutation.isError && (
                <p className="text-sm text-destructive">
                  {errorMessage(pauseMutation.error, "暂停失败")}
                </p>
              )}
              {stopMutation.isError && (
                <p className="text-sm text-destructive">
                  {errorMessage(stopMutation.error, "停止失败")}
                </p>
              )}
              {archiveMutation.isError && (
                <p className="text-sm text-destructive">
                  {errorMessage(archiveMutation.error, "归档失败")}
                </p>
              )}
              {status !== "running" && (
                <p className="text-xs text-muted-foreground">
                  只有 running 会话可以接收目标仓位决策；当前请先点击“启动”。
                </p>
              )}
              {!sourceRunId && (
                <Alert variant="warning">
                  <AlertTitle>会话缺少机器验证来源</AlertTitle>
                  <AlertDescription>
                    此会话没有绑定 <code>RR-</code> 完成运行，无法提交模拟决策。请返回研究运行页面，使用已完成运行和冻结数据发布重新创建会话。
                  </AlertDescription>
                </Alert>
              )}
              {decisionMutation.isError && (
                <Alert variant="destructive">
                  <AlertTitle>目标仓位未提交</AlertTitle>
                  <AlertDescription>
                    {errorMessage(decisionMutation.error, "请检查 RR- 来源、来源决策和信号 trace 是否属于同一研究运行")}
                  </AlertDescription>
                </Alert>
              )}
              {decisionResult && (
                <Alert variant="success">
                  <AlertTitle>目标仓位已记录</AlertTitle>
                  <AlertDescription>
                    {decisionResult.duplicate
                      ? "检测到相同 decision_id，已幂等返回原结果。"
                      : `已生成 ${decisionResult.orderCount} 个模拟订单；订单仍需行情回放后才会产生成交。`}
                  </AlertDescription>
                </Alert>
              )}
              {marketEventResult && (
                <Alert variant="success">
                  <AlertTitle>行情事件已处理</AlertTitle>
                  <AlertDescription>
                    {marketEventResult.duplicate
                      ? "检测到相同 source_event_id，已幂等返回原结果。"
                      : `已处理 ${marketEventResult.source_event_id}；成交 ${marketEventResult.fill_ids.length} 笔，拒单 ${marketEventResult.rejected_order_ids.length} 笔。`}
                  </AlertDescription>
                </Alert>
              )}
            </div>

            <Separator />

            <Tabs value={tab} onValueChange={setTab}>
              <TabsList>
                <TabsTrigger value="positions">持仓</TabsTrigger>
                <TabsTrigger value="orders">订单</TabsTrigger>
                <TabsTrigger value="fills">成交</TabsTrigger>
                <TabsTrigger value="ledger">账本</TabsTrigger>
                <TabsTrigger value="report">报告</TabsTrigger>
              </TabsList>

              <TabsContent value="positions" className="mt-4">
                <DataTableShell
                  isLoading={positionsQuery.isLoading}
                  isError={positionsQuery.isError}
                  error={positionsQuery.error}
                  onRetry={() => positionsQuery.refetch()}
                  isEmpty={positions.length === 0}
                  emptyTitle="暂无持仓"
                  emptyDescription="该会话当前没有任何持仓记录。"
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
                  emptyTitle="暂无订单"
                  emptyDescription="该会话尚未生成任何模拟订单。"
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
                  emptyTitle="暂无成交"
                  emptyDescription="该会话尚未发生任何模拟成交。"
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
                  emptyTitle="暂无账本记录"
                  emptyDescription="该会话尚未产生任何资金账本变动。"
                >
                  <LedgerTable rows={ledger} />
                </DataTableShell>
              </TabsContent>

              <TabsContent value="report" className="mt-4">
                {reportQuery.isLoading ? (
                  <LoadingState rows={4} />
                ) : reportQuery.isError ? (
                  <ErrorState
                    message={errorMessage(reportQuery.error, "报告加载失败")}
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
              <DialogTitle>提交结构化目标仓位</DialogTitle>
              <DialogDescription>
                该入口只写入模拟盘决策并生成模拟订单意图，不连接券商，也不会写入实盘订单、成交或持仓。
              </DialogDescription>
            </DialogHeader>
            <Alert variant="info">
              <AlertDescription>
                服务端会校验来源决策和信号 trace 必须存在于同一个已完成的 <code>RR-</code> 研究运行；页面不允许绕过这项血缘校验。
              </AlertDescription>
            </Alert>
            <div className="space-y-3">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="decision-id">决策 ID</Label>
                  <Input
                    id="decision-id"
                    value={decisionId}
                    onChange={(event) => setDecisionId(event.target.value)}
                    className="font-mono text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="decision-source-run">来源 RR-运行</Label>
                  <Input
                    id="decision-source-run"
                    value={sourceRunId}
                    readOnly
                    placeholder="当前会话未绑定 RR-运行"
                    className="font-mono text-xs"
                  />
                </div>
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="decision-source-id">来源决策 ID</Label>
                  <Input
                    id="decision-source-id"
                    value={sourceDecisionId}
                    onChange={(event) => setSourceDecisionId(event.target.value)}
                    placeholder="研究运行 signals 阶段的 decision_id"
                    className="font-mono text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="decision-trace">信号 trace ID</Label>
                  <Input
                    id="decision-trace"
                    value={signalTraceId}
                    onChange={(event) => setSignalTraceId(event.target.value)}
                    placeholder="必须属于上述 RR-运行"
                    className="font-mono text-xs"
                  />
                </div>
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div className="space-y-2">
                  <Label htmlFor="decision-symbol">标的</Label>
                  <Input
                    id="decision-symbol"
                    value={targetSymbol}
                    onChange={(event) => setTargetSymbol(event.target.value)}
                    placeholder="510300.SH"
                    className="font-mono"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="decision-rule">资产规则</Label>
                  <Select value={targetRuleKey} onValueChange={setTargetRuleKey}>
                    <SelectTrigger id="decision-rule">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {SIMULATION_TARGET_RULES.map((item) => (
                        <SelectItem key={item.key} value={item.key}>
                          {item.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="decision-quantity">目标数量</Label>
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
                <Label htmlFor="decision-reason">决策原因</Label>
                <Input
                  id="decision-reason"
                  value={targetReason}
                  onChange={(event) => setTargetReason(event.target.value)}
                  placeholder="例如：均线金叉，目标仓位由 0 调整为 1000 份"
                />
              </div>
              <p className="text-xs text-muted-foreground">
                当前固定为 A 股多头市价单、GFD；资产规则 {targetRule.key} 会决定手数、T+1、费用和风控参数。
              </p>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setDecisionOpen(false)} disabled={decisionMutation.isPending}>
                取消
              </Button>
              <Button onClick={() => decisionMutation.mutate()} disabled={!decisionValid || decisionMutation.isPending}>
                {decisionMutation.isPending ? "提交中…" : "提交目标仓位"}
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
  return (
    <div className="rounded-lg border border-border">
      <Table>
        <TableHeader className="sticky top-0 bg-card">
          <TableRow>
            <TableHead>标的</TableHead>
            <TableHead>方向</TableHead>
            <TableHead>类型</TableHead>
            <TableHead className="text-right">委托量</TableHead>
            <TableHead className="text-right">已成交</TableHead>
            <TableHead className="text-right">委托价</TableHead>
            <TableHead className="text-right">成交均价</TableHead>
            <TableHead>状态</TableHead>
            <TableHead>时间</TableHead>
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
  return (
    <div className="rounded-lg border border-border">
      <Table>
        <TableHeader className="sticky top-0 bg-card">
          <TableRow>
            <TableHead>标的</TableHead>
            <TableHead>方向</TableHead>
            <TableHead className="text-right">数量</TableHead>
            <TableHead className="text-right">成交价</TableHead>
            <TableHead className="text-right">佣金</TableHead>
            <TableHead className="text-right">税金</TableHead>
            <TableHead>时间</TableHead>
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
  return (
    <div className="rounded-lg border border-border">
      <Table>
        <TableHeader className="sticky top-0 bg-card">
          <TableRow>
            <TableHead>事件</TableHead>
            <TableHead className="text-right">资金变动</TableHead>
            <TableHead className="text-right">变动后资金</TableHead>
            <TableHead>说明</TableHead>
            <TableHead>时间</TableHead>
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
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <MetricCard
          label="总收益"
          value={formatPercent(report.total_return)}
          accent={pnlColor(report.total_return)}
        />
        <MetricCard
          label="年化收益"
          value={formatPercent(report.annual_return)}
          accent={pnlColor(report.annual_return)}
        />
        <MetricCard
          label="夏普比率"
          value={formatNumber(report.sharpe_ratio, 3)}
          accent={pnlColor(report.sharpe_ratio)}
        />
        <MetricCard
          label="最大回撤"
          value={formatPercent(report.max_drawdown)}
          accent="text-destructive"
        />
        <MetricCard
          label="胜率"
          value={formatPercent(report.win_rate)}
        />
        <MetricCard
          label="总成交笔数"
          value={formatNumber(report.total_trades, 0)}
        />
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">权益曲线</CardTitle>
        </CardHeader>
        <CardContent>
          {report.equity_curve.length > 0 ? (
            <div className="rounded-md border border-border">
              <Table>
                <TableHeader className="sticky top-0 bg-card">
                  <TableRow>
                    <TableHead>时间</TableHead>
                    <TableHead className="text-right">权益</TableHead>
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
              title="暂无权益数据"
              description="该会话尚未生成权益曲线。"
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
          <DialogTitle>新建模拟账户</DialogTitle>
          <DialogDescription>
            模拟账户与实盘账户完全隔离，仅用于纸面交易。初始资金仅作模拟记账用途。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-2">
            <Label htmlFor="acct-name">账户名称</Label>
            <Input
              id="acct-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="例如：均线策略模拟盘"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-2">
              <Label htmlFor="acct-cash">初始资金</Label>
              <Input
                id="acct-cash"
                type="number"
                value={initialCash}
                onChange={(e) => setInitialCash(e.target.value)}
                className="tabular-nums"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="acct-currency">币种</Label>
              <Input
                id="acct-currency"
                value={currency}
                onChange={(e) => setCurrency(e.target.value)}
                className="font-mono"
              />
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="acct-actor">操作人</Label>
            <Input
              id="acct-actor"
              value={actor}
              onChange={(e) => setActor(e.target.value)}
              placeholder="例如：analyst@finboard"
              className="font-mono"
            />
          </div>
        </div>
        {mutation.isError && (
          <p className="text-sm text-destructive">
            {errorMessage(mutation.error, "创建账户失败")}
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
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? "创建中…" : "创建账户"}
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
          <DialogTitle>新建模拟会话</DialogTitle>
          <DialogDescription>
            会话基于已发布策略、已完成的机器验证运行和数据发布版本创建。订单只能由结构化目标仓位决策生成，不会直接创建订单。
          </DialogDescription>
        </DialogHeader>
        <Alert variant="info">
          <AlertDescription>
            需要先在「研究运行」中获得 <code>RR-</code> 运行 ID，并填写该运行冻结的数据发布 ID；这两个值用于隔离和审计，不能省略。
          </AlertDescription>
        </Alert>
        <div className="space-y-3">
          <div className="space-y-2">
            <Label htmlFor="sess-account">模拟账户</Label>
            <Select value={accountId} onValueChange={setAccountId}>
              <SelectTrigger id="sess-account">
                <SelectValue placeholder="选择模拟账户" />
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
              <Label htmlFor="sess-strategy">策略 ID</Label>
              <Input
                id="sess-strategy"
                value={strategyId}
                onChange={(e) => setStrategyId(e.target.value)}
                placeholder="例如：ma-cross-v1"
                className="font-mono"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="sess-version">策略版本</Label>
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
              <Label htmlFor="sess-mode">数据源模式</Label>
              <Select value={sourceMode} onValueChange={setSourceMode}>
                <SelectTrigger id="sess-mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="historical_replay">historical_replay（历史回放）</SelectItem>
                  <SelectItem value="readonly_market">readonly_market（只读行情）</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="sess-run">验证运行 ID（必填）</Label>
              <Input
                id="sess-run"
                value={validationRunId}
                onChange={(e) => setValidationRunId(e.target.value)}
                placeholder="RR-...（已完成）"
                className="font-mono"
              />
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="sess-release">数据发布 ID（必填）</Label>
            <Input
              id="sess-release"
              value={dataReleaseId}
              onChange={(e) => setDataReleaseId(e.target.value)}
              placeholder="例如：multi-asset-bars-20260801-v1"
              className="font-mono"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="sess-actor">操作人</Label>
            <Input
              id="sess-actor"
              value={actor}
              onChange={(e) => setActor(e.target.value)}
              placeholder="例如：analyst@finboard"
              className="font-mono"
            />
          </div>
        </div>
        {mutation.isError && (
          <p className="text-sm text-destructive">
            {errorMessage(mutation.error, "创建会话失败")}
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
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? "创建中…" : "创建会话"}
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
          <DialogTitle>推进一根行情</DialogTitle>
          <DialogDescription>
            仅用于历史回放/纸面撮合。先处理一根行情建立价格，再提交目标仓位；在 next-bar-only 规则下，下一根行情才会尝试成交。
          </DialogDescription>
        </DialogHeader>
        <Alert variant="info">
          <AlertDescription>
            同一个 <code>source_event_id</code> 重复提交是幂等的；行情事件不会连接实盘行情或触发真实订单。
          </AlertDescription>
        </Alert>
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="bar-source-event">行情事件 ID</Label>
              <Input
                id="bar-source-event"
                value={sourceEventId}
                onChange={(event) => setSourceEventId(event.target.value)}
                className="font-mono text-xs"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="bar-timestamp">时间</Label>
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
              <Label htmlFor="bar-symbol">标的</Label>
              <Input
                id="bar-symbol"
                value={symbol}
                onChange={(event) => setSymbol(event.target.value)}
                placeholder="510300.SH"
                className="font-mono"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="bar-actor">操作人</Label>
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
              ["bar-open", "开盘", openPrice, setOpenPrice],
              ["bar-high", "最高", highPrice, setHighPrice],
              ["bar-low", "最低", lowPrice, setLowPrice],
              ["bar-close", "收盘", closePrice, setClosePrice],
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
              <Label htmlFor="bar-volume">成交量</Label>
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
              <Label htmlFor="bar-amount">成交额</Label>
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
            <AlertTitle>行情事件未处理</AlertTitle>
            <AlertDescription>
              {errorMessage(mutation.error, "请检查会话状态、时间顺序和 OHLC 数值")}
            </AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={mutation.isPending}>
            取消
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={!valid || mutation.isPending}>
            {mutation.isPending ? "处理中…" : "处理行情"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function Simulation() {
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
        title="模拟盘"
        description="产品模拟盘（纸面交易）：基于结构化目标仓位决策的隔离模拟运行"
        breadcrumbs={[
          { label: "研究", href: "/research" },
          { label: "模拟盘" },
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
            刷新账户
          </Button>
        }
      />
      <WorkflowIndicator currentPath="/research/simulation" />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
        <div className="space-y-4 lg:col-span-1">
          <Alert variant="info">
            <AlertTitle>实盘隔离</AlertTitle>
            <AlertDescription>
              模拟盘与实盘完全隔离。订单只能由结构化目标仓位决策生成，禁止直接创建订单。
            </AlertDescription>
          </Alert>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">模拟账户</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {accountsQuery.isLoading ? (
                <LoadingState rows={2} />
              ) : accountsQuery.isError ? (
                <ErrorState
                  message={errorMessage(accountsQuery.error, "无法加载账户")}
                  onRetry={() => accountsQuery.refetch()}
                />
              ) : accounts.length > 0 ? (
                <>
                  <Select
                    value={selectedAccountId ?? undefined}
                    onValueChange={handleSelectAccount}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder="选择模拟账户" />
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
                        <p className="text-muted-foreground">可用资金</p>
                        <p className="mt-0.5 font-mono tabular-nums">
                          ¥{formatCurrency(selectedAccount.cash, 0)}
                        </p>
                      </div>
                      <div>
                        <p className="text-muted-foreground">总权益</p>
                        <p className="mt-0.5 font-mono tabular-nums">
                          ¥{formatCurrency(selectedAccount.equity, 0)}
                        </p>
                      </div>
                      <div>
                        <p className="text-muted-foreground">冻结资金</p>
                        <p className="mt-0.5 font-mono tabular-nums text-muted-foreground">
                          ¥{formatCurrency(selectedAccount.frozen_cash, 0)}
                        </p>
                      </div>
                      <div>
                        <p className="text-muted-foreground">状态</p>
                        <p className="mt-0.5">
                          <StatusBadge status={selectedAccount.status} />
                        </p>
                      </div>
                    </div>
                  )}
                </>
              ) : (
                <EmptyState
                  title="暂无模拟账户"
                  description="点击下方按钮创建第一个模拟账户。"
                />
              )}
              <Button
                variant="outline"
                size="sm"
                className="w-full"
                onClick={() => setAccountDialogOpen(true)}
              >
                <Plus className="h-4 w-4" />
                新建账户
              </Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base">会话列表</CardTitle>
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
                  title="请先选择账户"
                  description="选择模拟账户后将展示该账户下的全部模拟会话。"
                />
              ) : sessionsQuery.isLoading ? (
                <LoadingState rows={4} />
              ) : sessionsQuery.isError ? (
                <ErrorState
                  message={errorMessage(sessionsQuery.error, "无法加载会话")}
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
                          <span>{timeAgo(s.created_at)}</span>
                        </div>
                      </button>
                    ))}
                  </div>
                </ScrollArea>
              ) : (
                <EmptyState
                  icon={<FlaskConical className="h-8 w-8" />}
                  title="暂无会话"
                  description="该账户下还没有模拟会话，可点击下方按钮创建。"
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
                新建会话
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
              title="请从左侧选择一个模拟会话"
              description="选中会话后将展示持仓、订单、成交、账本与绩效报告，并支持启动 / 暂停 / 停止 / 归档操作。"
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
        nextLabel="研究报告"
        description="查看模拟交易的完整绩效报告和归因分析"
      />
    </div>
  );
}
