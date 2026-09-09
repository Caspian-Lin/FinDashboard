import { Fragment, useEffect, useMemo, useState, lazy, Suspense } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import {
  Archive,
  ArrowUpRight,
  Database,
  RefreshCw,
  Search,
  ChevronRight,
  Landmark,
  Layers,
  HardDriveDownload,
  LineChart,
  Plus,
  Repeat,
  X,
  type LucideIcon,
} from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import {
  datasetApi,
  type CachedDataStatus,
  type DatasetReleaseCreate,
  type DatasetReleaseKind,
  type DatasetReleaseSummary,
  type EtfExecutionProfile,
  type LifecycleEvent,
} from "@/lib/research";
import { api, isJobRunning } from "@/lib/api";
import { RESEARCH_HINTS } from "@/lib/research-hints";
import { cn, formatDateTime, formatNumber, formatPercent } from "@/lib/utils";
import {
  HintLabel,
  ResearchHint,
} from "@/components/research/ResearchHint";
import { ReleaseDetailDrawer } from "@/components/research/ReleaseDetailDrawer";
import { SelectAllResultsButton } from "@/components/selection/SelectAllResultsButton";
import { useT, type LocalizedText } from "@/i18n";

const MarketDataTab = lazy(() => import("@/pages/Data"));

const MULTI_ASSET_CAPABILITIES = [
  "stock",
  "etf:index",
  "etf:cross_border",
  "etf:commodity",
  "etf:bond",
];

/* ------------------------------------------------------------------ */
/* 发布 kind 元数据:行情类(bars)与研究数据类(daily_metrics /           */
/* financial_indicators / convertible_metrics)。dataset_name / source / */
/* adjustment / capabilities 默认值与后端 dataset_publish 执行器口径一致。*/
/* ------------------------------------------------------------------ */

const BAR_RELEASE_KINDS = ["a_share_tushare", "multi_asset_mixed"] as const;

type ResearchReleaseKind =
  | "daily_metrics"
  | "financial_indicators"
  | "convertible_metrics";

const RESEARCH_RELEASE_KINDS: readonly ResearchReleaseKind[] = [
  "daily_metrics",
  "financial_indicators",
  "convertible_metrics",
];

function isResearchReleaseKind(
  kind: DatasetReleaseKind,
): kind is ResearchReleaseKind {
  return (RESEARCH_RELEASE_KINDS as readonly string[]).includes(kind);
}

const KIND_DEFAULTS: Record<
  DatasetReleaseKind,
  {
    datasetName: string;
    source: "tushare" | "mixed";
    adjustment: "qfq" | "hqfq" | "none";
    releaseIdPrefix: string;
    requiredCapabilities: string[];
  }
> = {
  a_share_tushare: {
    datasetName: "a_share_daily_bars",
    source: "tushare",
    adjustment: "qfq",
    releaseIdPrefix: "a-share-bars",
    requiredCapabilities: ["stock"],
  },
  multi_asset_mixed: {
    datasetName: "multi_asset_daily_bars",
    source: "mixed",
    adjustment: "qfq",
    releaseIdPrefix: "multi-asset-bars",
    requiredCapabilities: MULTI_ASSET_CAPABILITIES,
  },
  daily_metrics: {
    datasetName: "a_share_daily_metrics",
    source: "tushare",
    adjustment: "none",
    releaseIdPrefix: "a-share-daily-metrics",
    requiredCapabilities: ["stock"],
  },
  financial_indicators: {
    datasetName: "a_share_financial_indicators",
    source: "tushare",
    adjustment: "none",
    releaseIdPrefix: "a-share-financial-indicators",
    requiredCapabilities: ["stock"],
  },
  convertible_metrics: {
    datasetName: "a_share_convertible_metrics",
    source: "tushare",
    adjustment: "none",
    releaseIdPrefix: "a-share-convertible-metrics",
    requiredCapabilities: ["convertible"],
  },
};

const RELEASE_KIND_LABELS: Record<DatasetReleaseKind, LocalizedText> = {
  a_share_tushare: { zh: "A股 Tushare 单源", en: "A-share Tushare single source" },
  multi_asset_mixed: { zh: "多资产混合来源", en: "Multi-asset mixed sources" },
  daily_metrics: { zh: "每日指标（研究数据）", en: "Daily metrics (research data)" },
  financial_indicators: {
    zh: "财务指标（研究数据）",
    en: "Financial indicators (research data)",
  },
  convertible_metrics: {
    zh: "可转债指标（转股价值/溢价率）",
    en: "Convertible metrics (conversion value/premium)",
  },
};

interface ResearchKindInfo {
  kind: ResearchReleaseKind;
  icon: LucideIcon;
  title: LocalizedText;
  summary: LocalizedText;
  fields: string[];
  pitNote: LocalizedText;
}

const RESEARCH_KIND_META: Record<ResearchReleaseKind, ResearchKindInfo> = {
  daily_metrics: {
    kind: "daily_metrics",
    icon: LineChart,
    title: { zh: "每日指标", en: "Daily metrics" },
    summary: {
      zh: "每日截面指标（估值、换手、市值等），由 dataset_sync 从 tushare 摄取进 research_daily_metrics 表；发布把字段白名单冻结为逐标的 parquet，是 pb / market_cap / turnover_rate 等基本面因子的数据上游。",
      en: "Daily cross-sectional metrics (valuation, turnover, market cap, etc.) ingested from tushare into the research_daily_metrics table by dataset_sync; publishing freezes the field whitelist into per-symbol parquet, feeding fundamental factors like pb / market_cap / turnover_rate.",
    },
    fields: [
      "pe_ttm",
      "pb",
      "turnover_rate",
      "total_market_cap",
      "dividend_yield",
      "limit_status",
    ],
    pitNote: {
      zh: "逐日带 available_at 时点化，研究运行只读 available_at ≤ 决策时点的行，不会用到未来数据。",
      en: "Each day carries available_at; research runs only read rows with available_at at or before the decision time, never future data.",
    },
  },
  financial_indicators: {
    kind: "financial_indicators",
    icon: Landmark,
    title: { zh: "财务指标", en: "Financial indicators" },
    summary: {
      zh: "财报截面指标（盈利能力、杠杆、成长），按报告期 + 公告日由 dataset_sync 从 tushare 摄取进 research_financial_indicators 表；发布冻结为逐标的 parquet，供 ROE / 毛利率 / 营收同比等因子取数。",
      en: "Cross-sectional report metrics (profitability, leverage, growth) ingested from tushare by report period plus announcement date into the research_financial_indicators table; publishing freezes per-symbol parquet for factors like ROE / gross margin / revenue YoY.",
    },
    fields: [
      "eps",
      "roe",
      "gross_profit_margin",
      "net_profit_margin",
      "debt_to_assets",
      "revenue_yoy",
    ],
    pitNote: {
      zh: "按公告日（announcement_date）时点化：报告期数据在公告日之后才对决策可见，避免财报前视。",
      en: "Point-in-time by announcement_date: a report period only becomes visible to decisions after its announcement, avoiding look-ahead into unreleased reports.",
    },
  },
  convertible_metrics: {
    kind: "convertible_metrics",
    icon: Repeat,
    title: { zh: "可转债", en: "Convertible bonds" },
    summary: {
      zh: "可转债派生指标：转股价值 = 100 / 转股价 × 正股收盘，转股溢价率 = 转债收盘 / 转股价值 − 1。发布时从本地缓存 bars × 冻结转股价元数据（cb_basic 快照）计算；条款元数据由 dataset_sync 的 convertible_profiles 回填，缺失时先跑同步。",
      en: "Derived convertible metrics: conversion value = 100 / conversion price × underlying close; conversion premium = bond close / conversion value − 1. Computed at publish time from local cached bars × frozen conversion-price metadata (cb_basic snapshot); terms metadata is backfilled by the convertible_profiles dataset of dataset_sync — run the sync first if missing.",
    },
    fields: [
      "close",
      "conversion_price",
      "conversion_value",
      "conversion_premium",
      "underlying_symbol",
      "underlying_close",
    ],
    pitNote: {
      zh: "带日期冻结语义：发布行 = 冻结快照转股价 × 同日正股收盘；cb_basic 不含转股价下修史，不是全历史 PIT。",
      en: "Dated frozen semantics: each published row is frozen-snapshot conversion price × same-day underlying close; cb_basic has no conversion-price adjustment history, so this is not full-history PIT.",
    },
  },
};

const ETF_EXECUTION_PROFILES: {
  value: EtfExecutionProfile;
  label: LocalizedText;
  execution: LocalizedText;
}[] = [
  {
    value: "domestic_equity_etf",
    label: { zh: "国内股票 ETF", en: "Domestic equity ETF" },
    execution: { zh: "T+1 交收", en: "T+1 settlement" },
  },
  {
    value: "cross_border_etf",
    label: { zh: "跨境 ETF", en: "Cross-border ETF" },
    execution: { zh: "境外资产，T+0、免印花税", en: "Overseas assets, T+0, no stamp duty" },
  },
  {
    value: "bond_etf",
    label: { zh: "债券 ETF", en: "Bond ETF" },
    execution: { zh: "固定收益，10 份/手、免印花税", en: "Fixed income, 10 units/lot, no stamp duty" },
  },
  {
    value: "money_market_etf",
    label: { zh: "货币 ETF", en: "Money market ETF" },
    execution: { zh: "现金管理，T+0、无佣金", en: "Cash management, T+0, no commission" },
  },
  {
    value: "commodity_etf",
    label: { zh: "商品 ETF", en: "Commodity ETF" },
    execution: { zh: "黄金/商品，T+0", en: "Gold/commodity, T+0" },
  },
];

const ETF_MARKETS = [
  { value: "domestic", label: { zh: "国内（A 股）", en: "Domestic (A-share)" } },
  { value: "hk", label: { zh: "港股通", en: "Hong Kong Stock Connect" } },
  { value: "overseas", label: { zh: "海外", en: "Overseas" } },
  { value: "global", label: { zh: "全球", en: "Global" } },
] as const;

const ETF_STRATEGIES = [
  { value: "index", label: { zh: "被动指数", en: "Passive index" } },
  { value: "active", label: { zh: "主动管理", en: "Active management" } },
] as const;

const REVIEW_STATUS_LABELS: Record<string, LocalizedText> = {
  auto_adopted: { zh: "自动采用", en: "Auto-adopted" },
  needs_review: { zh: "待复核", en: "Pending review" },
  manually_confirmed: { zh: "已确认", en: "Confirmed" },
  manually_overridden: { zh: "已覆盖", en: "Overridden" },
};

const INSTRUMENT_TYPE_LABELS: Record<string, LocalizedText> = {
  stock: { zh: "股票", en: "Stock" },
  etf: { zh: "ETF", en: "ETF" },
};

const MARKET_LABELS: Record<string, LocalizedText> = {
  a_share: { zh: "A 股", en: "A-share" },
  hk: { zh: "港股", en: "HK stocks" },
  us: { zh: "美股", en: "US stocks" },
  future: { zh: "期货", en: "Futures" },
};

const INSTRUMENT_STATUS_LABELS: Record<string, LocalizedText> = {
  active: { zh: "正常", en: "Active" },
  suspended: { zh: "停牌", en: "Suspended" },
  pending_delist: { zh: "待确认退市", en: "Pending delisting" },
  delisted: { zh: "已退市", en: "Delisted" },
};

const STATUS_ORDER = ["active", "suspended", "pending_delist", "delisted"];

function EtfMetadataEditor({
  symbol,
  onSaved,
}: {
  symbol: string;
  onSaved?: () => void;
}) {
  const { tl } = useT();
  const queryClient = useQueryClient();
  const [executionProfile, setExecutionProfile] = useState<EtfExecutionProfile | "">("");
  const [underlyingMarket, setUnderlyingMarket] = useState("");
  const [strategyType, setStrategyType] = useState("");
  const [underlyingIndex, setUnderlyingIndex] = useState("");
  const [reason, setReason] = useState("");
  const { data, isLoading, isError } = useQuery({
    queryKey: ["etf-metadata", symbol],
    queryFn: () => datasetApi.etfMetadata(symbol),
  });

  useEffect(() => {
    setExecutionProfile((data?.execution_profile as EtfExecutionProfile) ?? "");
    setUnderlyingMarket(data?.underlying_market ?? "domestic");
    setStrategyType(data?.strategy_type ?? "index");
    setUnderlyingIndex(data?.underlying_index ?? "");
  }, [data, symbol]);

  const save = useMutation({
    mutationFn: () => {
      if (!executionProfile) {
        throw new Error(tl({ zh: "请选择执行档位", en: "Select an execution profile" }));
      }
      return datasetApi.updateEtfClassification(symbol, {
        execution_profile: executionProfile,
        underlying_market: underlyingMarket as "domestic" | "hk" | "overseas" | "global",
        strategy_type: strategyType as "index" | "active",
        underlying_index: underlyingIndex.trim() || null,
        reason: reason.trim() || undefined,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["etf-metadata", symbol] });
      queryClient.invalidateQueries({ queryKey: ["research-instruments"] });
      queryClient.invalidateQueries({ queryKey: ["etf-summary"] });
      queryClient.invalidateQueries({ queryKey: ["etf-review"] });
      onSaved?.();
    },
  });

  const selectedProfile = ETF_EXECUTION_PROFILES.find((item) => item.value === executionProfile);
  const reviewLabel = data
    ? data.review_status in REVIEW_STATUS_LABELS
      ? tl(REVIEW_STATUS_LABELS[data.review_status])
      : data.review_status
    : null;

  return (
    <div className="space-y-3 px-4 py-4">
      <div>
        <h3 className="text-sm font-semibold">{tl({ zh: "ETF 研究分类", en: "ETF research classification" })}</h3>
        <p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
          {tl({
            zh: "多维分类决定回测中的资产大类、交收周期和手数规则。执行档位是权威维度，标的市场与策略类型用于组合分析与风控。修改后设为「已覆盖」，自动同步不再覆盖。",
            en: "Multi-dimensional classification determines the asset class, settlement cycle and lot rules used in backtests. The execution profile is the authoritative dimension; underlying market and strategy type serve portfolio analysis and risk control. After an edit the record is marked as overridden, so auto-sync no longer overwrites it.",
          })}
        </p>
      </div>
      {isLoading ? (
        <LoadingState rows={2} />
      ) : isError ? (
        <p className="text-sm text-destructive">{tl({ zh: "ETF 元数据加载失败，请重试。", en: "Failed to load ETF metadata. Please retry." })}</p>
      ) : (
        <>
          {data && reviewLabel && (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span
                className={cn(
                  "rounded px-2 py-0.5 font-medium",
                  data.review_status === "needs_review"
                    ? "bg-warning/15 text-warning"
                    : data.review_status === "manually_overridden"
                      ? "bg-primary/15 text-primary"
                      : "bg-success/15 text-success",
                )}
              >
                {reviewLabel}
              </span>
              {data.manual_override && (
                <span className="text-muted-foreground">{tl({ zh: "人工覆盖（自动同步跳过）", en: "Manually overridden (auto-sync skipped)" })}</span>
              )}
              {data.confidence && Number(data.confidence) > 0 && (
                <span className="text-muted-foreground">
                  {tl({
                    zh: `置信度 ${formatPercent(Number(data.confidence))}`,
                    en: `Confidence ${formatPercent(Number(data.confidence))}`,
                  })}
                </span>
              )}
              {data.source && (
                <span className="text-muted-foreground">
                  {tl({
                    zh: `来源 ${data.source}`,
                    en: `Source ${data.source}`,
                  })}
                </span>
              )}
            </div>
          )}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-4 md:items-end">
            <div className="space-y-2">
              <Label htmlFor={`etf-profile-${symbol}`}>{tl({ zh: "执行档位", en: "Execution profile" })}</Label>
              <Select
                value={executionProfile}
                onValueChange={(value) => setExecutionProfile(value as EtfExecutionProfile)}
              >
                <SelectTrigger id={`etf-profile-${symbol}`}>
                  <SelectValue placeholder={tl({ zh: "请选择", en: "Select" })} />
                </SelectTrigger>
                <SelectContent>
                  {ETF_EXECUTION_PROFILES.map((item) => (
                    <SelectItem key={item.value} value={item.value}>
                      {tl(item.label)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor={`etf-market-${symbol}`}>{tl({ zh: "标的市场", en: "Underlying market" })}</Label>
              <Select value={underlyingMarket} onValueChange={setUnderlyingMarket}>
                <SelectTrigger id={`etf-market-${symbol}`}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ETF_MARKETS.map((item) => (
                    <SelectItem key={item.value} value={item.value}>
                      {tl(item.label)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor={`etf-strategy-${symbol}`}>{tl({ zh: "策略类型", en: "Strategy type" })}</Label>
              <Select value={strategyType} onValueChange={setStrategyType}>
                <SelectTrigger id={`etf-strategy-${symbol}`}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ETF_STRATEGIES.map((item) => (
                    <SelectItem key={item.value} value={item.value}>
                      {tl(item.label)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor={`etf-index-${symbol}`}>{tl({ zh: "跟踪指数（可选）", en: "Tracking index (optional)" })}</Label>
              <Input
                id={`etf-index-${symbol}`}
                value={underlyingIndex}
                onChange={(event) => setUnderlyingIndex(event.target.value)}
                placeholder={tl({ zh: "例如 000300.SH", en: "e.g. 000300.SH" })}
                spellCheck={false}
              />
            </div>
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_auto] md:items-end">
            <div className="space-y-2">
              <Label htmlFor={`etf-reason-${symbol}`}>{tl({ zh: "修改理由（可选，记录到审计）", en: "Change reason (optional, recorded for audit)" })}</Label>
              <Input
                id={`etf-reason-${symbol}`}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder={tl({
                  zh: "例如：根据基金合同第 X 条修正为跨境 ETF",
                  en: "e.g. corrected to cross-border ETF per clause X of the fund contract",
                })}
              />
            </div>
            <Button
              type="button"
              onClick={() => save.mutate()}
              disabled={!executionProfile || save.isPending}
            >
              {save.isPending
                ? tl({ zh: "保存中…", en: "Saving…" })
                : data?.execution_profile
                  ? tl({ zh: "保存修改", en: "Save changes" })
                  : tl({ zh: "补齐元数据", en: "Complete metadata" })}
            </Button>
          </div>
          {selectedProfile && (
            <p className="text-xs text-muted-foreground">
              {tl({
                zh: `将按「${tl(selectedProfile.label)}」处理：${tl(selectedProfile.execution)}。`,
                en: `Will be treated as "${tl(selectedProfile.label)}": ${tl(selectedProfile.execution)}.`,
              })}
            </p>
          )}
          {data?.evidence && data.evidence.length > 0 && (
            <div className="space-y-1">
              <p className="text-xs font-medium text-muted-foreground">{tl({ zh: "分类证据", en: "Classification evidence" })}</p>
              <ul className="list-inside list-disc space-y-0.5 text-xs text-muted-foreground">
                {data.evidence.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            </div>
          )}
          {save.isSuccess && (
            <Alert variant="success" aria-live="polite">
              <AlertTitle>{tl({ zh: "ETF 元数据已保存", en: "ETF metadata saved" })}</AlertTitle>
              <AlertDescription>
                {tl({
                  zh: `${symbol} 分类已更新并标记为人工覆盖，现在可以重新校验数据发布。`,
                  en: `${symbol} classification updated and marked as manually overridden; you can now re-validate the data release.`,
                })}
              </AlertDescription>
            </Alert>
          )}
          {save.isError && (
            <Alert variant="destructive" aria-live="assertive">
              <AlertTitle>{tl({ zh: "ETF 元数据保存失败", en: "Failed to save ETF metadata" })}</AlertTitle>
              <AlertDescription>
                {save.error instanceof Error
                  ? save.error.message
                  : tl({ zh: "请稍后重试", en: "Please try again later" })}
              </AlertDescription>
            </Alert>
          )}
        </>
      )}
    </div>
  );
}

function EtfSyncPanel({ onDone }: { onDone?: () => void }) {
  const { tl } = useT();
  const queryClient = useQueryClient();
  const [dryRunResult, setDryRunResult] = useState<Record<string, number> | null>(null);
  const sync = useMutation({
    mutationFn: (dryRun: boolean) => datasetApi.etfSync({ dry_run: dryRun }),
    onSuccess: (result, dryRun) => {
      setDryRunResult({
        total: result.total,
        insert: result.to_insert,
        update: result.to_update,
        skip: result.skipped_override,
        review: result.needs_review,
        adopt: result.auto_adopted,
      });
      if (!dryRun) {
        queryClient.invalidateQueries({ queryKey: ["etf-summary"] });
        queryClient.invalidateQueries({ queryKey: ["etf-review"] });
        onDone?.();
      }
    },
  });
  const summary = useQuery({
    queryKey: ["etf-summary"],
    queryFn: () => datasetApi.etfSummary(),
  });

  return (
    <div className="space-y-3 px-4 py-4">
      <div>
        <h3 className="text-sm font-semibold">{tl({ zh: "ETF 元数据批量同步", en: "Bulk ETF metadata sync" })}</h3>
        <p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
          {tl({
            zh: "从 akshare 拉取全市场 ETF 基金类型并自动分类。高置信度结果自动采用，低置信度进入待复核队列。人工覆盖的记录不会被覆盖。",
            en: "Pulls fund types for the whole ETF market from akshare and classifies them automatically. High-confidence results are auto-adopted; low-confidence ones enter the review queue. Manually overridden records are never overwritten.",
          })}
        </p>
      </div>
      {summary.data && (
        <div className="grid grid-cols-2 gap-2 text-xs md:grid-cols-4 lg:grid-cols-7">
          {[
            { label: { zh: "总计", en: "Total" }, value: summary.data.total },
            { label: { zh: "自动采用", en: "Auto-adopted" }, value: summary.data.auto_adopted },
            { label: { zh: "待复核", en: "Pending review" }, value: summary.data.needs_review },
            { label: { zh: "已确认", en: "Confirmed" }, value: summary.data.manually_confirmed },
            { label: { zh: "人工覆盖", en: "Overridden" }, value: summary.data.manually_overridden },
            { label: { zh: "缺失", en: "Missing" }, value: summary.data.missing_metadata },
          ].map((item) => (
            <div key={item.label.zh} className="rounded border p-2 text-center">
              <div className="text-lg font-semibold">{item.value}</div>
              <div className="text-muted-foreground">{tl(item.label)}</div>
            </div>
          ))}
        </div>
      )}
      <div className="flex gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() => sync.mutate(true)}
          disabled={sync.isPending}
        >
          {sync.isPending
            ? tl({ zh: "同步中…", en: "Syncing…" })
            : tl({ zh: "预览（dry-run）", en: "Preview (dry run)" })}
        </Button>
        <Button
          size="sm"
          onClick={() => sync.mutate(false)}
          disabled={sync.isPending}
        >
          {tl({ zh: "执行同步", en: "Run sync" })}
        </Button>
      </div>
      {dryRunResult && (
        <div className="rounded border p-3 text-xs">
          <p className="font-medium">{tl({ zh: "同步预览结果", en: "Sync preview result" })}</p>
          <ul className="mt-1 space-y-0.5 text-muted-foreground">
            <li>
              {tl({
                zh: `共 ${dryRunResult.total} 只 ETF`,
                en: `${dryRunResult.total} ETFs in total`,
              })}
            </li>
            <li>
              {tl({
                zh: `新增 ${dryRunResult.insert}，更新 ${dryRunResult.update}，跳过（人工覆盖）${dryRunResult.skip}`,
                en: `${dryRunResult.insert} to insert, ${dryRunResult.update} to update, ${dryRunResult.skip} skipped (manually overridden)`,
              })}
            </li>
            <li>
              {tl({
                zh: `自动采用 ${dryRunResult.adopt}，待复核 ${dryRunResult.review}`,
                en: `${dryRunResult.adopt} auto-adopted, ${dryRunResult.review} pending review`,
              })}
            </li>
          </ul>
        </div>
      )}
      {sync.isError && (
        <Alert variant="destructive">
          <AlertTitle>{tl({ zh: "同步失败", en: "Sync failed" })}</AlertTitle>
          <AlertDescription>
            {sync.error instanceof Error
              ? sync.error.message
              : tl({ zh: "请稍后重试", en: "Please try again later" })}
          </AlertDescription>
        </Alert>
      )}
    </div>
  );
}

function EtfReviewQueue({ onFixed }: { onFixed?: () => void }) {
  const { tl } = useT();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const { data, isLoading } = useQuery({
    queryKey: ["etf-review"],
    queryFn: () => datasetApi.etfReview({ limit: 200 }),
  });
  const confirm = useMutation({
    mutationFn: (codes: string[]) => datasetApi.etfBatchConfirm({ codes }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["etf-review"] });
      queryClient.invalidateQueries({ queryKey: ["etf-summary"] });
      setSelected(new Set());
      onFixed?.();
    },
  });

  const toggle = (code: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
  };

  if (isLoading) return <LoadingState rows={3} />;
  if (!data || data.length === 0) {
    return (
      <p className="px-4 py-4 text-sm text-muted-foreground">
        {tl({ zh: "没有待复核的 ETF 分类。", en: "No ETF classifications pending review." })}
      </p>
    );
  }

  return (
    <div className="space-y-2 px-4 py-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">
          {tl({
            zh: `ETF 待复核队列（${data.length}）`,
            en: `ETF review queue (${data.length})`,
          })}
        </h3>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              setSelected((prev) =>
                prev.size === data.length
                  ? new Set()
                  : new Set(data.map((item) => item.code)),
              )
            }
          >
            {selected.size === data.length
              ? tl({ zh: "取消全选", en: "Deselect all" })
              : tl({ zh: "全选", en: "Select all" })}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => confirm.mutate([...selected])}
            disabled={selected.size === 0 || confirm.isPending}
          >
            {tl({
              zh: `批量确认（${selected.size}）`,
              en: `Batch confirm (${selected.size})`,
            })}
          </Button>
        </div>
      </div>
      <div className="max-h-80 space-y-1 overflow-y-auto">
        {data.map((item) => (
          <label
            key={item.code}
            className="flex cursor-pointer items-center gap-2 rounded border p-2 text-xs hover:bg-muted/50"
          >
            <input
              type="checkbox"
              checked={selected.has(item.code)}
              onChange={() => toggle(item.code)}
            />
            <span className="font-mono font-medium">{item.code}</span>
            <span className="text-muted-foreground">
              {item.execution_profile ?? item.category}
              {item.underlying_market !== "domestic" && ` · ${item.underlying_market}`}
            </span>
            <span className="text-muted-foreground">
              {tl({
                zh: `置信度 ${formatPercent(Number(item.confidence))}`,
                en: `Confidence ${formatPercent(Number(item.confidence))}`,
              })}
            </span>
          </label>
        ))}
      </div>
      {confirm.isError && (
        <p className="text-sm text-destructive">
          {tl({
            zh: `批量确认失败：${confirm.error instanceof Error ? confirm.error.message : "请重试"}`,
            en: `Batch confirm failed: ${confirm.error instanceof Error ? confirm.error.message : "please retry"}`,
          })}
        </p>
      )}
    </div>
  );
}

function nextReleaseNames(
  releases: DatasetReleaseSummary[],
  releaseKind: DatasetReleaseKind,
) {
  const stamp = new Date().toISOString().slice(0, 10);
  const compact = stamp.replaceAll("-", "");
  const releasePrefix = `${KIND_DEFAULTS[releaseKind].releaseIdPrefix}-${compact}-v`;
  const existingVersions = releases.flatMap((release) => {
    if (!release.release_id.startsWith(releasePrefix)) return [];
    const parsed = Number(release.release_id.slice(releasePrefix.length));
    return Number.isInteger(parsed) && parsed > 0 ? [parsed] : [];
  });
  const nextVersion = Math.max(0, ...existingVersions) + 1;
  return {
    version: `${stamp}-v${nextVersion}`,
    releaseId: `${releasePrefix}${nextVersion}`,
  };
}

function releaseDateRange(items: CachedDataStatus[]) {
  const starts = items.flatMap((item) => (item.first_date ? [item.first_date] : []));
  const ends = items.flatMap((item) => (item.last_date ? [item.last_date] : []));
  if (starts.length !== items.length || ends.length !== items.length) {
    return null;
  }
  const start = starts.sort().at(0);
  const end = ends.sort().at(-1);
  return start && end && start <= end ? { start, end } : null;
}

function summarizePublishError(error: unknown, lang: "zh" | "en"): {
  summary: string;
  details?: string;
} {
  const message =
    error instanceof Error
      ? error.message
      : lang === "en"
        ? "Please check the cache range and quality gate configuration"
        : "请检查缓存范围与质量门配置";
  // "数据质量门未通过" 是后端错误消息中的固定标记,不翻译。
  if (message.length <= 600 || !message.includes("数据质量门未通过")) {
    return { summary: message };
  }

  const reasons = message
    .slice(message.indexOf("数据质量门未通过") + "数据质量门未通过".length)
    .replace(/^\s*:\s*/, "")
    .split(";")
    .map((item) => item.trim())
    .filter(Boolean);
  const capabilityCount = reasons.filter((item) =>
    item.startsWith("capability:"),
  ).length;
  const instrumentCount = reasons.filter((item) =>
    item.startsWith("instrument:"),
  ).length;
  const preview = reasons.slice(0, 5).join("；");

  return {
    summary:
      lang === "en"
        ? `Quality gate found ${reasons.length} blocker(s) (${capabilityCount} capability, ${instrumentCount} instrument).${preview}${reasons.length > 5 ? "..." : ""}`
        : `质量门发现 ${reasons.length} 项阻塞（能力类别 ${capabilityCount} 项，标的 ${instrumentCount} 项）。${preview}${reasons.length > 5 ? "……" : ""}`,
    details: message,
  };
}

type ReleaseSymbolsMode = "full_market" | "from_release" | "manual";

function ReleasePublisher({
  onGoToFetch,
  onGoToInstruments,
  existingReleases,
  releaseNamesReady,
  allowedKinds,
  initialKind,
}: {
  /** 空缓存时的「前往行情拉取」引导;不传(如同页签已有拉取面板)则不渲染。 */
  onGoToFetch?: () => void;
  onGoToInstruments: () => void;
  existingReleases: DatasetReleaseSummary[];
  releaseNamesReady: boolean;
  /** 表单里可切换的发布 kind(各页签预选自己的 kind)。 */
  allowedKinds: readonly DatasetReleaseKind[];
  initialKind: DatasetReleaseKind;
}) {
  const { tl, lang } = useT();
  const queryClient = useQueryClient();
  const kindDefaults = KIND_DEFAULTS[initialKind];
  const defaults = nextReleaseNames([], initialKind);
  const [isOpen, setIsOpen] = useState(false);
  const [cacheSearch, setCacheSearch] = useState("");
  const [datasetName, setDatasetName] = useState(kindDefaults.datasetName);
  const [releaseId, setReleaseId] = useState(defaults.releaseId);
  const [version, setVersion] = useState(defaults.version);
  const [releaseKind, setReleaseKind] = useState<DatasetReleaseKind>(initialKind);
  const [adjustment, setAdjustment] = useState<DatasetReleaseCreate["adjustment"]>(
    kindDefaults.adjustment,
  );
  // 研究数据类 kind 支持 全市场 / 复制发布 / 手动选缓存 三种范围(默认全市场,
  // 入队期按 kind 语义展开);bars 两类保持原手动选缓存行为。
  const [symbolsMode, setSymbolsMode] = useState<ReleaseSymbolsMode>(
    isResearchReleaseKind(initialKind) ? "full_market" : "manual",
  );
  const [symbolsFromRelease, setSymbolsFromRelease] = useState("");
  const [listingBoard, setListingBoard] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [selected, setSelected] = useState<Record<string, CachedDataStatus>>({});
  const isResearchKind = isResearchReleaseKind(releaseKind);
  // 研究数据类在全市场 / 复制发布模式下不需要缓存选择器。
  const showSymbolPicker = !isResearchKind || symbolsMode === "manual";

  const {
    data: cached,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: [
      "release-cache-candidates",
      { q: cacheSearch, period: "1d", adjustment, listingBoard },
    ],
    queryFn: () =>
      datasetApi.cachedData({
        q: cacheSearch || undefined,
        period: "1d",
        adjust: adjustment,
        listing_boards: listingBoard ? [listingBoard] : undefined,
        limit: 50,
      }),
    enabled: isOpen && showSymbolPicker,
  });

  const selectedItems = Object.values(selected);
  const candidateItems = useMemo(() => {
    const bySymbol = new Map<string, CachedDataStatus>();
    for (const item of cached?.items ?? []) {
      const current = bySymbol.get(item.symbol);
      if (!current || item.bar_count > current.bar_count) {
        bySymbol.set(item.symbol, item);
      }
    }
    return [...bySymbol.values()];
  }, [cached?.items]);
  const duplicateCacheCount = Math.max(
    0,
    (cached?.items.length ?? 0) - candidateItems.length,
  );
  const [repairedEtf, setRepairedEtf] = useState<string | null>(null);
  const source = KIND_DEFAULTS[releaseKind].source;
  const setNextReleaseNames = (
    nextKind: DatasetReleaseKind,
    releases = existingReleases,
  ) => {
    const next = nextReleaseNames(releases, nextKind);
    setVersion(next.version);
    setReleaseId(next.releaseId);
  };
  const applyKindChange = (next: DatasetReleaseKind) => {
    setReleaseKind(next);
    setNextReleaseNames(next);
    setDatasetName(KIND_DEFAULTS[next].datasetName);
    if (isResearchReleaseKind(next)) {
      // 研究数据无复权概念,固定 none;范围默认全市场(入队期按 kind 展开)。
      setAdjustment("none");
      setSymbolsMode("full_market");
    } else if (isResearchKind) {
      // 从研究类型切回 bars:恢复 bars 默认复权口径。
      setAdjustment("qfq");
    }
    clearSelection();
  };
  const toggleSymbol = (item: CachedDataStatus) => {
    // 除 multi_asset_mixed 外全部要求缓存来源为 tushare(研究数据来自
    // dataset_sync 的 tushare 摄取,转债 bars 亦为 tushare cb_daily)。
    if (releaseKind !== "multi_asset_mixed" && item.source !== "tushare") {
      return;
    }
    setSelected((previous) => {
      const next = { ...previous };
      if (next[item.symbol]) {
        delete next[item.symbol];
      } else {
        next[item.symbol] = item;
      }
      const range = releaseDateRange(Object.values(next));
      setStartDate(range?.start ?? "");
      setEndDate(range?.end ?? "");
      return next;
    });
  };

  const selectAllCandidates = useMutation({
    mutationFn: () =>
      datasetApi.cachedDataSelection({
        q: cacheSearch || undefined,
        period: "1d",
        adjust: adjustment,
        listing_boards: listingBoard ? [listingBoard] : undefined,
      }),
    onSuccess: (selection) => {
      setSelected((previous) => {
        const next = { ...previous };
        for (const item of selection.items) {
          if (releaseKind !== "multi_asset_mixed" && item.source !== "tushare") {
            continue;
          }
          next[item.symbol] = item;
        }
        const range = releaseDateRange(Object.values(next));
        setStartDate(range?.start ?? "");
        setEndDate(range?.end ?? "");
        return next;
      });
    },
  });

  const clearSelection = () => {
    setSelected({});
    setStartDate("");
    setEndDate("");
  };

  const publish = useMutation({
    mutationFn: () => {
      // 标的集三选一(#261):bars 两类与手动模式走内联 symbols;
      // 研究数据类默认 full_market=true(入队期按 kind 语义展开)或复制既有发布。
      const payload: DatasetReleaseCreate = {
        release_id: releaseId.trim(),
        dataset_name: datasetName.trim(),
        release_kind: releaseKind,
        source,
        version: version.trim(),
        start_date: startDate,
        end_date: endDate,
        adjustment,
        required_capabilities:
          KIND_DEFAULTS[releaseKind].requiredCapabilities,
      };
      if (showSymbolPicker) {
        payload.symbols = selectedItems.map((item) => item.symbol);
      } else if (symbolsMode === "from_release") {
        payload.symbols_from_release = symbolsFromRelease;
      } else {
        payload.full_market = true;
      }
      return datasetApi.createRelease(payload);
    },
    onSuccess: (job) => {
      // #144:createRelease 返回 JobOut,前端轮询 /api/jobs/{job_id};
      // succeeded 后 result_ref = release_id,再查发布详情补全 symbol_count。
      setPublishJobId(job.job_id);
    },
  });

  const [publishJobId, setPublishJobId] = useState<string | null>(null);
  const { data: publishJob } = useQuery({
    queryKey: ["job", publishJobId],
    queryFn: () => api.getJob(publishJobId as string),
    enabled: Boolean(publishJobId),
    refetchInterval: (query) => (isJobRunning(query.state.data) ? 1500 : false),
  });

  // 任务完成后,刷新发布列表并失效相关缓存,同时推进下一个版本号。
  const publishedReleaseId =
    publishJob && !isJobRunning(publishJob) && publishJob.status === "succeeded"
      ? publishJob.result_ref
      : null;
  const publishedReleaseQuery = useQuery({
    queryKey: ["dataset-releases", "published", publishedReleaseId],
    queryFn: () => datasetApi.releases({ limit: 50 }),
    enabled: publishedReleaseId !== null,
    staleTime: Infinity,
  });
  const publishedSummary = publishedReleaseId
    ? (publishedReleaseQuery.data ?? existingReleases).find(
        (item) => item.release_id === publishedReleaseId,
      )
    : undefined;

  useEffect(() => {
    if (!publishJob || isJobRunning(publishJob)) return;
    queryClient.invalidateQueries({ queryKey: ["dataset-releases"] });
    queryClient.invalidateQueries({ queryKey: ["dataset-manifests"] });
    if (publishJob.status === "succeeded" && publishedSummary) {
      setNextReleaseNames(releaseKind, [...existingReleases, publishedSummary]);
    }
    // setNextReleaseNames 是组件内闭包,仅依赖已在数组中的 existingReleases 与稳定 setter,
    // 显式省略以避免每次渲染重建导致的无效重跑。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [publishJob, publishedSummary, queryClient, releaseKind, existingReleases]);

  const selectedSources = new Set(
    selectedItems
      .map((item) => item.source)
      .filter((item): item is string => Boolean(item)),
  );
  // 研究数据类 source 固定 tushare(后端执行器强制),来源策略恒满足。
  const sourcePolicySatisfied = isResearchKind
    ? true
    : releaseKind === "a_share_tushare"
      ? selectedItems.every((item) => item.source === "tushare")
      : selectedSources.size >= 2;
  const symbolsReady = showSymbolPicker
    ? selectedItems.length > 0
    : symbolsMode === "from_release"
      ? symbolsFromRelease.trim().length > 0
      : true; // full_market:入队期展开,空展开由后端具名拒绝。
  const canPublish =
    releaseId.trim().length >= 3 &&
    datasetName.trim().length >= 3 &&
    version.trim().length > 0 &&
    symbolsReady &&
    startDate !== "" &&
    endDate !== "" &&
    startDate <= endDate &&
    sourcePolicySatisfied;
  const missingEtfSymbol =
    publish.isError && publish.error instanceof Error
      ? publish.error.message.match(
          /([A-Z0-9]+(?:\.[A-Z]+)?): ETF 缺少分类元数据/,
        )?.[1]
      : undefined;
  const publishError = summarizePublishError(publish.error, lang);

  if (!isOpen) {
    return (
      <Alert variant="info" className="mb-5">
        <AlertTitle className="flex items-center gap-1.5">
          {tl({ zh: "发布会固化哪些内容？", en: "What does a release freeze?" })}
          <ResearchHint hint={RESEARCH_HINTS.data.releases} />
        </AlertTitle>
        <AlertDescription className="flex flex-col items-start justify-between gap-3 sm:flex-row sm:items-center">
          <p>
            {tl({
              zh: "选择发布范围和日期区间，冻结成不可修改、带 checksum 和质量报告的数据版本。后续研究只引用发布版本，不再读取会变化的数据源。",
              en: "Pick a symbol scope and a date range and freeze them into an immutable data version with a checksum and quality report. Later research references the release only, never the changing data source.",
            })}
          </p>
          <Button
            type="button"
            size="sm"
            className="shrink-0"
            disabled={!releaseNamesReady}
            onClick={() => {
              setNextReleaseNames(releaseKind);
              setIsOpen(true);
            }}
          >
            <Plus />
            {tl({ zh: "创建数据发布", en: "Create data release" })}
          </Button>
        </AlertDescription>
      </Alert>
    );
  }

  return (
    <section className="mb-6 rounded-lg border border-border bg-card">
      <div className="flex items-start justify-between gap-4 border-b border-border px-4 py-4">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <Archive className="h-4 w-4 text-primary" />
            {tl({ zh: "创建不可变数据发布", en: "Create immutable data release" })}
          </h2>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            {tl({
              zh: "把选定范围冻结为不可修改、带 checksum 与质量报告的数据版本；不联网补数据，也不会启动回测或模拟盘。bars 发布冻结本地日线缓存，研究数据发布冻结 research_* 表已摄取的数据。",
              en: "Freezes the chosen scope into an immutable data version with a checksum and quality report; no data is fetched online, and no backtests or simulations are started. Bar releases freeze the local daily-bar cache; research releases freeze data already ingested into the research_* tables.",
            })}
          </p>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={tl({ zh: "收起创建数据发布", en: "Collapse create data release" })}
          onClick={() => setIsOpen(false)}
        >
          <X />
        </Button>
      </div>

      <div className="space-y-5 p-4">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          <div className="space-y-2">
            <Label htmlFor="release-kind">{tl({ zh: "发布类型", en: "Release kind" })}</Label>
            <Select
              value={releaseKind}
              onValueChange={(value) => {
                applyKindChange(value as DatasetReleaseKind);
              }}
            >
              <SelectTrigger id="release-kind">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {allowedKinds.map((kind) => (
                  <SelectItem key={kind} value={kind}>
                    {tl(RELEASE_KIND_LABELS[kind])}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="release-dataset-name">
              <HintLabel hint={RESEARCH_HINTS.data.datasetName}>
                {tl({ zh: "数据集名称", en: "Dataset name" })}
              </HintLabel>
            </Label>
            <Input
              id="release-dataset-name"
              value={datasetName}
              onChange={(event) => setDatasetName(event.target.value)}
              spellCheck={false}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="release-version">
              <HintLabel hint={RESEARCH_HINTS.data.version}>{tl({ zh: "版本", en: "Version" })}</HintLabel>
            </Label>
            <Input
              id="release-version"
              value={version}
              onChange={(event) => setVersion(event.target.value)}
              spellCheck={false}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="release-id">
              <HintLabel hint={RESEARCH_HINTS.data.releaseId}>{tl({ zh: "发布 ID", en: "Release ID" })}</HintLabel>
            </Label>
            <Input
              id="release-id"
              value={releaseId}
              onChange={(event) => setReleaseId(event.target.value)}
              spellCheck={false}
            />
          </div>
          <div className="space-y-2">
            <Label>{tl({ zh: "来源策略", en: "Source policy" })}</Label>
            <div className="flex min-h-10 items-center rounded-md border border-input bg-muted/40 px-3 text-sm">
              {releaseKind === "a_share_tushare"
                ? tl({ zh: "严格单源：tushare", en: "Strict single source: tushare" })
                : releaseKind === "multi_asset_mixed"
                  ? tl({ zh: "混合来源：逐标的记录实际来源", en: "Mixed sources: actual source recorded per symbol" })
                  : tl({
                      zh: "固定单源：tushare（研究数据来自 dataset_sync 摄取）",
                      en: "Fixed single source: tushare (research data ingested by dataset_sync)",
                    })}
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="release-listing-board">{tl({ zh: "上市板块", en: "Listing board" })}</Label>
            <Select
              value={listingBoard || "all"}
              onValueChange={(value) => {
                setListingBoard(value === "all" ? "" : value);
                clearSelection();
              }}
            >
              <SelectTrigger id="release-listing-board">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{tl({ zh: "全部板块", en: "All boards" })}</SelectItem>
                <SelectItem value="sse_main">{tl({ zh: "沪市主板", en: "SSE Main Board" })}</SelectItem>
                <SelectItem value="szse_main">{tl({ zh: "深市主板", en: "SZSE Main Board" })}</SelectItem>
                <SelectItem value="chinext">{tl({ zh: "创业板", en: "ChiNext" })}</SelectItem>
                <SelectItem value="star">{tl({ zh: "科创板", en: "STAR Market" })}</SelectItem>
                <SelectItem value="bse">{tl({ zh: "北交所", en: "BSE" })}</SelectItem>
                <SelectItem value="cdr">CDR</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="release-adjustment">
              <HintLabel hint={RESEARCH_HINTS.data.adjustment}>{tl({ zh: "复权方式", en: "Adjustment mode" })}</HintLabel>
            </Label>
            {isResearchKind ? (
              <div
                className="flex min-h-10 items-center rounded-md border border-input bg-muted/40 px-3 text-sm"
                aria-live="polite"
              >
                {tl({
                  zh: "不复权（none）—— 研究数据无复权概念",
                  en: "Unadjusted (none) — research data has no adjustment concept",
                })}
              </div>
            ) : (
              <Select
                value={adjustment}
                onValueChange={(value) => {
                  setAdjustment(value as DatasetReleaseCreate["adjustment"]);
                  setSelected({});
                  setStartDate("");
                  setEndDate("");
                }}
              >
                <SelectTrigger id="release-adjustment">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="qfq">{tl({ zh: "前复权（qfq）", en: "Forward-adjusted (qfq)" })}</SelectItem>
                  <SelectItem value="hqfq">{tl({ zh: "后复权（hqfq）", en: "Backward-adjusted (hqfq)" })}</SelectItem>
                  <SelectItem value="none">{tl({ zh: "不复权", en: "Unadjusted" })}</SelectItem>
                </SelectContent>
              </Select>
            )}
          </div>
          </div>

        {releaseKind === "multi_asset_mixed" ? (
          <Alert variant="warning">
            <AlertTitle>{tl({ zh: "多资产混合源发布会保留来源血缘", en: "Multi-asset mixed-source releases keep source lineage" })}</AlertTitle>
            <AlertDescription>
              {tl({
                zh: "必须同时包含股票、宽基/指数、跨境、商品和债券 ETF，且每个标的的元数据、日期覆盖和质量检查均通过。实际来源少于两种时不会生成发布记录。",
                en: "Must include stock, broad-index, cross-border, commodity and bond ETFs, with metadata, date coverage and quality checks passing for every symbol. No release is created when fewer than two actual sources are present.",
              })}
            </AlertDescription>
          </Alert>
        ) : releaseKind === "a_share_tushare" ? (
          <Alert variant="info">
            <AlertTitle>{tl({ zh: "A股单源发布严格失败关闭", en: "A-share single-source releases fail closed" })}</AlertTitle>
            <AlertDescription>
              {tl({
                zh: "只能发布 A 股股票，所选缓存的每根 Bar 都必须来自 tushare；任何 ETF、未记录来源或备用源修补数据都会阻止发布。",
                en: "Only A-share stocks can be published, and every bar in the selected caches must come from tushare; any ETF, unrecorded source, or fallback-source patched data blocks the release.",
              })}
            </AlertDescription>
          </Alert>
        ) : releaseKind === "convertible_metrics" ? (
          <Alert variant="info">
            <AlertTitle>{tl({ zh: "可转债派生指标发布只接受 A 股转债", en: "Convertible metric releases accept A-share convertibles only" })}</AlertTitle>
            <AlertDescription>
              {tl({
                zh: "发布执行时从本地缓存 bars × 冻结转股价元数据计算转股价值与转股溢价率；标的缺条款元数据（convertible_metadata_missing）时先到任务中心跑 dataset_sync 的 convertible_profiles。全市场展开只取转债标的。",
                en: "Conversion value and premium are computed at publish time from local cached bars × frozen conversion-price metadata; when a symbol lacks terms metadata (convertible_metadata_missing), run the convertible_profiles dataset of dataset_sync in the job center first. Full-market expansion only picks convertible instruments.",
              })}
            </AlertDescription>
          </Alert>
        ) : (
          <Alert variant="info">
            <AlertTitle>{tl({ zh: "研究数据发布从 research_* 表冻结，不读行情缓存", en: "Research releases freeze from research_* tables, not bar caches" })}</AlertTitle>
            <AlertDescription>
              {tl({
                zh: "先由 dataset_sync 摄取（任务中心 kind=dataset_sync），再创建发布。只接受 A 股股票标的，source 固定 tushare，质量门阈值 0.95；建议与同区间 bars 主发布做一致性校验。",
                en: "dataset_sync ingests the data first (job center, kind=dataset_sync), then create the release. Only A-share stocks are accepted, source is fixed to tushare, and the quality-gate threshold is 0.95; consider a consistency check against the bar release covering the same window.",
              })}
            </AlertDescription>
          </Alert>
        )}

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="release-start-date">{tl({ zh: "开始日期", en: "Start date" })}</Label>
            <Input
              id="release-start-date"
              type="date"
              value={startDate}
              max={endDate || undefined}
              onChange={(event) => setStartDate(event.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="release-end-date">{tl({ zh: "结束日期", en: "End date" })}</Label>
            <Input
              id="release-end-date"
              type="date"
              value={endDate}
              min={startDate || undefined}
              onChange={(event) => setEndDate(event.target.value)}
            />
          </div>
        </div>
        {!isResearchKind && (
          <Alert variant="info">
            <AlertTitle>{tl({ zh: "发布周期不要求每只标的全程存在", en: "Symbols need not span the entire release window" })}</AlertTitle>
            <AlertDescription>
              {tl({
                zh: "自动日期范围覆盖所选缓存的最早至最晚日期。质量门只检查每只标的从上市到退市之间与发布周期重叠的部分；中途上市或退市不会被当作缺失。停牌零成交 Bar 会保留并标记，有停复牌生命周期事件时也允许停牌日无 Bar；两者都没有的缺口因无法可靠区分停牌和坏数据，仍会进入质量报告。",
                en: "The automatic date range spans the earliest to latest dates across the selected caches. The quality gate only checks the part of each symbol's listed-to-delisted lifecycle that overlaps the release window; mid-window listings or delistings are not treated as gaps. Suspended zero-volume bars are kept and flagged, and suspension days may lack bars when suspension/resumption lifecycle events exist; gaps with neither are still reported to the quality gate because suspension and bad data cannot be reliably distinguished.",
              })}
            </AlertDescription>
          </Alert>
        )}

        {isResearchKind && (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="release-symbols-mode">{tl({ zh: "发布范围", en: "Symbol scope" })}</Label>
              <Select
                value={symbolsMode}
                onValueChange={(value) => {
                  setSymbolsMode(value as ReleaseSymbolsMode);
                  clearSelection();
                }}
              >
                <SelectTrigger id="release-symbols-mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="full_market">
                    {tl({ zh: "全市场（入队期按类型展开）", en: "Full market (expanded at enqueue time)" })}
                  </SelectItem>
                  <SelectItem value="from_release">
                    {tl({ zh: "复制既有发布的标的集", en: "Copy symbols from an existing release" })}
                  </SelectItem>
                  <SelectItem value="manual">
                    {tl({ zh: "从缓存手动选择", en: "Select manually from cache" })}
                  </SelectItem>
                </SelectContent>
              </Select>
              {symbolsMode === "full_market" && (
                <p className="text-xs text-muted-foreground">
                  {releaseKind === "convertible_metrics"
                    ? tl({
                        zh: "展开规则：instruments 表全部活跃可转债标的；展开发生在入队期，展开为空会被拒绝。",
                        en: "Expansion: all active convertible instruments; expansion happens at enqueue time and an empty expansion is rejected.",
                      })
                    : tl({
                        zh: "展开规则：instruments 表全部活跃 A 股股票；展开发生在入队期，展开为空会被拒绝。",
                        en: "Expansion: all active A-share stocks in the instruments table; expansion happens at enqueue time and an empty expansion is rejected.",
                      })}
                </p>
              )}
            </div>
            {symbolsMode === "from_release" && (
              <div className="space-y-2">
                <Label htmlFor="release-symbols-from">{tl({ zh: "来源发布", en: "Source release" })}</Label>
                <Select
                  value={symbolsFromRelease === "" ? "unset" : symbolsFromRelease}
                  onValueChange={(value) =>
                    setSymbolsFromRelease(value === "unset" ? "" : value)
                  }
                >
                  <SelectTrigger id="release-symbols-from">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="unset">
                      {tl({ zh: "请选择一个既有发布", en: "Select an existing release" })}
                    </SelectItem>
                    {existingReleases.map((rel) => (
                      <SelectItem key={rel.release_id} value={rel.release_id}>
                        {rel.release_id} · {rel.dataset_name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {tl({
                    zh: "复制该发布冻结的标的集（按目标发布类型重新校验）；来源发布不可用时入队即被拒绝。",
                    en: "Copies the frozen symbol set of that release (re-validated for the target release kind); the enqueue is rejected when the source release is unusable.",
                  })}
                </p>
              </div>
            )}
          </div>
        )}

        {showSymbolPicker && (
        <div className="space-y-3">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
            <div className="flex-1 space-y-2">
              <Label htmlFor="release-symbol-search">
                <HintLabel hint={RESEARCH_HINTS.data.cachedSymbols}>
                  {tl({ zh: "从缓存选择标的", en: "Select symbols from cache" })}
                </HintLabel>
              </Label>
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  id="release-symbol-search"
                  value={cacheSearch}
                  onChange={(event) => setCacheSearch(event.target.value)}
                  placeholder={tl({ zh: "输入代码筛选，例如 510300", en: "Filter by code, e.g. 510300" })}
                  className="pl-9"
                />
              </div>
            </div>
            <div className="flex items-center gap-3 pb-1 sm:pb-2">
              <SelectAllResultsButton
                totalCount={cached?.total ?? 0}
                isPending={selectAllCandidates.isPending}
                onSelectAll={() => selectAllCandidates.mutate()}
                className="min-h-9 rounded-md border border-border px-3 hover:bg-accent hover:no-underline"
              />
              {selectedItems.length > 0 && (
                <button
                  type="button"
                  onClick={clearSelection}
                  className="text-xs text-muted-foreground hover:text-foreground hover:underline"
                >
                  {tl({ zh: "清空", en: "Clear" })}
                </button>
              )}
              <p className="text-sm text-muted-foreground">
                {tl({
                  zh: `已选 ${selectedItems.length} 只`,
                  en: `${selectedItems.length} selected`,
                })}
              </p>
            </div>
          </div>

          {selectedItems.length > 0 && (
            <div className="flex flex-wrap gap-2" aria-label={tl({ zh: "已选发布标的", en: "Selected release symbols" })}>
              {selectedItems.slice(0, 20).map((item) => (
                <button
                  type="button"
                  key={item.symbol}
                  onClick={() => toggleSymbol(item)}
                  className="inline-flex min-h-9 items-center gap-1.5 rounded-md bg-primary/10 px-2.5 py-1 font-mono text-xs text-primary hover:bg-primary/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  aria-label={tl({
                    zh: `移除 ${item.symbol}`,
                    en: `Remove ${item.symbol}`,
                  })}
                >
                  {item.symbol}
                  <X className="h-3 w-3" />
                </button>
              ))}
              {selectedItems.length > 20 && (
                <Badge variant="secondary" className="min-h-9 px-2.5">
                  {tl({
                    zh: `另有 ${selectedItems.length - 20} 只已选`,
                    en: `${selectedItems.length - 20} more selected`,
                  })}
                </Badge>
              )}
            </div>
          )}

          <div className="max-h-64 overflow-y-auto rounded-lg border border-border">
            {isLoading ? (
              <LoadingState rows={4} className="p-4" />
            ) : isError ? (
              <ErrorState
                message={
                  error instanceof Error
                    ? error.message
                    : tl({ zh: "无法读取本地缓存", en: "Failed to read local caches" })
                }
                className="m-3"
              />
            ) : candidateItems.length > 0 ? (
              <div className="divide-y divide-border">
                {candidateItems.map((item) => {
                  const checkboxId = `release-symbol-${item.symbol.replaceAll(".", "-")}`;
                  const sourceCompatible =
                    releaseKind === "multi_asset_mixed" || item.source === "tushare";
                  return (
                    <label
                      key={`${item.symbol}-${item.period}-${item.adjust}`}
                      htmlFor={checkboxId}
                      className={cn(
                        "flex min-h-11 items-center gap-3 px-3 py-2",
                        sourceCompatible
                          ? "cursor-pointer hover:bg-accent"
                          : "cursor-not-allowed opacity-50",
                      )}
                    >
                      <Checkbox
                        id={checkboxId}
                        checked={Boolean(selected[item.symbol])}
                        disabled={!sourceCompatible}
                        onCheckedChange={() => toggleSymbol(item)}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="font-mono text-sm font-medium">
                          {item.symbol}
                        </span>
                        <span className="ml-3 text-xs text-muted-foreground">
                          {tl({
                            zh: `${formatNumber(item.bar_count, 0)} 根 · ${item.first_date ?? "—"} 至 ${item.last_date ?? "—"}${item.source ? ` · ${item.source}` : " · 来源未记录"}`,
                            en: `${formatNumber(item.bar_count, 0)} bars · ${item.first_date ?? "—"} to ${item.last_date ?? "—"}${item.source ? ` · ${item.source}` : " · source unrecorded"}`,
                          })}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </div>
            ) : (
              <EmptyState
                icon={<Database className="h-7 w-7" />}
                title={
                  cacheSearch
                    ? tl({ zh: "没有匹配的缓存", en: "No matching caches" })
                    : tl({ zh: "尚无可发布的本地缓存", en: "No publishable local caches yet" })
                }
                description={
                  cacheSearch
                    ? tl({ zh: "确认代码和复权方式，或换一个关键词。", en: "Check the code and adjustment mode, or try another keyword." })
                    : tl({ zh: "先拉取至少一个标的的日线数据，再返回这里创建发布。", en: "Fetch daily bars for at least one symbol first, then come back here to create a release." })
                }
                action={
                  !cacheSearch && onGoToFetch ? (
                    <Button type="button" variant="outline" onClick={onGoToFetch}>
                      {tl({ zh: "前往行情拉取", en: "Go to bar data fetch" })}
                    </Button>
                  ) : undefined
                }
                className="m-3"
              />
            )}
          </div>
          {duplicateCacheCount > 0 && (
            <Alert variant="warning">
              <AlertTitle>{tl({ zh: "发现重复缓存记录，已按标的去重", en: "Duplicate cache records found, deduplicated per symbol" })}</AlertTitle>
              <AlertDescription>
                {tl({
                  zh: `当前接口返回 ${duplicateCacheCount} 条重复记录；发布接口只接收标的代码，页面已为每个标的保留覆盖更多日期的一条，避免同一标的被重复发布。若需指定来源、周期或复权方式，请先在上方筛选后再选择。`,
                  en: `The API returned ${duplicateCacheCount} duplicate records; the publish API only accepts symbol codes, so this page keeps the record covering more dates per symbol to avoid publishing the same symbol twice. To pin a source, period or adjustment mode, filter above before selecting.`,
                })}
              </AlertDescription>
            </Alert>
          )}
          {cached && cached.total > cached.items.length && (
            <p className="text-xs text-muted-foreground">
              {tl({
                zh: `当前显示 ${cached.items.length} / ${cached.total} 条匹配缓存；“全选筛选结果”会选择全部 ${cached.total} 条，而不只是当前显示项。`,
                en: `Showing ${cached.items.length} / ${cached.total} matching caches; "Select all filtered results" selects all ${cached.total} of them, not just the ones currently shown.`,
              })}
            </p>
          )}
          {selectedItems.length > 500 && (
            <Alert variant="warning">
              <AlertTitle>{tl({ zh: "这是一个大型数据发布", en: "This is a large data release" })}</AlertTitle>
              <AlertDescription>
                {tl({
                  zh: `将逐只冻结并校验 ${selectedItems.length} 只标的，处理时间和磁盘占用会明显增加。提交后请等待完成，勿重复创建新的发布 ID。`,
                  en: `Will freeze and validate ${selectedItems.length} symbols one by one; processing time and disk usage will grow noticeably. Wait for completion after submitting and do not create another release ID.`,
                })}
              </AlertDescription>
            </Alert>
          )}
          {selectedItems.length > 0 && !sourcePolicySatisfied && (
            <Alert variant="warning">
              <AlertTitle>{tl({ zh: "来源策略尚未满足", en: "Source policy not yet satisfied" })}</AlertTitle>
              <AlertDescription>
                {releaseKind === "a_share_tushare"
                  ? tl({ zh: "A股单源发布只能选择来源为 tushare 的缓存。", en: "A-share single-source releases can only select caches sourced from tushare." })
                  : tl({ zh: "多资产混合源发布至少需要两种实际数据来源。", en: "Multi-asset mixed-source releases require at least two actual data sources." })}
              </AlertDescription>
            </Alert>
          )}
        </div>
        )}

        {publish.isPending && (
          <Alert variant="info" aria-live="polite">
            <AlertTitle>{tl({ zh: "已提交发布任务", en: "Publish job submitted" })}</AlertTitle>
            <AlertDescription>
              {tl({
                zh: `发布任务已进入统一队列,完成后会显示结果。${publishJob?.phase ? `（当前阶段: ${publishJob.phase}）` : ""}`,
                en: `The publish job has entered the unified queue; the result will appear once it completes.${publishJob?.phase ? ` (Current phase: ${publishJob.phase})` : ""}`,
              })}
            </AlertDescription>
          </Alert>
        )}
        {publishJob &&
          !isJobRunning(publishJob) &&
          publishJob.status === "succeeded" &&
          publishedSummary && (
            <Alert variant="success" aria-live="polite">
              <AlertTitle>{tl({ zh: "数据发布成功", en: "Data release published" })}</AlertTitle>
              <AlertDescription>
                {tl({
                  zh: `${publishedSummary.release_id} 已冻结 ${publishedSummary.symbol_count} 只标的，可在下方列表及后续因子/策略页面中引用。`,
                  en: `${publishedSummary.release_id} froze ${publishedSummary.symbol_count} symbols; reference it in the list below and in later factor/strategy pages.`,
                })}
              </AlertDescription>
            </Alert>
          )}
        {publishJob &&
          !isJobRunning(publishJob) &&
          publishJob.status !== "succeeded" && (
            <Alert variant="destructive" aria-live="assertive">
              <AlertTitle>{tl({ zh: "数据发布失败", en: "Data release failed" })}</AlertTitle>
              <AlertDescription>
                {publishJob.error_summary ?? publishJob.status}
              </AlertDescription>
            </Alert>
          )}
        {publish.isError && (
          <Alert variant="destructive" aria-live="assertive">
              <AlertTitle>
                {missingEtfSymbol
                  ? tl({ zh: "需要补齐 ETF 元数据", en: "ETF metadata required" })
                  : tl({ zh: "数据发布失败", en: "Data release failed" })}
              </AlertTitle>
            <AlertDescription>
              <p>{publishError.summary}</p>
              {publishError.details && (
                <details className="mt-3 rounded-md border border-destructive/30 bg-background/70 p-3 text-foreground">
                  <summary className="cursor-pointer font-medium">
                    {tl({ zh: "查看完整失败明细", en: "View full failure details" })}
                  </summary>
                  <p className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words font-mono text-xs">
                    {publishError.details}
                  </p>
                </details>
              )}
              {missingEtfSymbol && (
                <div className="mt-3 rounded-md border border-destructive/30 bg-background text-foreground">
                  <EtfMetadataEditor
                    symbol={missingEtfSymbol}
                    onSaved={() => setRepairedEtf(missingEtfSymbol)}
                  />
                </div>
              )}
              {missingEtfSymbol && repairedEtf === missingEtfSymbol && (
                <Button
                  type="button"
                  className="mt-3"
                  onClick={() => publish.mutate()}
                  disabled={publish.isPending || isJobRunning(publishJob)}
                >
                  {tl({ zh: "重新校验并发布", en: "Re-validate and publish" })}
                </Button>
              )}
              {missingEtfSymbol && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="mt-3"
                  onClick={onGoToInstruments}
                >
                  {tl({ zh: "前往标的元数据批量同步 ETF 分类", en: "Go to instrument metadata to bulk-sync ETF classifications" })}
                </Button>
              )}
            </AlertDescription>
          </Alert>
        )}

        <div className="flex justify-end">
          <Button
            type="button"
            onClick={() => publish.mutate()}
            disabled={!canPublish || publish.isPending || isJobRunning(publishJob)}
          >
            <Archive />
            {publish.isPending || isJobRunning(publishJob)
              ? tl({ zh: "正在冻结并校验…", en: "Freezing and validating…" })
              : tl({ zh: "冻结并发布", en: "Freeze and publish" })}
          </Button>
        </div>
      </div>
    </section>
  );
}

/** 发布记录的 dataset_kind(#187/#265);旧记录缺省按 bars 处理。 */
function releaseDatasetKind(release: DatasetReleaseSummary): string {
  return release.dataset_kind ?? "bars";
}

function useReleasesQuery() {
  return useQuery({
    queryKey: ["dataset-releases", { limit: 50 }],
    queryFn: () => datasetApi.releases({ limit: 50 }),
  });
}

function ReleaseListTable({
  releases,
  onOpenRelease,
}: {
  releases: DatasetReleaseSummary[];
  onOpenRelease: (releaseId: string) => void;
}) {
  const { tl } = useT();
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{tl({ zh: "数据集", en: "Dataset" })}</TableHead>
            <TableHead>{tl({ zh: "版本", en: "Version" })}</TableHead>
            <TableHead>{tl({ zh: "数据范围", en: "Date range" })}</TableHead>
            <TableHead>{tl({ zh: "周期", en: "Period" })}</TableHead>
            <TableHead>{tl({ zh: "复权", en: "Adjustment" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "标的数", en: "Symbols" })}</TableHead>
            <TableHead className="text-right">{tl({ zh: "覆盖率", en: "Coverage" })}</TableHead>
            <TableHead>{tl({ zh: "质量", en: "Quality" })}</TableHead>
            <TableHead>{tl({ zh: "发布 ID", en: "Release ID" })}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {releases.map((rel) => (
            <TableRow
              key={rel.release_id}
              className="cursor-pointer hover:bg-muted/50"
              onClick={() => onOpenRelease(rel.release_id)}
            >
              <TableCell>
                <p className="font-medium">{rel.dataset_name}</p>
                <p className="font-mono text-xs text-muted-foreground">
                  {rel.source}
                </p>
              </TableCell>
              <TableCell>
                <Badge variant="secondary">{rel.version}</Badge>
              </TableCell>
              <TableCell className="whitespace-nowrap tabular-nums text-muted-foreground">
                {tl({
                  zh: `${rel.start_date} 至 ${rel.end_date}`,
                  en: `${rel.start_date} to ${rel.end_date}`,
                })}
              </TableCell>
              <TableCell className="text-muted-foreground">{rel.period}</TableCell>
              <TableCell className="text-muted-foreground">{rel.adjustment}</TableCell>
              <TableCell className="tabular-nums text-right">
                {formatNumber(rel.symbol_count, 0)}
              </TableCell>
              <TableCell className="tabular-nums text-right">
                {formatPercent(rel.coverage_pct, 1)}
              </TableCell>
              <TableCell>
                <StatusBadge status={rel.quality_status} />
              </TableCell>
              <TableCell>
                <span className="font-mono text-xs text-muted-foreground">
                  {rel.release_id}
                </span>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

/** 行情页签的 bars 类发布区:两种 bars kind 的发布表单 + bars 发布列表。 */
function BarsReleasesSection({
  onGoToInstruments,
  onOpenRelease,
}: {
  onGoToInstruments: () => void;
  onOpenRelease: (releaseId: string) => void;
}) {
  const { tl } = useT();
  const { data, isLoading, isError, error, refetch, isFetching } = useReleasesQuery();
  const barsReleases = useMemo(
    () => (data ?? []).filter((rel) => releaseDatasetKind(rel) === "bars"),
    [data],
  );

  return (
    <div>
      <ReleasePublisher
        onGoToInstruments={onGoToInstruments}
        existingReleases={barsReleases}
        releaseNamesReady={!isLoading && !isError}
        allowedKinds={BAR_RELEASE_KINDS}
        initialKind="a_share_tushare"
      />
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <p className="text-sm text-muted-foreground">
            {data
              ? tl({
                  zh: `共 ${barsReleases.length} 条行情类发布`,
                  en: `${barsReleases.length} bar releases`,
                })
              : tl({ zh: "加载中…", en: "Loading…" })}
          </p>
          <ResearchHint hint={RESEARCH_HINTS.data.releaseList} />
        </div>
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
        <ErrorState
          message={
            error instanceof Error
              ? error.message
              : tl({ zh: "无法加载数据发布", en: "Failed to load data releases" })
          }
          onRetry={() => refetch()}
        />
      ) : barsReleases.length > 0 ? (
        <ReleaseListTable releases={barsReleases} onOpenRelease={onOpenRelease} />
      ) : (
        <EmptyState
          icon={<Database className="h-8 w-8" />}
          title={tl({ zh: "暂无行情类发布", en: "No bar data releases yet" })}
          description={tl({
            zh: "行情缓存通过「创建数据发布」冻结后将在此列出，包含版本、覆盖率与质量状态。",
            en: "Bar caches frozen via “Create data release” will be listed here, with version, coverage and quality status.",
          })}
        />
      )}
    </div>
  );
}

/** 研究数据摄取说明卡:数据来源、质量门自动发布语义与任务中心入口。 */
function ResearchIngestCard({ kind }: { kind: ResearchReleaseKind }) {
  const { tl } = useT();
  return (
    <Alert variant="info">
      <AlertTitle>
        {tl({
          zh: "数据从哪里来？研究数据摄取（dataset_sync）",
          en: "Where does the data come from? Research data ingest (dataset_sync)",
        })}
      </AlertTitle>
      <AlertDescription className="space-y-2">
        <p>
          {kind === "convertible_metrics"
            ? tl({
                zh: "数据来自 dataset_sync 后台任务：转债日线随行情同步进入缓存，转股价等条款快照走 convertible_profiles 数据集；本页不直接触发联网拉取。",
                en: "Data comes from the dataset_sync background job: convertible daily bars enter the cache via market data sync, while terms snapshots (conversion price etc.) come from the convertible_profiles dataset; this page never fetches online directly.",
              })
            : tl({
                zh: "数据来自 dataset_sync 后台任务（按数据集定时/手动摄取进 research_* 表）；本页不直接触发联网拉取。",
                en: "Data comes from the dataset_sync background job (scheduled or manual ingestion into the research_* tables); this page never fetches online directly.",
              })}
        </p>
        <p>
          {tl({
            zh: "质量门通过即自动标记发布（mark_published）；摄取 ≠ 发布 —— 研究运行只引用显式创建的数据发布。",
            en: "Passing the quality gate auto-marks the ingest as published; ingesting is not publishing — research runs only reference explicitly created data releases.",
          })}
        </p>
        <Link
          to="/jobs?kind=dataset_sync"
          className="inline-flex items-center gap-1 font-medium text-primary hover:underline"
        >
          {tl({
            zh: "前往任务中心查看 dataset_sync 任务",
            en: "Open the job center for dataset_sync jobs",
          })}
          <ArrowUpRight className="h-3.5 w-3.5" />
        </Link>
      </AlertDescription>
    </Alert>
  );
}

/** 研究数据类型页签:类型说明卡 + 摄取说明卡 + 预选该 kind 的发布表单 + 该 kind 发布列表。 */
function DatasetTypeTab({
  info,
  onGoToBars,
  onGoToInstruments,
  onOpenRelease,
}: {
  info: ResearchKindInfo;
  onGoToBars: () => void;
  onGoToInstruments: () => void;
  onOpenRelease: (releaseId: string) => void;
}) {
  const { tl } = useT();
  const { data, isLoading, isError, error, refetch, isFetching } = useReleasesQuery();
  const kindReleases = useMemo(
    () => (data ?? []).filter((rel) => releaseDatasetKind(rel) === info.kind),
    [data, info.kind],
  );
  const InfoIcon = info.icon;

  return (
    <div className="space-y-5">
      <section className="rounded-lg border border-border bg-card p-4">
        <div className="flex flex-wrap items-center gap-2">
          <InfoIcon className="h-4 w-4 text-primary" />
          <h2 className="text-base font-semibold">{tl(info.title)}</h2>
          <Badge variant="secondary" className="font-mono text-xs">
            {info.kind}
          </Badge>
          <Badge variant="outline" className="font-mono text-xs">
            {KIND_DEFAULTS[info.kind].datasetName}
          </Badge>
        </div>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
          {tl(info.summary)}
        </p>
        <div className="mt-3">
          <p className="text-xs font-medium text-muted-foreground">
            {tl({ zh: "字段示例", en: "Example fields" })}
          </p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {info.fields.map((field) => (
              <Badge key={field} variant="outline" className="font-mono text-xs">
                {field}
              </Badge>
            ))}
          </div>
        </div>
        <p className="mt-3 max-w-3xl text-xs leading-5 text-muted-foreground">
          <span className="font-medium text-foreground">
            {tl({ zh: "PIT 语义：", en: "PIT semantics: " })}
          </span>
          {tl(info.pitNote)}
        </p>
      </section>

      <ResearchIngestCard kind={info.kind} />

      <ReleasePublisher
        onGoToFetch={onGoToBars}
        onGoToInstruments={onGoToInstruments}
        existingReleases={data ?? []}
        releaseNamesReady={!isLoading && !isError}
        allowedKinds={[info.kind]}
        initialKind={info.kind}
      />

      <div>
        <div className="mb-3 flex items-center justify-between">
          <div className="flex items-center gap-1.5">
            <p className="text-sm text-muted-foreground">
              {data
                ? tl({
                    zh: `共 ${kindReleases.length} 条${tl(info.title)}发布`,
                    en: `${kindReleases.length} ${tl(info.title)} releases`,
                  })
                : tl({ zh: "加载中…", en: "Loading…" })}
            </p>
            <ResearchHint hint={RESEARCH_HINTS.data.releaseList} />
          </div>
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
          <LoadingState rows={4} />
        ) : isError ? (
          <ErrorState
            message={
              error instanceof Error
                ? error.message
                : tl({ zh: "无法加载数据发布", en: "Failed to load data releases" })
            }
            onRetry={() => refetch()}
          />
        ) : kindReleases.length > 0 ? (
          <ReleaseListTable releases={kindReleases} onOpenRelease={onOpenRelease} />
        ) : (
          <EmptyState
            icon={<Database className="h-8 w-8" />}
            title={tl({ zh: "暂无该类型发布", en: "No releases of this kind yet" })}
            description={tl({
              zh: "先在任务中心完成 dataset_sync 摄取，再通过上方表单创建该类型的不可变发布。",
              en: "Finish the dataset_sync ingest in the job center first, then create an immutable release of this kind with the form above.",
            })}
          />
        )}
      </div>
    </div>
  );
}

function LifecyclePanel({ symbol }: { symbol: string }) {
  const { tl } = useT();
  const { data, isLoading, isError } = useQuery({
    queryKey: ["instrument-lifecycle", symbol],
    queryFn: () => datasetApi.lifecycle(symbol, { limit: 50 }),
  });

  if (isLoading) {
    return <LoadingState rows={3} />;
  }
  if (isError) {
    return (
      <p className="px-4 py-3 text-sm text-destructive">
        {tl({ zh: "生命周期事件加载失败", en: "Failed to load lifecycle events" })}
      </p>
    );
  }
  if (!data || data.length === 0) {
    return (
      <p className="px-4 py-3 text-sm text-muted-foreground">
        {tl({ zh: "该标的无生命周期事件记录。", en: "No lifecycle events recorded for this instrument." })}
      </p>
    );
  }

  return (
    <div className="px-4 pb-3">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{tl({ zh: "事件类型", en: "Event type" })}</TableHead>
            <TableHead>{tl({ zh: "日期", en: "Date" })}</TableHead>
            <TableHead>{tl({ zh: "来源 / 版本", en: "Source / Version" })}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {data.map((ev: LifecycleEvent) => (
            <TableRow key={ev.id}>
              <TableCell>
                <Badge variant="secondary">{ev.event_type}</Badge>
              </TableCell>
              <TableCell className="tabular-nums text-muted-foreground">
                {formatDateTime(ev.effective_date)}
              </TableCell>
              <TableCell className="text-muted-foreground">
                <span>{ev.source}</span>
                <span className="ml-2 font-mono text-xs opacity-70">
                  {ev.dataset_version}
                </span>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function InstrumentsTab({ onGoToFetch }: { onGoToFetch: () => void }) {
  const { tl } = useT();
  const [search, setSearch] = useState("");
  const [market, setMarket] = useState<string>("all");
  const [instrumentType, setInstrumentType] = useState<string>("all");
  const [status, setStatus] = useState<string>("all");
  const [expandedRow, setExpandedRow] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const pageSize = 50;

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: [
      "research-instruments",
      {
        search,
        market,
        instrumentType,
        status,
        limit: pageSize,
        offset: page * pageSize,
      },
    ],
    queryFn: () =>
      datasetApi.instruments({
        q: search.trim() || undefined,
        market: market === "all" ? undefined : market,
        instrument_type: instrumentType === "all" ? undefined : instrumentType,
        status,
        limit: pageSize,
        offset: page * pageSize,
      }),
  });
  const { data: instrumentSummary } = useQuery({
    queryKey: ["research-instrument-summary"],
    queryFn: () => datasetApi.instrumentSummary(),
  });
  const { data: etfSummary } = useQuery({
    queryKey: ["etf-summary"],
    queryFn: () => datasetApi.etfSummary(),
  });
  const { data: cacheStats } = useQuery({
    queryKey: ["research-cache-count"],
    queryFn: () => datasetApi.cachedData({ limit: 1 }),
  });

  const items = data?.items ?? [];
  const metadataBehindCache =
    data !== undefined &&
    cacheStats !== undefined &&
    cacheStats.total > 0 &&
    data.total < cacheStats.total;

  const toggleRow = (code: string) => {
    setExpandedRow((prev) => (prev === code ? null : code));
  };

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <div className="relative min-w-[220px] flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder={tl({ zh: "搜索代码或名称…", en: "Search code or name…" })}
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(0);
            }}
            className="pl-9"
          />
        </div>
        <Select
          value={market}
          onValueChange={(v) => {
            setMarket(v);
            setPage(0);
          }}
        >
          <SelectTrigger className="w-[140px]">
            <SelectValue placeholder={tl({ zh: "市场", en: "Market" })} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{tl({ zh: "全部市场", en: "All markets" })}</SelectItem>
            <SelectItem value="a_share">{tl({ zh: "A 股", en: "A-share" })}</SelectItem>
            <SelectItem value="hk">{tl({ zh: "港股", en: "HK stocks" })}</SelectItem>
            <SelectItem value="us">{tl({ zh: "美股", en: "US stocks" })}</SelectItem>
            <SelectItem value="future">{tl({ zh: "期货", en: "Futures" })}</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={instrumentType}
          onValueChange={(v) => {
            setInstrumentType(v);
            setPage(0);
          }}
        >
          <SelectTrigger className="w-[120px]">
            <SelectValue placeholder={tl({ zh: "类型", en: "Type" })} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{tl({ zh: "全部类型", en: "All types" })}</SelectItem>
            <SelectItem value="stock">{tl({ zh: "股票", en: "Stock" })}</SelectItem>
            <SelectItem value="etf">ETF</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={status}
          onValueChange={(v) => {
            setStatus(v);
            setPage(0);
          }}
        >
          <SelectTrigger className="w-[140px]">
            <SelectValue placeholder={tl({ zh: "状态", en: "Status" })} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{tl({ zh: "全部状态", en: "All statuses" })}</SelectItem>
            <SelectItem value="active">{tl({ zh: "正常", en: "Active" })}</SelectItem>
            <SelectItem value="suspended">{tl({ zh: "停牌", en: "Suspended" })}</SelectItem>
            <SelectItem value="pending_delist">{tl({ zh: "待确认退市", en: "Pending delisting" })}</SelectItem>
            <SelectItem value="delisted">{tl({ zh: "已退市", en: "Delisted" })}</SelectItem>
          </SelectContent>
        </Select>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          {tl({ zh: "刷新", en: "Refresh" })}
        </Button>
        <span className="text-sm text-muted-foreground">
          {data
            ? tl({
                zh: `显示 ${page * pageSize + 1}-${Math.min((page + 1) * pageSize, data.total)} / ${data.total} 只标的`,
                en: `Showing ${page * pageSize + 1}-${Math.min((page + 1) * pageSize, data.total)} of ${data.total} instruments`,
              })
            : ""}
        </span>
      </div>

      {instrumentSummary && (
        <div className="mb-4 space-y-3 rounded-lg border border-border bg-card p-4">
          <div className="flex items-center gap-1.5">
            <h3 className="text-sm font-semibold">{tl({ zh: "标的字典概览", en: "Instrument dictionary overview" })}</h3>
            <ResearchHint hint={RESEARCH_HINTS.data.instrumentMetadata} />
          </div>
          <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
            <div>
              <span className="text-muted-foreground">{tl({ zh: "全部标的", en: "All instruments" })}</span>
              <span className="ml-2 font-semibold tabular-nums">{instrumentSummary.total}</span>
            </div>
            <div>
              <span className="text-muted-foreground">{tl({ zh: "当前活跃", en: "Currently active" })}</span>
              <span className="ml-2 font-semibold tabular-nums">{instrumentSummary.active_total}</span>
            </div>
            {Object.entries(instrumentSummary.by_instrument_type).map(([key, count]) => (
              <div key={key}>
                <span className="text-muted-foreground">
                  {key in INSTRUMENT_TYPE_LABELS ? tl(INSTRUMENT_TYPE_LABELS[key]) : key}
                </span>
                <span className="ml-2 font-semibold tabular-nums">{count}</span>
              </div>
            ))}
          </div>
          <div className="grid gap-3 border-t border-border pt-3 text-xs sm:grid-cols-3">
            <div>
              <p className="mb-1 font-medium text-muted-foreground">{tl({ zh: "按状态", en: "By status" })}</p>
              <div className="flex flex-wrap gap-x-3 gap-y-1">
                {Object.entries(instrumentSummary.by_status)
                  .sort(([a], [b]) => {
                    const ai = STATUS_ORDER.indexOf(a);
                    const bi = STATUS_ORDER.indexOf(b);
                    return (ai === -1 ? STATUS_ORDER.length : ai) - (bi === -1 ? STATUS_ORDER.length : bi);
                  })
                  .map(([key, count]) => (
                    <span key={key} className="text-muted-foreground">
                      {key in INSTRUMENT_STATUS_LABELS ? tl(INSTRUMENT_STATUS_LABELS[key]) : key} <span className="font-medium tabular-nums text-foreground">{count}</span>
                    </span>
                  ))}
              </div>
            </div>
            <div>
              <p className="mb-1 font-medium text-muted-foreground">{tl({ zh: "按市场", en: "By market" })}</p>
              <div className="flex flex-wrap gap-x-3 gap-y-1">
                {Object.entries(instrumentSummary.by_market).map(([key, count]) => (
                  <span key={key} className="text-muted-foreground">
                    {key in MARKET_LABELS ? tl(MARKET_LABELS[key]) : key} <span className="font-medium tabular-nums text-foreground">{count}</span>
                  </span>
                ))}
              </div>
            </div>
            <div>
              <p className="mb-1 font-medium text-muted-foreground">{tl({ zh: "ETF 口径", en: "ETF count" })}</p>
              <p className="text-muted-foreground">
                {tl({ zh: "当前活跃 ETF ", en: "Currently active ETFs: " })}
                <span className="font-medium tabular-nums text-foreground">
                  {instrumentSummary.active_etf_total}
                </span>
                {tl({ zh: " 只", en: "" })}
              </p>
            </div>
          </div>
        </div>
      )}

      {instrumentSummary && etfSummary && (
        <Alert className="mb-4 border-primary/25 bg-primary/5">
          <ResearchHint hint={RESEARCH_HINTS.data.instrumentCounts} className="mt-0.5 text-primary" />
          <AlertTitle>{tl({ zh: "ETF 数量有两套统计口径", en: "ETF counts come from two different sources" })}</AlertTitle>
          <AlertDescription>
            {tl({
              zh: `当前活跃标的池有 ${instrumentSummary.active_etf_total} 只 ETF，ETF 分类元数据目录有 ${etfSummary.total} 条记录。后者来自基金目录同步，会保留历史、已退市或暂未进入当前标的池的记录；行情拉取和数据发布以标的字典中的活跃标的为准。`,
              en: `The current active universe has ${instrumentSummary.active_etf_total} ETFs, while the ETF classification metadata directory has ${etfSummary.total} records. The latter comes from the fund directory sync and keeps historical, delisted, or not-yet-in-universe records; bar fetching and data releases follow the active instruments in the dictionary.`,
            })}
          </AlertDescription>
        </Alert>
      )}

      {(market === "all" || market === "a_share") && instrumentType !== "stock" && (
        <div className="mb-4 grid grid-cols-1 gap-3 lg:grid-cols-2">
          <EtfSyncPanel />
          <EtfReviewQueue />
        </div>
      )}

      {metadataBehindCache && (
        <Alert variant="warning" className="mb-4">
          <AlertTitle>{tl({ zh: "元数据尚未覆盖现有行情缓存", en: "Metadata does not yet cover existing bar caches" })}</AlertTitle>
          <AlertDescription className="flex flex-col items-start justify-between gap-3 sm:flex-row sm:items-center">
            <p>
              {tl({
                zh: `当前数据库只有 ${data.total} 只匹配的活跃标的元数据，但本地已有 ${cacheStats.total} 条行情缓存。两者独立存储；缓存文件不会自动生成名称、市场、上市状态和生命周期资料。`,
                en: `The database currently has metadata for only ${data.total} matching active instruments, while the local cache already holds ${cacheStats.total} bar cache entries. The two are stored independently; cache files do not automatically gain name, market, listing status or lifecycle information.`,
              })}
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="shrink-0"
              onClick={onGoToFetch}
            >
              {tl({ zh: "前往同步标的池", en: "Go to universe sync" })}
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {isLoading ? (
        <LoadingState rows={8} />
      ) : isError ? (
        <ErrorState
          message={
            error instanceof Error
              ? error.message
              : tl({ zh: "无法加载标的元数据", en: "Failed to load instrument metadata" })
          }
          onRetry={() => refetch()}
        />
      ) : items.length > 0 ? (
        <div className="overflow-x-auto rounded-lg border border-border">
          <ScrollArea className="max-h-[600px]">
            <Table>
              <TableHeader className="sticky top-0 bg-card">
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>{tl({ zh: "代码", en: "Code" })}</TableHead>
                  <TableHead>{tl({ zh: "名称", en: "Name" })}</TableHead>
                  <TableHead>{tl({ zh: "市场", en: "Market" })}</TableHead>
                  <TableHead>{tl({ zh: "类型", en: "Type" })}</TableHead>
                  <TableHead>{tl({ zh: "上市日期", en: "List date" })}</TableHead>
                  <TableHead>{tl({ zh: "状态", en: "Status" })}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((inst) => {
                  const isOpen = expandedRow === inst.code;
                  return (
                    <Fragment key={inst.code}>
                      <TableRow
                        onClick={() => toggleRow(inst.code)}
                        className={cn("cursor-pointer", isOpen && "bg-muted/50")}
                      >
                        <TableCell>
                          <ChevronRight
                            className={cn(
                              "h-4 w-4 text-muted-foreground transition-transform",
                              isOpen && "rotate-90",
                            )}
                          />
                        </TableCell>
                        <TableCell className="font-mono font-medium">
                          {inst.code}
                        </TableCell>
                        <TableCell>{inst.name}</TableCell>
                        <TableCell className="text-muted-foreground">
                          {inst.market in MARKET_LABELS ? tl(MARKET_LABELS[inst.market]) : inst.market}
                        </TableCell>
                        <TableCell className="text-muted-foreground">
                          {inst.instrument_type in INSTRUMENT_TYPE_LABELS
                            ? tl(INSTRUMENT_TYPE_LABELS[inst.instrument_type])
                            : inst.instrument_type}
                        </TableCell>
                        <TableCell className="tabular-nums text-muted-foreground">
                          {inst.list_date ?? "—"}
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center gap-1">
                            {inst.status === "suspended" ? (
                              <Badge variant="destructive">{tl({ zh: "停牌", en: "Suspended" })}</Badge>
                            ) : inst.status === "pending_delist" ? (
                              <Badge variant="warning">{tl({ zh: "待确认退市", en: "Pending delisting" })}</Badge>
                            ) : inst.status === "active" ? (
                              <Badge variant="success">{tl({ zh: "正常", en: "Active" })}</Badge>
                            ) : (
                              <StatusBadge status={inst.status}>
                                {inst.status in INSTRUMENT_STATUS_LABELS
                                  ? tl(INSTRUMENT_STATUS_LABELS[inst.status])
                                  : inst.status}
                              </StatusBadge>
                            )}
                          </div>
                        </TableCell>
                      </TableRow>
                      {isOpen && (
                        <TableRow key={`${inst.code}-detail`}>
                          <TableCell colSpan={7} className="bg-muted/30 p-0">
                            <div className="grid grid-cols-2 gap-x-6 gap-y-2 border-b border-border px-4 py-3 text-xs md:grid-cols-4">
                              <div>
                                <span className="text-muted-foreground">{tl({ zh: "市场 / 类型", en: "Market / Type" })}</span>
                                <p className="mt-0.5 font-medium">
                                  {tl({
                                    zh: `${inst.market in MARKET_LABELS ? tl(MARKET_LABELS[inst.market]) : inst.market} · ${inst.instrument_type in INSTRUMENT_TYPE_LABELS ? tl(INSTRUMENT_TYPE_LABELS[inst.instrument_type]) : inst.instrument_type}`,
                                    en: `${inst.market in MARKET_LABELS ? tl(MARKET_LABELS[inst.market]) : inst.market} · ${inst.instrument_type in INSTRUMENT_TYPE_LABELS ? tl(INSTRUMENT_TYPE_LABELS[inst.instrument_type]) : inst.instrument_type}`,
                                  })}
                                </p>
                              </div>
                              <div>
                                <span className="text-muted-foreground">{tl({ zh: "状态", en: "Status" })}</span>
                                <p className="mt-0.5 font-medium">
                                  {inst.status in INSTRUMENT_STATUS_LABELS
                                    ? tl(INSTRUMENT_STATUS_LABELS[inst.status])
                                    : inst.status}
                                </p>
                              </div>
                              <div>
                                <span className="text-muted-foreground">{tl({ zh: "交易所", en: "Exchange" })}</span>
                                <p className="mt-0.5 font-medium">{inst.exchange ?? "—"}</p>
                              </div>
                              <div>
                                <span className="text-muted-foreground">{tl({ zh: "行业 / 板块", en: "Industry / Sector" })}</span>
                                <p className="mt-0.5 font-medium">{[inst.industry, inst.sector].filter(Boolean).join(" / ") || "—"}</p>
                              </div>
                            </div>
                            {inst.instrument_type === "etf" && (
                              <EtfMetadataEditor symbol={inst.code} />
                            )}
                            <LifecyclePanel symbol={inst.code} />
                          </TableCell>
                        </TableRow>
                      )}
                    </Fragment>
                  );
                })}
              </TableBody>
            </Table>
          </ScrollArea>
        </div>
      ) : (
        <EmptyState
          icon={<Layers className="h-8 w-8" />}
          title={
            search || market !== "all" || instrumentType !== "all" || status !== "all"
              ? tl({ zh: "无匹配标的", en: "No matching instruments" })
              : tl({ zh: "暂无标的元数据", en: "No instrument metadata yet" })
          }
          description={
            search || market !== "all" || instrumentType !== "all" || status !== "all"
              ? tl({ zh: "尝试调整搜索关键词或市场筛选条件。", en: "Try adjusting the search keyword or market filters." })
              : tl({
                  zh: "先在行情拉取页同步标的池，名称、市场、类型和上市状态才会写入数据库。",
                  en: "Sync the universe on the bar data fetch page first; name, market, type and listing status are then written to the database.",
                })
          }
          action={
            !search && market === "all" && instrumentType === "all" && status === "all" ? (
              <Button type="button" variant="outline" onClick={onGoToFetch}>
                {tl({ zh: "前往同步标的池", en: "Go to universe sync" })}
              </Button>
            ) : undefined
          }
        />
      )}

      {data && data.total > pageSize && (
        <div className="mt-3 flex items-center justify-center gap-4">
          <Button
            variant="outline"
            size="sm"
            disabled={page === 0 || isFetching}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
          >
            {tl({ zh: "上一页", en: "Previous" })}
          </Button>
          <span className="text-sm text-muted-foreground tabular-nums">
            {tl({
              zh: `第 ${page + 1} / ${Math.ceil(data.total / pageSize)} 页`,
              en: `Page ${page + 1} / ${Math.ceil(data.total / pageSize)}`,
            })}
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={
              (page + 1) * pageSize >= data.total || isFetching
            }
            onClick={() => setPage((p) => p + 1)}
          >
            {tl({ zh: "下一页", en: "Next" })}
          </Button>
        </div>
      )}
    </div>
  );
}

export default function ResearchData() {
  const { tl } = useT();
  const [searchParams, setSearchParams] = useSearchParams();
  // 发布详情抽屉走 ?release= 深链(研究运行「冻结输入」卡片/因子实验室跳转目标)。
  const drawerRelease = searchParams.get("release");
  const openRelease = (releaseId: string) => {
    setSearchParams({ release: releaseId }, { replace: true });
  };
  const closeRelease = () => {
    setSearchParams({}, { replace: true });
  };
  // 页签状态进 URL(?tab=):刷新不再跳回默认页签。
  // 旧深链兼容:fetch(行情拉取)与 releases(数据发布)合并进「行情」页签。
  const rawTab = searchParams.get("tab");
  const activeTab =
    rawTab === null || rawTab === "fetch" || rawTab === "releases"
      ? "bars"
      : rawTab;
  const setActiveTab = (value: string) => {
    const next: Record<string, string> = {};
    if (value !== "bars") next.tab = value;
    if (drawerRelease) next.release = drawerRelease;
    setSearchParams(next, { replace: true });
  };
  const activeTabHint = {
    bars: RESEARCH_HINTS.data.fetch,
    metrics: RESEARCH_HINTS.data.releases,
    financial: RESEARCH_HINTS.data.releases,
    convertible: RESEARCH_HINTS.data.releases,
    instruments: RESEARCH_HINTS.data.instrumentMetadata,
  }[activeTab];

  return (
    <div>
      <PageHeader
        title={tl({ zh: "数据与标的", en: "Data & Instruments" })}
        description={tl({
          zh: "按数据类型浏览:行情、每日指标、财务指标、可转债与标的元数据;各类型均可浏览已发布版本并发起新发布",
          en: "Browse by data type: market data, daily metrics, financial indicators, convertibles and instrument metadata; each type supports browsing published releases and creating new ones",
        })}
      />

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList className="h-auto max-w-full justify-start overflow-x-auto">
          <TabsTrigger value="bars">
            <HardDriveDownload className="mr-1.5 h-4 w-4" />
            {tl({ zh: "行情", en: "Market data" })}
          </TabsTrigger>
          <TabsTrigger value="metrics">
            <LineChart className="mr-1.5 h-4 w-4" />
            {tl({ zh: "每日指标", en: "Daily metrics" })}
          </TabsTrigger>
          <TabsTrigger value="financial">
            <Landmark className="mr-1.5 h-4 w-4" />
            {tl({ zh: "财务指标", en: "Financial indicators" })}
          </TabsTrigger>
          <TabsTrigger value="convertible">
            <Repeat className="mr-1.5 h-4 w-4" />
            {tl({ zh: "可转债", en: "Convertibles" })}
          </TabsTrigger>
          <TabsTrigger value="instruments">
            <Layers className="mr-1.5 h-4 w-4" />
            {tl({ zh: "标的与档案", en: "Instruments & profiles" })}
          </TabsTrigger>
        </TabsList>
        {activeTabHint && (
          <div className="mt-3 flex max-w-3xl items-start gap-2 text-sm text-muted-foreground">
            <ResearchHint hint={activeTabHint} className="mt-0.5 shrink-0" />
            <p>{tl(activeTabHint.description)}</p>
          </div>
        )}

        <TabsContent value="bars">
          <Suspense fallback={<LoadingState rows={5} />}>
            <MarketDataTab embedded />
          </Suspense>
          <div className="mt-6">
            <BarsReleasesSection
              onGoToInstruments={() => setActiveTab("instruments")}
              onOpenRelease={openRelease}
            />
          </div>
        </TabsContent>
        <TabsContent value="metrics">
          <DatasetTypeTab
            info={RESEARCH_KIND_META.daily_metrics}
            onGoToBars={() => setActiveTab("bars")}
            onGoToInstruments={() => setActiveTab("instruments")}
            onOpenRelease={openRelease}
          />
        </TabsContent>
        <TabsContent value="financial">
          <DatasetTypeTab
            info={RESEARCH_KIND_META.financial_indicators}
            onGoToBars={() => setActiveTab("bars")}
            onGoToInstruments={() => setActiveTab("instruments")}
            onOpenRelease={openRelease}
          />
        </TabsContent>
        <TabsContent value="convertible">
          <DatasetTypeTab
            info={RESEARCH_KIND_META.convertible_metrics}
            onGoToBars={() => setActiveTab("bars")}
            onGoToInstruments={() => setActiveTab("instruments")}
            onOpenRelease={openRelease}
          />
        </TabsContent>
        <TabsContent value="instruments">
          <InstrumentsTab onGoToFetch={() => setActiveTab("bars")} />
        </TabsContent>
      </Tabs>

      <ReleaseDetailDrawer releaseId={drawerRelease} onClose={closeRelease} />
    </div>
  );
}
