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
  WorkflowHelpPopover,
  WORKFLOW_NEXT,
} from "@/components/research/ResearchHint";
import { FeatureSnapshotProgress } from "@/components/research/FeatureSnapshotProgress";
import { RESEARCH_HINTS } from "@/lib/research-hints";
import {
  factorLabApi,
  datasetApi,
  type FactorCatalogEntry,
  type FeatureSnapshot,
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
import { api, isJobRunning } from "@/lib/api";
import { cn, formatDateTime, formatNumber } from "@/lib/utils";
import { useT, type LocalizedText } from "@/i18n";

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message) return err.message;
  if (typeof err === "string" && err) return err;
  return fallback;
}

const FACTOR_LABELS: Record<string, LocalizedText> = {
  pb: { zh: "市净率", en: "P/B" },
  earnings_yield: { zh: "盈利收益率", en: "Earnings yield" },
  dividend_yield: { zh: "股息率", en: "Dividend yield" },
  roe: { zh: "净资产收益率", en: "ROE" },
  gross_profit_margin: { zh: "毛利率", en: "Gross profit margin" },
  debt_to_assets: { zh: "资产负债率", en: "Debt-to-assets ratio" },
  revenue_yoy: { zh: "营业收入同比", en: "Revenue YoY" },
  momentum: { zh: "20 日动量", en: "20-day momentum" },
  volatility_20d: { zh: "20 日波动率", en: "20-day volatility" },
  volatility_60d: { zh: "60 日波动率", en: "60-day volatility" },
  volatility_120d: { zh: "120 日波动率", en: "120-day volatility" },
  downside_volatility: { zh: "下行波动率", en: "Downside volatility" },
  turnover_rate: { zh: "换手率", en: "Turnover" },
  market_beta: { zh: "市场贝塔", en: "Market beta" },
  industry_exposure: { zh: "行业暴露", en: "Industry exposure" },
  asset_class_exposure: { zh: "资产类别暴露", en: "Asset class exposure" },
  size_exposure: { zh: "规模暴露", en: "Size exposure" },
  volatility_exposure: { zh: "波动率暴露", en: "Volatility exposure" },
  liquidity_exposure: { zh: "流动性暴露", en: "Liquidity exposure" },
  risk_free_rate: { zh: "无风险利率", en: "Risk-free rate" },
  government_bond_return: { zh: "国债收益", en: "Government bond return" },
  fx_usdcny_return: { zh: "美元兑人民币收益", en: "USD/CNY return" },
  gold_return: { zh: "黄金收益", en: "Gold return" },
  market_breadth: { zh: "市场宽度", en: "Market breadth" },
  volatility_regime: { zh: "波动状态", en: "Volatility regime" },
};

const ROLE_LABELS: Record<string, LocalizedText> = {
  alpha: { zh: "Alpha", en: "Alpha" },
  risk: { zh: "风险暴露", en: "Risk exposure" },
  market_input: { zh: "市场输入", en: "Market input" },
};

const PREFERENCE_LABELS: Record<string, LocalizedText> = {
  higher: { zh: "值高更优", en: "Higher is better" },
  lower: { zh: "值低更优", en: "Lower is better" },
  exposure_only: { zh: "仅作暴露", en: "Exposure only" },
};

const FREQUENCY_LABELS: Record<string, LocalizedText> = {
  daily: { zh: "日频", en: "Daily" },
  report: { zh: "财报期", en: "Report period" },
  event: { zh: "事件驱动", en: "Event-driven" },
};

const MISSING_POLICY_LABELS: Record<string, LocalizedText> = {
  fail_closed: { zh: "缺失即阻断", en: "Fail closed on missing" },
  exclude: { zh: "缺失剔除", en: "Exclude missing" },
  forward_fill: { zh: "向前填充", en: "Forward fill" },
  cross_section_median: { zh: "截面中位数", en: "Cross-section median" },
};

const CATALOG_PAGE_SIZE = 10;

function factorLabel(name: string): LocalizedText {
  return FACTOR_LABELS[name] ?? { zh: name, en: name };
}

function formatFactorWindow(window: number | null): LocalizedText {
  return window
    ? { zh: `${window} 日窗口`, en: `${window}-day window` }
    : { zh: "单期值", en: "Single-period value" };
}

function CatalogTab() {
  const { tl } = useT();
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
            ? tl({
                zh: `共 ${totalFactors} 个因子 · 第 ${catalogPage}/${pageCount} 页`,
                en: `${totalFactors} factors · Page ${catalogPage}/${pageCount}`,
              })
            : tl({ zh: "加载中…", en: "Loading…" })}
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          {tl({ zh: "刷新", en: "Refresh" })}
        </Button>
      </div>

      <Alert className="mb-4">
        <Info className="h-4 w-4" />
        <AlertTitle>{tl({ zh: "如何理解因子目录", en: "How to read the factor catalog" })}</AlertTitle>
          <AlertDescription>
            {tl({
              zh: "目录项描述的是可复现的研究输入，不是买卖指令。Alpha 因子需要经过 OOS 验证才能进入后续策略研究；风险暴露和市场输入只用于解释、约束或状态分层。点击行可展开完整口径，表格较窄时可横向滚动查看全部字段。",
              en: "Catalog entries describe reproducible research inputs, not trading instructions. Alpha factors must pass OOS validation before entering downstream strategy research; risk exposures and market inputs are only used for explanation, constraints, or regime layering. Click a row to expand the full specification; scroll horizontally when the table is narrow.",
            })}
          </AlertDescription>
      </Alert>

      {isLoading ? (
        <LoadingState rows={6} />
      ) : isError ? (
        <EmptyState
          icon={<Atom className="h-8 w-8" />}
          title={tl({ zh: "加载失败", en: "Failed to load" })}
          description={
            error instanceof Error ? error.message : tl({ zh: "无法加载因子目录", en: "Unable to load the factor catalog" })
          }
        />
      ) : data && data.length > 0 ? (
        <div className="overflow-hidden rounded-lg border border-border">
          <div className="max-h-[560px] overflow-auto scrollbar-thin">
            <Table className="min-w-[900px]">
              <TableHeader className="sticky top-0 bg-card">
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>{tl({ zh: "因子", en: "Factor" })}</TableHead>
                  <TableHead>{tl({ zh: "经济含义", en: "Economic rationale" })}</TableHead>
                  <TableHead>{tl({ zh: "计算口径", en: "Calculation" })}</TableHead>
                  <TableHead>{tl({ zh: "预期失效", en: "Expected failure" })}</TableHead>
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
                            <span className="font-medium">{tl(factorLabel(factor.name))}</span>
                            <StatusBadge status={factor.role}>
                              {ROLE_LABELS[factor.role]
                                ? tl(ROLE_LABELS[factor.role])
                                : factor.role}
                            </StatusBadge>
                            {factor.signal_eligible && (
                              <Badge variant="secondary" className="text-[10px]">
                                {tl({ zh: "可做信号", en: "Signal eligible" })}
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
                            {FREQUENCY_LABELS[factor.frequency] ? tl(FREQUENCY_LABELS[factor.frequency]) : factor.frequency} ·{" "}
                            {tl(formatFactorWindow(factor.calculation_window))} ·{" "}
                            {PREFERENCE_LABELS[factor.preference] ? tl(PREFERENCE_LABELS[factor.preference]) : factor.preference}
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
                                <p className="font-medium text-foreground">{tl({ zh: "数据可用规则", en: "Data availability rule" })}</p>
                                <p className="mt-1 leading-5 text-muted-foreground">
                                  {factor.available_at_rule}
                                </p>
                              </div>
                              <div>
                                <p className="font-medium text-foreground">{tl({ zh: "缺失值策略", en: "Missing value policy" })}</p>
                                <p className="mt-1 text-muted-foreground">
                                  {MISSING_POLICY_LABELS[factor.missing_policy]
                                    ? tl(MISSING_POLICY_LABELS[factor.missing_policy])
                                    : factor.missing_policy}
                                </p>
                              </div>
                              <div>
                                <p className="font-medium text-foreground">{tl({ zh: "转换 / 中性化", en: "Transform / Neutralization" })}</p>
                                <p className="mt-1 font-mono text-muted-foreground">
                                  {factor.default_transform} ·{" "}
                                  {factor.default_neutralization.length > 0
                                    ? factor.default_neutralization.join(" · ")
                                    : tl({ zh: "不做中性化", en: "No neutralization" })}
                                </p>
                              </div>
                              <div>
                                <p className="font-medium text-foreground">{tl({ zh: "单位 / 研究边界", en: "Unit / Research boundary" })}</p>
                                <p className="mt-1 text-muted-foreground">
                                  {factor.unit} · {factor.signal_eligible
                                    ? tl({ zh: "可在 OOS 通过后进入信号", en: "Eligible for signals after passing OOS" })
                                    : tl({ zh: "不可直接生成信号", en: "Cannot generate signals directly" })}
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
              {tl({
                zh: `显示 ${visibleStart}–${visibleEnd} / ${totalFactors} 个因子，点击行可展开完整说明`,
                en: `Showing ${visibleStart}–${visibleEnd} of ${totalFactors} factors, click a row for the full specification`,
              })}
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
                {tl({ zh: "上一页", en: "Previous" })}
              </Button>
              <span className="min-w-[72px] text-center text-sm tabular-nums text-muted-foreground">
                {tl({ zh: `第 ${catalogPage} / ${pageCount} 页`, en: `Page ${catalogPage} / ${pageCount}` })}
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
                {tl({ zh: "下一页", en: "Next" })}
                <ChevronRight className="h-4 w-4" />
              </Button>
            </div>
          </div>
        </div>
      ) : (
        <EmptyState
          icon={<Atom className="h-8 w-8" />}
          title={tl({ zh: "暂无因子", en: "No factors yet" })}
          description={tl({
            zh: "因子目录注册后将在此列出，包含角色、版本与依赖关系。",
            en: "Registered factors will be listed here with role, version, and dependencies.",
          })}
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
  const { tl } = useT();
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
        throw new Error(tl({ zh: "请选择数据发布和决策日", en: "Select a dataset release and a decision date" }));
      }
      return factorLabApi.startFeatureSnapshotJob({
        dataset_release_id: releaseId,
        decision_at: `${decisionDate}T23:59:59+08:00`,
      });
    },
    onSuccess: (job) => setJobId(job.job_id),
  });
  const jobQuery = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.getJob(jobId as string),
    enabled: Boolean(jobId),
    refetchInterval: (query) => (isJobRunning(query.state.data) ? 1000 : false),
  });
  // 任务成功后 result_ref 是 snapshot_id(FeatureSnapshot.snapshot_id 字符串)。
  const completedSnapshotId =
    jobQuery.data && !isJobRunning(jobQuery.data) && jobQuery.data.status === "succeeded"
      ? jobQuery.data.result_ref
      : null;
  const snapshotQuery = useQuery({
    queryKey: ["feature-snapshot-detail", completedSnapshotId],
    queryFn: () => factorLabApi.featureDetail(completedSnapshotId as string),
    enabled: Boolean(completedSnapshotId),
  });
  const jobActive = Boolean(jobId) && isJobRunning(jobQuery.data);
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
          <DialogTitle>{tl({ zh: "生成特征快照", en: "Generate feature snapshot" })}</DialogTitle>
          <DialogDescription>
            {tl({
              zh: "从已发布且不可变的数据版本计算价格特征，并保存为可供实验与研究运行引用的快照。",
              en: "Compute price features from a published, immutable data version and save them as a snapshot that experiments and research runs can reference.",
            })}
          </DialogDescription>
        </DialogHeader>

        <Alert>
          <CalendarDays className="h-4 w-4" />
          <AlertTitle>{tl({ zh: "本次生成的内容", en: "What this generates" })}</AlertTitle>
          <AlertDescription>
            {tl({
              zh: "决策日按 A 股收盘后 23:59:59 处理，默认计算 20 日动量、20/60/120 日波动率和 60 日下行波动率。所有观测都必须满足 available_at 不晚于决策时点。",
              en: "Decision dates are treated as 23:59:59 after A-share market close; by default this computes 20-day momentum, 20/60/120-day volatility, and 60-day downside volatility. All observations must satisfy available_at no later than the decision time.",
            })}
          </AlertDescription>
        </Alert>

        {releasesQuery.isError ? (
          <Alert variant="destructive">
            <AlertTitle>{tl({ zh: "数据发布加载失败", en: "Failed to load dataset releases" })}</AlertTitle>
            <AlertDescription>{errorMessage(releasesQuery.error, tl({ zh: "无法加载可用数据发布", en: "Unable to load available dataset releases" }))}</AlertDescription>
          </Alert>
        ) : releasesQuery.data && releasesQuery.data.length === 0 ? (
          <Alert variant="warning">
            <AlertTitle>{tl({ zh: "还没有可用数据发布", en: "No dataset releases yet" })}</AlertTitle>
            <AlertDescription>
              {tl({ zh: "请先到 ", en: "Go to " })}
              <Link className="underline" to="/research/data">{tl({ zh: "数据与标的", en: "Data & Instruments" })}</Link>
              {tl({ zh: " 冻结并发布数据，再生成特征快照。", en: " to freeze and publish data before generating a feature snapshot." })}
            </AlertDescription>
          </Alert>
        ) : (
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="feature-snapshot-release">{tl({ zh: "数据发布", en: "Dataset release" })}</Label>
              <Select
                value={releaseId}
                onValueChange={(value) => {
                  setReleaseId(value);
                  const next = releasesQuery.data?.find((item) => item.release_id === value);
                  setDecisionDate(next?.end_date ?? "");
                }}
              >
                <SelectTrigger id="feature-snapshot-release">
                  <SelectValue placeholder={tl({ zh: "选择已发布数据版本", en: "Select a published data version" })} />
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
                  {tl({
                    zh: `范围 ${selectedRelease.start_date} ~ ${selectedRelease.end_date} · ${selectedRelease.symbol_count} 个标的 · 覆盖 ${selectedRelease.coverage_pct}`,
                    en: `Range ${selectedRelease.start_date} ~ ${selectedRelease.end_date} · ${selectedRelease.symbol_count} symbols · coverage ${selectedRelease.coverage_pct}`,
                  })}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="feature-snapshot-decision-date">{tl({ zh: "决策日（收盘后）", en: "Decision date (after close)" })}</Label>
              <Input
                id="feature-snapshot-decision-date"
                type="date"
                value={decisionDate}
                min={selectedRelease?.start_date}
                max={selectedRelease?.end_date}
                onChange={(event) => setDecisionDate(event.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                {tl({ zh: "不能超出数据发布范围，也不能晚于当前时间。", en: "Must stay within the release range and cannot be later than now." })}
              </p>
            </div>
          </div>
        )}

        {mutation.isError && (
          <Alert variant="destructive">
            <AlertTitle>{tl({ zh: "快照生成失败", en: "Snapshot generation failed" })}</AlertTitle>
            <AlertDescription>{errorMessage(mutation.error, tl({ zh: "请检查数据发布和历史数据覆盖", en: "Check the dataset release and historical data coverage" }))}</AlertDescription>
          </Alert>
        )}
        {jobQuery.isError && (
          <Alert variant="destructive">
            <AlertTitle>{tl({ zh: "进度查询失败", en: "Failed to poll job status" })}</AlertTitle>
            <AlertDescription>{errorMessage(jobQuery.error, tl({ zh: "暂时无法读取任务状态", en: "Temporarily unable to read job status" }))}</AlertDescription>
          </Alert>
        )}
        {jobQuery.data && <FeatureSnapshotProgress job={jobQuery.data} />}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
            {tl({ zh: "取消", en: "Cancel" })}
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={!valid || busy}>
            <Sparkles className="mr-2 h-4 w-4" />
            {busy
              ? tl({ zh: "计算并发布中…", en: "Computing and publishing…" })
              : tl({ zh: "计算并发布快照", en: "Compute and publish snapshot" })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function FeaturesTab() {
  const { tl } = useT();
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
          {data
            ? tl({ zh: `共 ${data.length} 个特征快照`, en: `${data.length} feature snapshots` })
            : tl({ zh: "加载中…", en: "Loading…" })}
        </p>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
            {tl({ zh: "刷新", en: "Refresh" })}
          </Button>
          <Button size="sm" onClick={() => setGenerateOpen(true)}>
            <Sparkles className="h-4 w-4" />
            {tl({ zh: "生成快照", en: "Generate snapshot" })}
          </Button>
        </div>
      </div>

      <Alert className="mb-4">
        <Layers className="h-4 w-4" />
        <AlertTitle>{tl({ zh: "快照是研究输入的不可变切片", en: "Snapshots are immutable slices of research inputs" })}</AlertTitle>
        <AlertDescription>
          {tl({
            zh: "它绑定一个数据发布、一个决策时点、计算窗口、每个标的的特征值和 checksum。快照生成成功后，才能在因子实验和研究运行中选择；生成过程不会启动回测或交易。",
            en: "It binds one dataset release, one decision time, calculation windows, per-symbol feature values, and a checksum. Only after a snapshot is generated can it be selected in factor experiments and research runs; generation never starts backtests or trading.",
          })}
        </AlertDescription>
      </Alert>

      {generatedSnapshot && (
        <Alert variant="success" className="mb-4" aria-live="polite">
          <Sparkles className="h-4 w-4" />
          <AlertTitle>{tl({ zh: "特征快照已生成并发布", en: "Feature snapshot generated and published" })}</AlertTitle>
          <AlertDescription>
            <span className="font-mono">{generatedSnapshot.snapshot_id}</span> ·{" "}
            {tl({ zh: `${featureSnapshotSymbolCount(generatedSnapshot)} 个标的 ·`, en: `${featureSnapshotSymbolCount(generatedSnapshot)} symbols ·` })}{" "}
            {tl({
              zh: `${featureSnapshotNames(generatedSnapshot).length} 个因子。现在可以创建因子实验或排队研究运行。`,
              en: `${featureSnapshotNames(generatedSnapshot).length} factors. You can now create a factor experiment or queue a research run.`,
            })}
          </AlertDescription>
        </Alert>
      )}

      {isLoading ? (
        <LoadingState rows={6} />
      ) : isError ? (
        <EmptyState
          icon={<Layers className="h-8 w-8" />}
          title={tl({ zh: "加载失败", en: "Failed to load" })}
          description={
            error instanceof Error ? error.message : tl({ zh: "无法加载特征快照", en: "Unable to load feature snapshots" })
          }
        />
      ) : data && data.length > 0 ? (
        <div className="overflow-hidden rounded-lg border border-border">
          <ScrollArea className="max-h-[600px]">
            <Table>
              <TableHeader className="sticky top-0 bg-card">
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>{tl({ zh: "快照 ID", en: "Snapshot ID" })}</TableHead>
                  <TableHead>{tl({ zh: "数据发布 ID", en: "Release ID" })}</TableHead>
                  <TableHead className="text-right">{tl({ zh: "因子数", en: "Factors" })}</TableHead>
                  <TableHead className="text-right">{tl({ zh: "标的数", en: "Symbols" })}</TableHead>
                  <TableHead className="text-right">{tl({ zh: "行数", en: "Rows" })}</TableHead>
                  <TableHead>{tl({ zh: "研究状态", en: "Research status" })}</TableHead>
                  <TableHead>{tl({ zh: "创建时间", en: "Created" })}</TableHead>
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
                                  {tl({ zh: "标的数：", en: "Symbols: " })}
                                  <span className="font-mono text-foreground">
                                    {formatNumber(featureSnapshotSymbolCount(snap), 0)}
                                  </span>
                                </span>
                                <span>
                                  {tl({ zh: "行数：", en: "Rows: " })}
                                  <span className="font-mono text-foreground">
                                    {formatNumber(featureSnapshotObservationCount(snap), 0)}
                                  </span>
                                </span>
                                <span>
                                  {tl({ zh: "状态：", en: "Status: " })}
                                  <StatusBadge status={featureSnapshotStatus(snap)} />
                                </span>
                                <span>{tl({ zh: "决策时点：", en: "Decision time: " })}<span className="font-mono text-foreground">{formatDateTime(snap.decision_at)}</span></span>
                                <span>
                                  {tl({ zh: "校验和：", en: "Checksum: " })}
                                  <span className="font-mono text-foreground">
                                    {snap.checksum}
                                  </span>
                                </span>
                              </div>
                              <Separator />
                              <div>
                                <p className="mb-2 text-xs font-medium text-muted-foreground">
                                  {tl({
                                    zh: `因子列表（${featureSnapshotNames(snap).length}）`,
                                    en: `Factors (${featureSnapshotNames(snap).length})`,
                                  })}
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
                              {(snap.issues ?? []).length > 0 && (
                                <Alert variant="warning">
                                  <AlertTitle>{tl({ zh: "质量提示", en: "Quality notes" })}</AlertTitle>
                                  <AlertDescription>{(snap.issues ?? []).join("；")}</AlertDescription>
                                </Alert>
                              )}
                              <p className="text-xs text-muted-foreground">
                                {tl({ zh: "计算窗口：", en: "Calculation windows: " })}{Object.entries(snap.calculation_windows ?? {})
                                  .map(([name, window]) => tl({ zh: `${name}=${window}日`, en: `${name}=${window}d` }))
                                  .join(" · ") || tl({ zh: "无", en: "none" })}
                                {tl({ zh: "· 代码版本 ", en: "· code version " })}<span className="font-mono">{snap.code_version}</span>
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
          title={tl({ zh: "暂无特征快照", en: "No feature snapshots yet" })}
          description={tl({
            zh: "当前数据库中没有已发布快照。选择一个已发布数据版本和决策日，显式生成后才能用于因子实验和研究运行。",
            en: "No published snapshots in the database yet. Pick a published data version and a decision date; generate one explicitly before it can be used in factor experiments and research runs.",
          })}
          action={
            <Button size="sm" onClick={() => setGenerateOpen(true)}>
              <Sparkles className="h-4 w-4" />
              {tl({ zh: "生成第一份快照", en: "Generate first snapshot" })}
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
  const { tl } = useT();
  const [expandedId, setExpandedId] = React.useState<string | null>(null);
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["factor-signals", { limit: 50 }],
    queryFn: () => factorLabApi.signals({ limit: 50 }),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data
            ? tl({ zh: `共 ${data.length} 个因子信号`, en: `${data.length} factor signals` })
            : tl({ zh: "加载中…", en: "Loading…" })}
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          {tl({ zh: "刷新", en: "Refresh" })}
        </Button>
      </div>

      {isLoading ? (
        <LoadingState rows={6} />
      ) : isError ? (
        <EmptyState
          icon={<Signal className="h-8 w-8" />}
          title={tl({ zh: "加载失败", en: "Failed to load" })}
          description={
            error instanceof Error ? error.message : tl({ zh: "无法加载因子信号", en: "Unable to load factor signals" })
          }
        />
      ) : data && data.length > 0 ? (
        <div className="overflow-hidden rounded-lg border border-border">
          <ScrollArea className="max-h-[600px]">
            <Table>
              <TableHeader className="sticky top-0 bg-card">
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>{tl({ zh: "信号 ID", en: "Signal ID" })}</TableHead>
                  <TableHead>{tl({ zh: "因子名", en: "Factor" })}</TableHead>
                  <TableHead>{tl({ zh: "研究状态", en: "Research status" })}</TableHead>
                  <TableHead>{tl({ zh: "快照 ID", en: "Snapshot ID" })}</TableHead>
                  <TableHead>{tl({ zh: "创建时间", en: "Created" })}</TableHead>
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
                                {tl({
                                  zh: `信号 payload（${payloadKeys.length} 个字段，含 IC 值、分层收益等）`,
                                  en: `Signal payload (${payloadKeys.length} fields, incl. IC values, layered returns, etc.)`,
                                })}
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
          title={tl({ zh: "暂无因子信号", en: "No factor signals yet" })}
          description={tl({
            zh: "因子信号由因子计算产出，用于驱动策略目标仓位与组合决策。",
            en: "Factor signals are produced by factor computations and drive strategy target positions and portfolio decisions.",
          })}
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
  const { tl } = useT();
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
          <DialogTitle>{tl({ zh: "创建因子实验", en: "Create factor experiment" })}</DialogTitle>
          <DialogDescription>
            {tl({
              zh: "冻结因子假设、数据版本与评估计划，用于系统性检验因子的预测能力。",
              en: "Freeze the factor hypothesis, data versions, and evaluation plan to systematically test the factor's predictive power.",
            })}
          </DialogDescription>
        </DialogHeader>

        {releases && releases.length === 0 && (
          <Alert variant="warning">
            <AlertTitle>{tl({ zh: "缺少数据发布", en: "Missing dataset release" })}</AlertTitle>
            <AlertDescription>
              {tl({ zh: "先到 ", en: "Go to " })}
              <Link className="underline" to="/research/data">{tl({ zh: "数据与标的", en: "Data & Instruments" })}</Link>
              {tl({ zh: " 创建不可变数据发布，再回来绑定因子实验。", en: " to create an immutable dataset release, then come back to bind the factor experiment." })}
            </AlertDescription>
          </Alert>
        )}
        {features && features.length === 0 && (
          <Alert variant="warning">
            <AlertTitle>{tl({ zh: "缺少特征快照", en: "Missing feature snapshot" })}</AlertTitle>
            <AlertDescription>
              {tl({
                zh: "因子实验必须绑定已发布的 FeatureSnapshot。当前没有可用快照，请先在「特征快照」页完成生成/发布；仅有因子目录不能直接创建实验。",
                en: "Factor experiments must bind a published FeatureSnapshot. No snapshot is available yet — generate and publish one on the Feature Snapshots tab first; a factor catalog alone cannot create an experiment.",
              })}
            </AlertDescription>
          </Alert>
        )}

        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="exp-hypothesis">
              {tl({ zh: "因子假设", en: "Factor hypothesis" })}
              <span className="ml-1 text-xs text-muted-foreground">
                {tl({ zh: "（至少 10 字符）", en: "(at least 10 characters)" })}
              </span>
            </Label>
            <Textarea
              id="exp-hypothesis"
              value={form.hypothesis}
              onChange={(e) =>
                setForm((p) => ({ ...p, hypothesis: e.target.value }))
              }
              placeholder={tl({ zh: "例如：过去 20 日动量排名对未来 5 日收益有正向预测力", en: "e.g. 20-day momentum rank positively predicts 5-day forward returns" })}
              className="min-h-[72px]"
            />
          </div>

          <div className="space-y-2">
            <Label>
              {tl({ zh: "因子列表", en: "Factors" })}
              <span className="ml-1 text-xs text-muted-foreground">
                {tl({
                  zh: `（从目录选择，已选 ${form.factor_names.length}）`,
                  en: `(pick from catalog, ${form.factor_names.length} selected)`,
                })}
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
                  {tl({ zh: "因子目录为空或加载中…", en: "Factor catalog is empty or loading…" })}
                </p>
              )}
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-2">
              <Label>{tl({ zh: "数据发布", en: "Dataset release" })}</Label>
              <Select
                value={form.dataset_release_id}
                onValueChange={(v) =>
                  setForm((p) => ({ ...p, dataset_release_id: v }))
                }
              >
                <SelectTrigger>
                  <SelectValue placeholder={tl({ zh: "选择数据发布", en: "Select a dataset release" })} />
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
              <Label>{tl({ zh: "特征快照", en: "Feature snapshot" })}</Label>
              <Select
                value={form.feature_snapshot_id}
                onValueChange={(v) =>
                  setForm((p) => ({ ...p, feature_snapshot_id: v }))
                }
              >
                <SelectTrigger>
                  <SelectValue placeholder={tl({ zh: "选择特征快照", en: "Select a feature snapshot" })} />
                </SelectTrigger>
                <SelectContent>
                  {(features ?? []).map((s: FeatureSnapshot) => (
                    <SelectItem
                      key={s.snapshot_id}
                      value={s.snapshot_id}
                    >
                      <span className="font-mono">
                        {s.snapshot_id.slice(0, 12)}… ·{" "}
                        {featureSnapshotNames(s).length}{" "}
                        {tl({ zh: "因子", en: "factors" })}
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
              {tl({ zh: "评估计划", en: "Evaluation plan" })}
            </Label>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label htmlFor="is-start" className="text-xs">
                  {tl({ zh: "样本内起始", en: "In-sample start" })}
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
                  {tl({ zh: "样本内结束", en: "In-sample end" })}
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
                  {tl({ zh: "OOS 起始", en: "OOS start" })}
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
                  {tl({ zh: "OOS 结束", en: "OOS end" })}
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
                {tl({ zh: "试验预算", en: "Trial budget" })}
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
                {tl({ zh: "分层数", en: "Quantiles" })}
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
                {tl({ zh: "基准代码", en: "Benchmark symbol" })}
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
                {tl({ zh: "交易成本 (bps)", en: "Transaction cost (bps)" })}
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
              {tl({ zh: "对比组", en: "Comparison group" })}
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
            {errorMessage(mutation.error, tl({ zh: "创建因子实验失败", en: "Failed to create factor experiment" }))}
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
            onClick={handleSubmit}
          >
            {mutation.isPending
              ? tl({ zh: "创建中…", en: "Creating…" })
              : tl({ zh: "创建实验", en: "Create experiment" })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ExperimentsTab() {
  const { tl } = useT();
  const [createOpen, setCreateOpen] = React.useState(false);
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["factor-experiments", { limit: 50 }],
    queryFn: () => factorLabApi.listFactorExperiments({ limit: 50 }),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data
            ? tl({ zh: `共 ${data.length} 个因子实验`, en: `${data.length} factor experiments` })
            : tl({ zh: "加载中…", en: "Loading…" })}
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
            {tl({ zh: "刷新", en: "Refresh" })}
          </Button>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" />
            {tl({ zh: "创建实验", en: "Create experiment" })}
          </Button>
        </div>
      </div>

      {isLoading ? (
        <LoadingState rows={6} />
      ) : isError ? (
        <EmptyState
          icon={<TestTube className="h-8 w-8" />}
          title={tl({ zh: "加载失败", en: "Failed to load" })}
          description={
            error instanceof Error ? error.message : tl({ zh: "无法加载因子实验", en: "Unable to load factor experiments" })
          }
        />
      ) : data && data.length > 0 ? (
        <div className="overflow-hidden rounded-lg border border-border">
          <ScrollArea className="max-h-[600px]">
            <Table>
              <TableHeader className="sticky top-0 bg-card">
                <TableRow>
                  <TableHead>{tl({ zh: "实验 ID", en: "Experiment ID" })}</TableHead>
                  <TableHead>{tl({ zh: "假设", en: "Hypothesis" })}</TableHead>
                  <TableHead>{tl({ zh: "因子列表", en: "Factors" })}</TableHead>
                  <TableHead>{tl({ zh: "数据发布 ID", en: "Release ID" })}</TableHead>
                  <TableHead>{tl({ zh: "状态", en: "Status" })}</TableHead>
                  <TableHead>{tl({ zh: "创建时间", en: "Created" })}</TableHead>
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
          title={tl({ zh: "暂无因子实验", en: "No factor experiments yet" })}
          description={tl({
            zh: "创建因子实验以系统性验证因子假设的预测力，并与机器验证实验关联。",
            en: "Create a factor experiment to systematically validate the predictive power of a factor hypothesis and link it to machine-validated experiments.",
          })}
          action={
            <Button size="sm" onClick={() => setCreateOpen(true)}>
              <Plus className="h-4 w-4" />
              {tl({ zh: "创建实验", en: "Create experiment" })}
            </Button>
          }
        />
      )}

      <CreateExperimentDialog open={createOpen} onOpenChange={setCreateOpen} />
    </div>
  );
}

export default function FactorLab() {
  const { tl } = useT();
  return (
    <div>
      <PageHeader
        title={tl({ zh: "因子实验室", en: "Factor Lab" })}
        description={tl({ zh: "因子发现、信号预览与实验验证", en: "Factor discovery, signal preview, and experiment validation" })}
        actions={<WorkflowHelpPopover next={WORKFLOW_NEXT.factors} />}
      />

      <Tabs defaultValue="catalog">
        <TabsList className="h-auto flex-wrap gap-1">
          <TabsTrigger value="catalog">{tl({ zh: "因子目录", en: "Factor catalog" })}</TabsTrigger>
          <ResearchHint hint={RESEARCH_HINTS.factors.catalog} />
          <TabsTrigger value="features">{tl({ zh: "特征快照", en: "Feature snapshots" })}</TabsTrigger>
          <ResearchHint hint={RESEARCH_HINTS.factors.features} />
          <TabsTrigger value="signals">{tl({ zh: "因子信号", en: "Factor signals" })}</TabsTrigger>
          <ResearchHint hint={RESEARCH_HINTS.factors.signals} />
          <TabsTrigger value="experiments">{tl({ zh: "因子实验", en: "Factor experiments" })}</TabsTrigger>
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
    </div>
  );
}
