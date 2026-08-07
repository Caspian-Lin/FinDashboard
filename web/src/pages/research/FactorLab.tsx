import * as React from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Atom,
  CalendarDays,
  Info,
  Layers,
  Signal,
  Sparkles,
  TestTube,
  RefreshCw,
  Plus,
  ChevronLeft,
  ChevronRight,
  Check,
} from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { Link } from "react-router-dom";
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
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  ResearchHint,
  WorkflowIndicator,
  NextStepCTA,
} from "@/components/research/ResearchHint";
import { FeatureSnapshotProgress } from "@/components/research/FeatureSnapshotProgress";
import { RESEARCH_HINTS } from "@/lib/research-hints";
import {
  factorLabApi,
  datasetApi,
  type FactorCatalogEntry,
  type FeatureSnapshot,
  type FeatureSnapshotJobStatus,
  type FactorSignal,
  type FactorExperiment,
  type FactorExperimentCreate,
  type DatasetReleaseSummary,
  featureSnapshotCreatedAt,
  featureSnapshotNames,
  featureSnapshotObservationCount,
  featureSnapshotStatus,
  featureSnapshotSymbolCount,
} from "@/lib/research";
import { cn, formatDateTime, formatNumber } from "@/lib/utils";

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message) return err.message;
  if (typeof err === "string" && err) return err;
  return fallback;
}

const FACTOR_LABELS: Record<string, string> = {
  pb: "市净率",
  earnings_yield: "盈利收益率",
  dividend_yield: "股息率",
  roe: "净资产收益率",
  gross_profit_margin: "毛利率",
  debt_to_assets: "资产负债率",
  revenue_yoy: "营业收入同比",
  momentum: "20 日动量",
  volatility_20d: "20 日波动率",
  volatility_60d: "60 日波动率",
  volatility_120d: "120 日波动率",
  downside_volatility: "下行波动率",
  turnover_rate: "换手率",
  market_beta: "市场贝塔",
  industry_exposure: "行业暴露",
  asset_class_exposure: "资产类别暴露",
  size_exposure: "规模暴露",
  volatility_exposure: "波动率暴露",
  liquidity_exposure: "流动性暴露",
  risk_free_rate: "无风险利率",
  government_bond_return: "国债收益",
  fx_usdcny_return: "美元兑人民币收益",
  gold_return: "黄金收益",
  market_breadth: "市场宽度",
  volatility_regime: "波动状态",
};

const ROLE_LABELS: Record<string, string> = {
  alpha: "Alpha",
  risk: "风险暴露",
  market_input: "市场输入",
};

const PREFERENCE_LABELS: Record<string, string> = {
  higher: "值高更优",
  lower: "值低更优",
  exposure_only: "仅作暴露",
};

const FREQUENCY_LABELS: Record<string, string> = {
  daily: "日频",
  report: "财报期",
  event: "事件驱动",
};

const MISSING_POLICY_LABELS: Record<string, string> = {
  fail_closed: "缺失即阻断",
  exclude: "缺失剔除",
  forward_fill: "向前填充",
  cross_section_median: "截面中位数",
};

const CATALOG_PAGE_SIZE = 10;

function factorLabel(name: string): string {
  return FACTOR_LABELS[name] ?? name;
}

function formatFactorWindow(window: number | null): string {
  return window ? `${window} 日窗口` : "单期值";
}

function CatalogTab() {
  const [expandedName, setExpandedName] = React.useState<string | null>(null);
  const [catalogPage, setCatalogPage] = React.useState(1);
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["factor-catalog"],
    queryFn: () => factorLabApi.catalog(),
  });
  const totalFactors = data?.length ?? 0;
  const pageCount = Math.max(1, Math.ceil(totalFactors / CATALOG_PAGE_SIZE));
  const pageStartIndex = (catalogPage - 1) * CATALOG_PAGE_SIZE;
  const visibleFactors = data?.slice(
    pageStartIndex,
    pageStartIndex + CATALOG_PAGE_SIZE,
  ) ?? [];
  const visibleStart = totalFactors === 0 ? 0 : pageStartIndex + 1;
  const visibleEnd = Math.min(
    pageStartIndex + CATALOG_PAGE_SIZE,
    totalFactors,
  );

  React.useEffect(() => {
    setCatalogPage((current) => Math.min(current, pageCount));
  }, [pageCount]);

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data
            ? `共 ${totalFactors} 个因子 · 第 ${catalogPage}/${pageCount} 页`
            : "加载中…"}
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

      <Alert className="mb-4">
        <Info className="h-4 w-4" />
        <AlertTitle>如何理解因子目录</AlertTitle>
          <AlertDescription>
            目录项描述的是可复现的研究输入，不是买卖指令。Alpha 因子需要经过
            OOS 验证才能进入后续策略研究；风险暴露和市场输入只用于解释、约束或状态分层。
            点击行可展开完整口径，表格较窄时可横向滚动查看全部字段。
          </AlertDescription>
      </Alert>

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
        <div className="overflow-hidden rounded-lg border border-border">
          <div className="max-h-[560px] overflow-auto scrollbar-thin">
            <Table className="min-w-[900px]">
              <TableHeader className="sticky top-0 bg-card">
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>因子</TableHead>
                  <TableHead>经济含义</TableHead>
                  <TableHead>计算口径</TableHead>
                  <TableHead>预期失效</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visibleFactors.map((factor: FactorCatalogEntry) => {
                  const open = expandedName === factor.name;
                  return (
                    <React.Fragment key={factor.name}>
                      <TableRow
                        className="cursor-pointer align-top hover:bg-muted/50"
                        onClick={() => setExpandedName(open ? null : factor.name)}
                      >
                        <TableCell className="p-2">
                          <ChevronRight
                            className={cn(
                              "h-4 w-4 text-muted-foreground transition-transform",
                              open && "rotate-90",
                            )}
                          />
                        </TableCell>
                        <TableCell className="min-w-[180px] whitespace-normal">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="font-medium">{factorLabel(factor.name)}</span>
                            <StatusBadge status={factor.role}>
                              {ROLE_LABELS[factor.role] ?? factor.role}
                            </StatusBadge>
                            {factor.signal_eligible && (
                              <Badge variant="secondary" className="text-[10px]">
                                可做信号
                              </Badge>
                            )}
                          </div>
                          <p className="mt-1 font-mono text-xs text-muted-foreground">
                            {factor.name} · v{factor.version}
                          </p>
                        </TableCell>
                        <TableCell className="min-w-[260px] max-w-[360px] whitespace-normal break-words text-sm leading-5">
                          {factor.economic_hypothesis}
                        </TableCell>
                        <TableCell className="min-w-[220px] whitespace-normal break-words text-xs leading-5 text-muted-foreground">
                          <p>
                            {FREQUENCY_LABELS[factor.frequency] ?? factor.frequency} ·{" "}
                            {formatFactorWindow(factor.calculation_window)} ·{" "}
                            {PREFERENCE_LABELS[factor.preference] ?? factor.preference}
                          </p>
                          <p className="mt-1 break-all font-mono">{factor.source_fields.join(" · ")}</p>
                        </TableCell>
                        <TableCell className="min-w-[260px] max-w-[360px] whitespace-normal break-words text-sm leading-5 text-muted-foreground">
                          {factor.expected_failure}
                        </TableCell>
                      </TableRow>
                      {open && (
                        <TableRow className="bg-muted/30 hover:bg-muted/30">
                          <TableCell colSpan={5} className="p-4">
                            <div className="grid gap-4 text-xs md:grid-cols-2 xl:grid-cols-4">
                              <div>
                                <p className="font-medium text-foreground">数据可用规则</p>
                                <p className="mt-1 leading-5 text-muted-foreground">
                                  {factor.available_at_rule}
                                </p>
                              </div>
                              <div>
                                <p className="font-medium text-foreground">缺失值策略</p>
                                <p className="mt-1 text-muted-foreground">
                                  {MISSING_POLICY_LABELS[factor.missing_policy] ?? factor.missing_policy}
                                </p>
                              </div>
                              <div>
                                <p className="font-medium text-foreground">转换 / 中性化</p>
                                <p className="mt-1 font-mono text-muted-foreground">
                                  {factor.default_transform} ·{" "}
                                  {factor.default_neutralization.length > 0
                                    ? factor.default_neutralization.join(" · ")
                                    : "不做中性化"}
                                </p>
                              </div>
                              <div>
                                <p className="font-medium text-foreground">单位 / 研究边界</p>
                                <p className="mt-1 text-muted-foreground">
                                  {factor.unit} · {factor.signal_eligible ? "可在 OOS 通过后进入信号" : "不可直接生成信号"}
                                </p>
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
          </div>
          <div className="flex flex-col gap-3 border-t border-border px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
            <p className="text-xs text-muted-foreground">
              显示 {visibleStart}–{visibleEnd} / {totalFactors} 个因子，点击行可展开完整说明
            </p>
            <div className="flex items-center justify-between gap-2 sm:justify-end">
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setCatalogPage((current) => Math.max(1, current - 1));
                  setExpandedName(null);
                }}
                disabled={catalogPage <= 1}
              >
                <ChevronLeft className="h-4 w-4" />
                上一页
              </Button>
              <span className="min-w-[72px] text-center text-sm tabular-nums text-muted-foreground">
                第 {catalogPage} / {pageCount} 页
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setCatalogPage((current) => Math.min(pageCount, current + 1));
                  setExpandedName(null);
                }}
                disabled={catalogPage >= pageCount}
              >
                下一页
                <ChevronRight className="h-4 w-4" />
              </Button>
            </div>
          </div>
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

function GenerateFeatureSnapshotDialog({
  open,
  onOpenChange,
  onGenerated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onGenerated: (snapshot: import("@/lib/research").FeatureSnapshot) => void;
}) {
  const [releaseId, setReleaseId] = React.useState("");
  const [decisionDate, setDecisionDate] = React.useState("");
  const [jobId, setJobId] = React.useState<string | null>(null);
  const handledSnapshotId = React.useRef<string | null>(null);
  const releasesQuery = useQuery({
    queryKey: ["dataset-releases", "feature-snapshot-generator"],
    queryFn: () => datasetApi.releases({ limit: 100 }),
    enabled: open,
  });
  const selectedRelease = releasesQuery.data?.find((item) => item.release_id === releaseId);
  const mutation = useMutation({
    mutationFn: () => {
      if (!releaseId || !decisionDate) {
        throw new Error("请选择数据发布和决策日");
      }
      return factorLabApi.startFeatureSnapshotJob({
        dataset_release_id: releaseId,
        decision_at: `${decisionDate}T23:59:59+08:00`,
      });
    },
    onSuccess: (job) => setJobId(job.job_id),
  });
  const jobQuery = useQuery<FeatureSnapshotJobStatus>({
    queryKey: ["feature-snapshot-job", jobId],
    queryFn: () => factorLabApi.featureSnapshotJob(jobId as string),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return !status || status === "queued" || status === "running"
        ? 1000
        : false;
    },
  });
  const completedSnapshotId =
    jobQuery.data?.status === "succeeded"
      ? jobQuery.data.snapshot_id
      : null;
  const snapshotQuery = useQuery({
    queryKey: ["feature-snapshot-detail", completedSnapshotId],
    queryFn: () => factorLabApi.featureDetail(completedSnapshotId as string),
    enabled: Boolean(completedSnapshotId),
  });
  const jobActive =
    Boolean(jobId) &&
    (!jobQuery.data ||
      jobQuery.data.status === "queued" ||
      jobQuery.data.status === "running");
  const busy = mutation.isPending || jobActive;

  React.useEffect(() => {
    if (
      snapshotQuery.data &&
      handledSnapshotId.current !== snapshotQuery.data.snapshot_id
    ) {
      handledSnapshotId.current = snapshotQuery.data.snapshot_id;
      onGenerated(snapshotQuery.data);
      onOpenChange(false);
    }
  }, [onGenerated, onOpenChange, snapshotQuery.data]);

  const resetMutation = mutation.reset;

  React.useEffect(() => {
    if (!open) {
      setReleaseId("");
      setDecisionDate("");
      setJobId(null);
      handledSnapshotId.current = null;
      resetMutation();
      return;
    }
    const first = releasesQuery.data?.[0];
    if (!releaseId && first) {
      setReleaseId(first.release_id);
      setDecisionDate(first.end_date);
    }
  }, [open, releaseId, releasesQuery.data, resetMutation]);

  const valid =
    !!selectedRelease &&
    selectedRelease.quality_status !== "failed" &&
    !!decisionDate &&
    decisionDate >= selectedRelease.start_date &&
    decisionDate <= selectedRelease.end_date;

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen && busy) return;
        onOpenChange(nextOpen);
      }}
    >
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>生成特征快照</DialogTitle>
          <DialogDescription>
            从已发布且不可变的数据版本计算价格特征，并保存为可供实验与研究运行引用的快照。
          </DialogDescription>
        </DialogHeader>

        <Alert>
          <CalendarDays className="h-4 w-4" />
          <AlertTitle>本次生成的内容</AlertTitle>
          <AlertDescription>
            决策日按 A 股收盘后 23:59:59 处理，默认计算 20 日动量、20/60/120 日波动率和
            60 日下行波动率。所有观测都必须满足 available_at 不晚于决策时点。
          </AlertDescription>
        </Alert>

        {releasesQuery.isError ? (
          <Alert variant="destructive">
            <AlertTitle>数据发布加载失败</AlertTitle>
            <AlertDescription>{errorMessage(releasesQuery.error, "无法加载可用数据发布")}</AlertDescription>
          </Alert>
        ) : releasesQuery.data && releasesQuery.data.length === 0 ? (
          <Alert variant="warning">
            <AlertTitle>还没有可用数据发布</AlertTitle>
            <AlertDescription>
              请先到 <Link className="underline" to="/research/data">数据与标的</Link> 冻结并发布数据，再生成特征快照。
            </AlertDescription>
          </Alert>
        ) : (
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="feature-snapshot-release">数据发布</Label>
              <Select
                value={releaseId}
                onValueChange={(value) => {
                  setReleaseId(value);
                  const next = releasesQuery.data?.find((item) => item.release_id === value);
                  setDecisionDate(next?.end_date ?? "");
                }}
              >
                <SelectTrigger id="feature-snapshot-release">
                  <SelectValue placeholder="选择已发布数据版本" />
                </SelectTrigger>
                <SelectContent>
                  {(releasesQuery.data ?? []).map((release: DatasetReleaseSummary) => (
                    <SelectItem key={release.release_id} value={release.release_id}>
                      {release.dataset_name} · {release.version} · {release.quality_status}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {selectedRelease && (
                <p className="text-xs text-muted-foreground">
                  范围 {selectedRelease.start_date} ~ {selectedRelease.end_date} · {selectedRelease.symbol_count} 个标的 · 覆盖 {selectedRelease.coverage_pct}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="feature-snapshot-decision-date">决策日（收盘后）</Label>
              <Input
                id="feature-snapshot-decision-date"
                type="date"
                value={decisionDate}
                min={selectedRelease?.start_date}
                max={selectedRelease?.end_date}
                onChange={(event) => setDecisionDate(event.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                不能超出数据发布范围，也不能晚于当前时间。
              </p>
            </div>
          </div>
        )}

        {mutation.isError && (
          <Alert variant="destructive">
            <AlertTitle>快照生成失败</AlertTitle>
            <AlertDescription>{errorMessage(mutation.error, "请检查数据发布和历史数据覆盖")}</AlertDescription>
          </Alert>
        )}
        {jobQuery.isError && (
          <Alert variant="destructive">
            <AlertTitle>进度查询失败</AlertTitle>
            <AlertDescription>{errorMessage(jobQuery.error, "暂时无法读取任务状态")}</AlertDescription>
          </Alert>
        )}
        {jobQuery.data && <FeatureSnapshotProgress job={jobQuery.data} />}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
            取消
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={!valid || busy}>
            <Sparkles className="mr-2 h-4 w-4" />
            {busy ? "计算并发布中…" : "计算并发布快照"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function FeaturesTab() {
  const [expandedId, setExpandedId] = React.useState<string | null>(null);
  const [generateOpen, setGenerateOpen] = React.useState(false);
  const [generatedSnapshot, setGeneratedSnapshot] = React.useState<import("@/lib/research").FeatureSnapshot | null>(null);
  const queryClient = useQueryClient();
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["feature-snapshots", { limit: 50 }],
    queryFn: () => factorLabApi.features(undefined, 50),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {data ? `共 ${data.length} 个特征快照` : "加载中…"}
        </p>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
            刷新
          </Button>
          <Button size="sm" onClick={() => setGenerateOpen(true)}>
            <Sparkles className="h-4 w-4" />
            生成快照
          </Button>
        </div>
      </div>

      <Alert className="mb-4">
        <Layers className="h-4 w-4" />
        <AlertTitle>快照是研究输入的不可变切片</AlertTitle>
        <AlertDescription>
          它绑定一个数据发布、一个决策时点、计算窗口、每个标的的特征值和 checksum。
          快照生成成功后，才能在因子实验和研究运行中选择；生成过程不会启动回测或交易。
        </AlertDescription>
      </Alert>

      {generatedSnapshot && (
        <Alert variant="success" className="mb-4" aria-live="polite">
          <Sparkles className="h-4 w-4" />
          <AlertTitle>特征快照已生成并发布</AlertTitle>
          <AlertDescription>
            <span className="font-mono">{generatedSnapshot.snapshot_id}</span> ·{" "}
            {featureSnapshotSymbolCount(generatedSnapshot)} 个标的 ·{" "}
            {featureSnapshotNames(generatedSnapshot).length} 个因子。现在可以创建因子实验或排队研究运行。
          </AlertDescription>
        </Alert>
      )}

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
                          {featureSnapshotNames(snap).length}
                        </TableCell>
                        <TableCell className="tabular-nums text-right">
                          {formatNumber(featureSnapshotSymbolCount(snap), 0)}
                        </TableCell>
                        <TableCell className="tabular-nums text-right">
                          {formatNumber(featureSnapshotObservationCount(snap), 0)}
                        </TableCell>
                        <TableCell>
                          <StatusBadge status={featureSnapshotStatus(snap)} />
                        </TableCell>
                        <TableCell className="tabular-nums text-muted-foreground">
                          {formatDateTime(featureSnapshotCreatedAt(snap))}
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
                                    {formatNumber(featureSnapshotSymbolCount(snap), 0)}
                                  </span>
                                </span>
                                <span>
                                  行数：
                                  <span className="font-mono text-foreground">
                                    {formatNumber(featureSnapshotObservationCount(snap), 0)}
                                  </span>
                                </span>
                                <span>
                                  状态：
                                  <StatusBadge status={featureSnapshotStatus(snap)} />
                                </span>
                                <span>决策时点：<span className="font-mono text-foreground">{formatDateTime(snap.decision_at)}</span></span>
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
                                  因子列表（{featureSnapshotNames(snap).length}）
                                </p>
                                <div className="flex flex-wrap gap-1">
                                  {featureSnapshotNames(snap).map((f) => (
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
                              {snap.issues.length > 0 && (
                                <Alert variant="warning">
                                  <AlertTitle>质量提示</AlertTitle>
                                  <AlertDescription>{snap.issues.join("；")}</AlertDescription>
                                </Alert>
                              )}
                              <p className="text-xs text-muted-foreground">
                                计算窗口：{Object.entries(snap.calculation_windows)
                                  .map(([name, window]) => `${name}=${window}日`)
                                  .join(" · ") || "无"}
                                · 代码版本 <span className="font-mono">{snap.code_version}</span>
                              </p>
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
          description="当前数据库中没有已发布快照。选择一个已发布数据版本和决策日，显式生成后才能用于因子实验和研究运行。"
          action={
            <Button size="sm" onClick={() => setGenerateOpen(true)}>
              <Sparkles className="h-4 w-4" />
              生成第一份快照
            </Button>
          }
        />
      )}

      <GenerateFeatureSnapshotDialog
        open={generateOpen}
        onOpenChange={setGenerateOpen}
        onGenerated={(snapshot) => {
          setGeneratedSnapshot(snapshot);
          void queryClient.invalidateQueries({ queryKey: ["feature-snapshots"] });
        }}
      />
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
  const resetMutation = mutation.reset;

  React.useEffect(() => {
    if (!open) {
      setForm(DEFAULT_FORM);
      resetMutation();
    }
  }, [open, resetMutation]);

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

        {releases && releases.length === 0 && (
          <Alert variant="warning">
            <AlertTitle>缺少数据发布</AlertTitle>
            <AlertDescription>
              先到 <Link className="underline" to="/research/data">数据与标的</Link> 创建不可变数据发布，再回来绑定因子实验。
            </AlertDescription>
          </Alert>
        )}
        {features && features.length === 0 && (
          <Alert variant="warning">
            <AlertTitle>缺少特征快照</AlertTitle>
            <AlertDescription>
              因子实验必须绑定已发布的 FeatureSnapshot。当前没有可用快照，请先在「特征快照」页完成生成/发布；仅有因子目录不能直接创建实验。
            </AlertDescription>
          </Alert>
        )}

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
                        {featureSnapshotNames(s).length} 因子
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
