import { Fragment, useEffect, useMemo, useState, lazy, Suspense } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  Database,
  RefreshCw,
  Package,
  Search,
  ChevronRight,
  Layers,
  HardDriveDownload,
  Plus,
  X,
} from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
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
  type DatasetReleaseSummary,
  type EtfExecutionProfile,
  type LifecycleEvent,
} from "@/lib/research";
import { RESEARCH_HINTS } from "@/lib/research-hints";
import { cn, formatDateTime, formatNumber, formatPercent } from "@/lib/utils";
import {
  WorkflowIndicator,
  NextStepCTA,
  HintLabel,
  ResearchHint,
} from "@/components/research/ResearchHint";
import { SelectAllResultsButton } from "@/components/selection/SelectAllResultsButton";

const MarketDataTab = lazy(() => import("@/pages/Data"));

function coverageColor(pct: number): string {
  if (pct >= 95) return "bg-success";
  if (pct >= 80) return "bg-primary";
  if (pct >= 60) return "bg-warning";
  return "bg-destructive";
}

const MULTI_ASSET_CAPABILITIES = [
  "stock",
  "etf:index",
  "etf:cross_border",
  "etf:commodity",
  "etf:bond",
];

const ETF_EXECUTION_PROFILES: {
  value: EtfExecutionProfile;
  label: string;
  execution: string;
}[] = [
  { value: "domestic_equity_etf", label: "国内股票 ETF", execution: "T+1 交收" },
  { value: "cross_border_etf", label: "跨境 ETF", execution: "境外资产，T+0、免印花税" },
  { value: "bond_etf", label: "债券 ETF", execution: "固定收益，10 份/手、免印花税" },
  { value: "money_market_etf", label: "货币 ETF", execution: "现金管理，T+0、无佣金" },
  { value: "commodity_etf", label: "商品 ETF", execution: "黄金/商品，T+0" },
];

const ETF_MARKETS = [
  { value: "domestic", label: "国内（A 股）" },
  { value: "hk", label: "港股通" },
  { value: "overseas", label: "海外" },
  { value: "global", label: "全球" },
] as const;

const ETF_STRATEGIES = [
  { value: "index", label: "被动指数" },
  { value: "active", label: "主动管理" },
] as const;

const REVIEW_STATUS_LABELS: Record<string, string> = {
  auto_adopted: "自动采用",
  needs_review: "待复核",
  manually_confirmed: "已确认",
  manually_overridden: "已覆盖",
};

const INSTRUMENT_TYPE_LABELS: Record<string, string> = {
  stock: "股票",
  etf: "ETF",
};

const MARKET_LABELS: Record<string, string> = {
  a_share: "A 股",
  hk: "港股",
  us: "美股",
  future: "期货",
};

const INSTRUMENT_STATUS_LABELS: Record<string, string> = {
  active: "正常",
  suspended: "停牌",
  pending_delist: "待确认退市",
  delisted: "已退市",
};

const STATUS_ORDER = ["active", "suspended", "pending_delist", "delisted"];

function EtfMetadataEditor({
  symbol,
  onSaved,
}: {
  symbol: string;
  onSaved?: () => void;
}) {
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
        throw new Error("请选择执行档位");
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
  const reviewLabel = data ? REVIEW_STATUS_LABELS[data.review_status] ?? data.review_status : null;

  return (
    <div className="space-y-3 px-4 py-4">
      <div>
        <h3 className="text-sm font-semibold">ETF 研究分类</h3>
        <p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
          多维分类决定回测中的资产大类、交收周期和手数规则。执行档位是权威维度，
          标的市场与策略类型用于组合分析与风控。修改后设为「已覆盖」，自动同步不再覆盖。
        </p>
      </div>
      {isLoading ? (
        <LoadingState rows={2} />
      ) : isError ? (
        <p className="text-sm text-destructive">ETF 元数据加载失败，请重试。</p>
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
                <span className="text-muted-foreground">人工覆盖（自动同步跳过）</span>
              )}
              {data.confidence && Number(data.confidence) > 0 && (
                <span className="text-muted-foreground">
                  置信度 {formatPercent(Number(data.confidence))}
                </span>
              )}
              {data.source && (
                <span className="text-muted-foreground">来源 {data.source}</span>
              )}
            </div>
          )}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-4 md:items-end">
            <div className="space-y-2">
              <Label htmlFor={`etf-profile-${symbol}`}>执行档位</Label>
              <Select
                value={executionProfile}
                onValueChange={(value) => setExecutionProfile(value as EtfExecutionProfile)}
              >
                <SelectTrigger id={`etf-profile-${symbol}`}>
                  <SelectValue placeholder="请选择" />
                </SelectTrigger>
                <SelectContent>
                  {ETF_EXECUTION_PROFILES.map((item) => (
                    <SelectItem key={item.value} value={item.value}>
                      {item.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor={`etf-market-${symbol}`}>标的市场</Label>
              <Select value={underlyingMarket} onValueChange={setUnderlyingMarket}>
                <SelectTrigger id={`etf-market-${symbol}`}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ETF_MARKETS.map((item) => (
                    <SelectItem key={item.value} value={item.value}>
                      {item.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor={`etf-strategy-${symbol}`}>策略类型</Label>
              <Select value={strategyType} onValueChange={setStrategyType}>
                <SelectTrigger id={`etf-strategy-${symbol}`}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ETF_STRATEGIES.map((item) => (
                    <SelectItem key={item.value} value={item.value}>
                      {item.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor={`etf-index-${symbol}`}>跟踪指数（可选）</Label>
              <Input
                id={`etf-index-${symbol}`}
                value={underlyingIndex}
                onChange={(event) => setUnderlyingIndex(event.target.value)}
                placeholder="例如 000300.SH"
                spellCheck={false}
              />
            </div>
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_auto] md:items-end">
            <div className="space-y-2">
              <Label htmlFor={`etf-reason-${symbol}`}>修改理由（可选，记录到审计）</Label>
              <Input
                id={`etf-reason-${symbol}`}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="例如：根据基金合同第 X 条修正为跨境 ETF"
              />
            </div>
            <Button
              type="button"
              onClick={() => save.mutate()}
              disabled={!executionProfile || save.isPending}
            >
              {save.isPending ? "保存中…" : data?.execution_profile ? "保存修改" : "补齐元数据"}
            </Button>
          </div>
          {selectedProfile && (
            <p className="text-xs text-muted-foreground">
              将按「{selectedProfile.label}」处理：{selectedProfile.execution}。
            </p>
          )}
          {data?.evidence && data.evidence.length > 0 && (
            <div className="space-y-1">
              <p className="text-xs font-medium text-muted-foreground">分类证据</p>
              <ul className="list-inside list-disc space-y-0.5 text-xs text-muted-foreground">
                {data.evidence.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            </div>
          )}
          {save.isSuccess && (
            <Alert variant="success" aria-live="polite">
              <AlertTitle>ETF 元数据已保存</AlertTitle>
              <AlertDescription>
                {symbol} 分类已更新并标记为人工覆盖，现在可以重新校验数据发布。
              </AlertDescription>
            </Alert>
          )}
          {save.isError && (
            <Alert variant="destructive" aria-live="assertive">
              <AlertTitle>ETF 元数据保存失败</AlertTitle>
              <AlertDescription>
                {save.error instanceof Error ? save.error.message : "请稍后重试"}
              </AlertDescription>
            </Alert>
          )}
        </>
      )}
    </div>
  );
}

function EtfSyncPanel({ onDone }: { onDone?: () => void }) {
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
        <h3 className="text-sm font-semibold">ETF 元数据批量同步</h3>
        <p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
          从 akshare 拉取全市场 ETF 基金类型并自动分类。高置信度结果自动采用，
          低置信度进入待复核队列。人工覆盖的记录不会被覆盖。
        </p>
      </div>
      {summary.data && (
        <div className="grid grid-cols-2 gap-2 text-xs md:grid-cols-4 lg:grid-cols-7">
          {[
            { label: "总计", value: summary.data.total },
            { label: "自动采用", value: summary.data.auto_adopted },
            { label: "待复核", value: summary.data.needs_review },
            { label: "已确认", value: summary.data.manually_confirmed },
            { label: "人工覆盖", value: summary.data.manually_overridden },
            { label: "缺失", value: summary.data.missing_metadata },
          ].map((item) => (
            <div key={item.label} className="rounded border p-2 text-center">
              <div className="text-lg font-semibold">{item.value}</div>
              <div className="text-muted-foreground">{item.label}</div>
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
          {sync.isPending ? "同步中…" : "预览（dry-run）"}
        </Button>
        <Button
          size="sm"
          onClick={() => sync.mutate(false)}
          disabled={sync.isPending}
        >
          执行同步
        </Button>
      </div>
      {dryRunResult && (
        <div className="rounded border p-3 text-xs">
          <p className="font-medium">同步预览结果</p>
          <ul className="mt-1 space-y-0.5 text-muted-foreground">
            <li>共 {dryRunResult.total} 只 ETF</li>
            <li>新增 {dryRunResult.insert}，更新 {dryRunResult.update}，跳过（人工覆盖）{dryRunResult.skip}</li>
            <li>自动采用 {dryRunResult.adopt}，待复核 {dryRunResult.review}</li>
          </ul>
        </div>
      )}
      {sync.isError && (
        <Alert variant="destructive">
          <AlertTitle>同步失败</AlertTitle>
          <AlertDescription>
            {sync.error instanceof Error ? sync.error.message : "请稍后重试"}
          </AlertDescription>
        </Alert>
      )}
    </div>
  );
}

function EtfReviewQueue({ onFixed }: { onFixed?: () => void }) {
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
        没有待复核的 ETF 分类。
      </p>
    );
  }

  return (
    <div className="space-y-2 px-4 py-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">ETF 待复核队列（{data.length}）</h3>
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
            {selected.size === data.length ? "取消全选" : "全选"}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => confirm.mutate([...selected])}
            disabled={selected.size === 0 || confirm.isPending}
          >
            批量确认（{selected.size}）
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
              置信度 {formatPercent(Number(item.confidence))}
            </span>
          </label>
        ))}
      </div>
      {confirm.isError && (
        <p className="text-sm text-destructive">
          批量确认失败：{confirm.error instanceof Error ? confirm.error.message : "请重试"}
        </p>
      )}
    </div>
  );
}

function nextReleaseNames(
  releases: DatasetReleaseSummary[],
  releaseKind: DatasetReleaseCreate["release_kind"],
) {
  const stamp = new Date().toISOString().slice(0, 10);
  const compact = stamp.replaceAll("-", "");
  const prefix =
    releaseKind === "a_share_tushare" ? "a-share-bars" : "multi-asset-bars";
  const releasePrefix = `${prefix}-${compact}-v`;
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

function summarizePublishError(error: unknown): {
  summary: string;
  details?: string;
} {
  const message =
    error instanceof Error ? error.message : "请检查缓存范围与质量门配置";
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
    summary: `质量门发现 ${reasons.length} 项阻塞（能力类别 ${capabilityCount} 项，标的 ${instrumentCount} 项）。${preview}${reasons.length > 5 ? "……" : ""}`,
    details: message,
  };
}

function ReleasePublisher({
  onGoToFetch,
  onGoToInstruments,
  existingReleases,
  releaseNamesReady,
}: {
  onGoToFetch: () => void;
  onGoToInstruments: () => void;
  existingReleases: DatasetReleaseSummary[];
  releaseNamesReady: boolean;
}) {
  const queryClient = useQueryClient();
  const defaults = nextReleaseNames([], "a_share_tushare");
  const [isOpen, setIsOpen] = useState(false);
  const [cacheSearch, setCacheSearch] = useState("");
  const [datasetName, setDatasetName] = useState("a_share_daily_bars");
  const [releaseId, setReleaseId] = useState(defaults.releaseId);
  const [version, setVersion] = useState(defaults.version);
  const [releaseKind, setReleaseKind] = useState<DatasetReleaseCreate["release_kind"]>(
    "a_share_tushare",
  );
  const [adjustment, setAdjustment] =
    useState<DatasetReleaseCreate["adjustment"]>("qfq");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [selected, setSelected] = useState<Record<string, CachedDataStatus>>({});

  const {
    data: cached,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: [
      "release-cache-candidates",
      { q: cacheSearch, period: "1d", adjustment },
    ],
    queryFn: () =>
      datasetApi.cachedData({
        q: cacheSearch || undefined,
        period: "1d",
        adjust: adjustment,
        limit: 50,
      }),
    enabled: isOpen,
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
  const source = releaseKind === "a_share_tushare" ? "tushare" : "mixed";
  const setNextReleaseNames = (
    nextKind: DatasetReleaseCreate["release_kind"],
    releases = existingReleases,
  ) => {
    const next = nextReleaseNames(releases, nextKind);
    setVersion(next.version);
    setReleaseId(next.releaseId);
  };
  const toggleSymbol = (item: CachedDataStatus) => {
    if (releaseKind === "a_share_tushare" && item.source !== "tushare") {
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
      }),
    onSuccess: (selection) => {
      setSelected((previous) => {
        const next = { ...previous };
        for (const item of selection.items) {
          if (releaseKind === "a_share_tushare" && item.source !== "tushare") {
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
    mutationFn: () =>
      datasetApi.createRelease({
        release_id: releaseId.trim(),
        dataset_name: datasetName.trim(),
        release_kind: releaseKind,
        source,
        version: version.trim(),
        symbols: selectedItems.map((item) => item.symbol),
        start_date: startDate,
        end_date: endDate,
        adjustment,
        required_capabilities:
          releaseKind === "multi_asset_mixed" ? MULTI_ASSET_CAPABILITIES : ["stock"],
      }),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ["dataset-releases"] });
      queryClient.invalidateQueries({ queryKey: ["dataset-manifests"] });
      setNextReleaseNames(releaseKind, [...existingReleases, created]);
    },
  });

  const selectedSources = new Set(
    selectedItems
      .map((item) => item.source)
      .filter((item): item is string => Boolean(item)),
  );
  const sourcePolicySatisfied =
    releaseKind === "a_share_tushare"
      ? selectedItems.every((item) => item.source === "tushare")
      : selectedSources.size >= 2;
  const canPublish =
    releaseId.trim().length >= 3 &&
    datasetName.trim().length >= 3 &&
    version.trim().length > 0 &&
    selectedItems.length > 0 &&
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
  const publishError = summarizePublishError(publish.error);

  if (!isOpen) {
    return (
      <Alert variant="info" className="mb-5">
        <AlertTitle className="flex items-center gap-1.5">
          发布会固化哪些内容？
          <ResearchHint hint={RESEARCH_HINTS.data.releases} />
        </AlertTitle>
        <AlertDescription className="flex flex-col items-start justify-between gap-3 sm:flex-row sm:items-center">
          <p>
            从本地缓存选择标的和日期范围，冻结成不可修改、带 checksum
            和质量报告的数据版本。后续研究只引用发布版本，不再读取会变化的缓存。
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
            创建数据发布
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
            创建不可变数据发布
          </h2>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            这里只冻结已下载到本地的日线缓存，不联网补数据，也不会启动回测或模拟盘。
          </p>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="收起创建数据发布"
          onClick={() => setIsOpen(false)}
        >
          <X />
        </Button>
      </div>

      <div className="space-y-5 p-4">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          <div className="space-y-2">
            <Label htmlFor="release-kind">发布类型</Label>
            <Select
              value={releaseKind}
              onValueChange={(value) => {
                const next = value as DatasetReleaseCreate["release_kind"];
                setReleaseKind(next);
                setNextReleaseNames(next);
                setDatasetName(
                  next === "a_share_tushare"
                    ? "a_share_daily_bars"
                    : "multi_asset_daily_bars",
                );
                clearSelection();
              }}
            >
              <SelectTrigger id="release-kind">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="a_share_tushare">A股 Tushare 单源</SelectItem>
                <SelectItem value="multi_asset_mixed">多资产混合来源</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="release-dataset-name">
              <HintLabel hint={RESEARCH_HINTS.data.datasetName}>数据集名称</HintLabel>
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
              <HintLabel hint={RESEARCH_HINTS.data.version}>版本</HintLabel>
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
              <HintLabel hint={RESEARCH_HINTS.data.releaseId}>发布 ID</HintLabel>
            </Label>
            <Input
              id="release-id"
              value={releaseId}
              onChange={(event) => setReleaseId(event.target.value)}
              spellCheck={false}
            />
          </div>
          <div className="space-y-2">
            <Label>来源策略</Label>
            <div className="flex min-h-10 items-center rounded-md border border-input bg-muted/40 px-3 text-sm">
              {releaseKind === "a_share_tushare"
                ? "严格单源：tushare"
                : "混合来源：逐标的记录实际来源"}
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="release-adjustment">
              <HintLabel hint={RESEARCH_HINTS.data.adjustment}>复权方式</HintLabel>
            </Label>
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
                <SelectItem value="qfq">前复权（qfq）</SelectItem>
                <SelectItem value="hqfq">后复权（hqfq）</SelectItem>
                <SelectItem value="none">不复权</SelectItem>
              </SelectContent>
            </Select>
          </div>
          </div>

        {releaseKind === "multi_asset_mixed" ? (
          <Alert variant="warning">
            <AlertTitle>多资产混合源发布会保留来源血缘</AlertTitle>
            <AlertDescription>
              必须同时包含股票、宽基/指数、跨境、商品和债券 ETF，且每个标的的元数据、
              日期覆盖和质量检查均通过。实际来源少于两种时不会生成发布记录。
            </AlertDescription>
          </Alert>
        ) : (
          <Alert variant="info">
            <AlertTitle>A股单源发布严格失败关闭</AlertTitle>
            <AlertDescription>
              只能发布 A 股股票，所选缓存的每根 Bar 都必须来自 tushare；任何 ETF、
              未记录来源或备用源修补数据都会阻止发布。
            </AlertDescription>
          </Alert>
        )}

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="release-start-date">开始日期</Label>
            <Input
              id="release-start-date"
              type="date"
              value={startDate}
              max={endDate || undefined}
              onChange={(event) => setStartDate(event.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="release-end-date">结束日期</Label>
            <Input
              id="release-end-date"
              type="date"
              value={endDate}
              min={startDate || undefined}
              onChange={(event) => setEndDate(event.target.value)}
            />
          </div>
        </div>
        <Alert variant="info">
          <AlertTitle>发布周期不要求每只标的全程存在</AlertTitle>
          <AlertDescription>
            自动日期范围覆盖所选缓存的最早至最晚日期。质量门只检查每只标的从上市到退市之间
            与发布周期重叠的部分；中途上市或退市不会被当作缺失。停牌零成交 Bar 会保留并标记，
            有停复牌生命周期事件时也允许停牌日无 Bar；两者都没有的缺口因无法可靠区分停牌和
            坏数据，仍会进入质量报告。
          </AlertDescription>
        </Alert>

        <div className="space-y-3">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
            <div className="flex-1 space-y-2">
              <Label htmlFor="release-symbol-search">
                <HintLabel hint={RESEARCH_HINTS.data.cachedSymbols}>
                  从缓存选择标的
                </HintLabel>
              </Label>
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  id="release-symbol-search"
                  value={cacheSearch}
                  onChange={(event) => setCacheSearch(event.target.value)}
                  placeholder="输入代码筛选，例如 510300"
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
                  清空
                </button>
              )}
              <p className="text-sm text-muted-foreground">
                已选 {selectedItems.length} 只
              </p>
            </div>
          </div>

          {selectedItems.length > 0 && (
            <div className="flex flex-wrap gap-2" aria-label="已选发布标的">
              {selectedItems.slice(0, 20).map((item) => (
                <button
                  type="button"
                  key={item.symbol}
                  onClick={() => toggleSymbol(item)}
                  className="inline-flex min-h-9 items-center gap-1.5 rounded-md bg-primary/10 px-2.5 py-1 font-mono text-xs text-primary hover:bg-primary/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  aria-label={`移除 ${item.symbol}`}
                >
                  {item.symbol}
                  <X className="h-3 w-3" />
                </button>
              ))}
              {selectedItems.length > 20 && (
                <Badge variant="secondary" className="min-h-9 px-2.5">
                  另有 {selectedItems.length - 20} 只已选
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
                  error instanceof Error ? error.message : "无法读取本地缓存"
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
                          {formatNumber(item.bar_count, 0)} 根 ·{" "}
                          {item.first_date ?? "—"} 至 {item.last_date ?? "—"}
                          {item.source ? ` · ${item.source}` : " · 来源未记录"}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </div>
            ) : (
              <EmptyState
                icon={<Database className="h-7 w-7" />}
                title={cacheSearch ? "没有匹配的缓存" : "尚无可发布的本地缓存"}
                description={
                  cacheSearch
                    ? "确认代码和复权方式，或换一个关键词。"
                    : "先拉取至少一个标的的日线数据，再返回这里创建发布。"
                }
                action={
                  !cacheSearch ? (
                    <Button type="button" variant="outline" onClick={onGoToFetch}>
                      前往行情拉取
                    </Button>
                  ) : undefined
                }
                className="m-3"
              />
            )}
          </div>
          {duplicateCacheCount > 0 && (
            <Alert variant="warning">
              <AlertTitle>发现重复缓存记录，已按标的去重</AlertTitle>
              <AlertDescription>
                当前接口返回 {duplicateCacheCount} 条重复记录；发布接口只接收标的代码，
                页面已为每个标的保留覆盖更多日期的一条，避免同一标的被重复发布。若需指定来源、周期或复权方式，请先在上方筛选后再选择。
              </AlertDescription>
            </Alert>
          )}
          {cached && cached.total > cached.items.length && (
            <p className="text-xs text-muted-foreground">
              当前显示 {cached.items.length} / {cached.total} 条匹配缓存；“全选筛选结果”
              会选择全部 {cached.total} 条，而不只是当前显示项。
            </p>
          )}
          {selectedItems.length > 500 && (
            <Alert variant="warning">
              <AlertTitle>这是一个大型数据发布</AlertTitle>
              <AlertDescription>
                将逐只冻结并校验 {selectedItems.length} 只标的，处理时间和磁盘占用会明显增加。
                提交后请等待完成，勿重复创建新的发布 ID。
              </AlertDescription>
            </Alert>
          )}
          {selectedItems.length > 0 && !sourcePolicySatisfied && (
            <Alert variant="warning">
              <AlertTitle>来源策略尚未满足</AlertTitle>
              <AlertDescription>
                {releaseKind === "a_share_tushare"
                  ? "A股单源发布只能选择来源为 tushare 的缓存。"
                  : "多资产混合源发布至少需要两种实际数据来源。"}
              </AlertDescription>
            </Alert>
          )}
        </div>

        {publish.isSuccess && (
          <Alert variant="success" aria-live="polite">
            <AlertTitle>数据发布成功</AlertTitle>
            <AlertDescription>
              {publish.data.release_id} 已冻结 {publish.data.symbol_count} 只标的，
              可在下方列表及后续因子/策略页面中引用。
            </AlertDescription>
          </Alert>
        )}
        {publish.isError && (
          <Alert variant="destructive" aria-live="assertive">
            <AlertTitle>
              {missingEtfSymbol ? "需要补齐 ETF 元数据" : "数据发布失败"}
            </AlertTitle>
            <AlertDescription>
              <p>{publishError.summary}</p>
              {publishError.details && (
                <details className="mt-3 rounded-md border border-destructive/30 bg-background/70 p-3 text-foreground">
                  <summary className="cursor-pointer font-medium">
                    查看完整失败明细
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
                  disabled={publish.isPending}
                >
                  重新校验并发布
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
                  前往标的元数据批量同步 ETF 分类
                </Button>
              )}
            </AlertDescription>
          </Alert>
        )}

        <div className="flex justify-end">
          <Button
            type="button"
            onClick={() => publish.mutate()}
            disabled={!canPublish || publish.isPending}
          >
            <Archive />
            {publish.isPending ? "正在冻结并校验…" : "冻结并发布"}
          </Button>
        </div>
      </div>
    </section>
  );
}

function ReleasesTab({
  onGoToFetch,
  onGoToInstruments,
}: {
  onGoToFetch: () => void;
  onGoToInstruments: () => void;
}) {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["dataset-releases", { limit: 50 }],
    queryFn: () => datasetApi.releases({ limit: 50 }),
  });

  return (
    <div>
      <ReleasePublisher
        onGoToFetch={onGoToFetch}
        onGoToInstruments={onGoToInstruments}
        existingReleases={data ?? []}
        releaseNamesReady={!isLoading && !isError}
      />
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <p className="text-sm text-muted-foreground">
            {data ? `共 ${data.length} 条发布记录` : "加载中…"}
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
          刷新
        </Button>
      </div>

      {isLoading ? (
        <LoadingState rows={6} />
      ) : isError ? (
        <ErrorState
          message={error instanceof Error ? error.message : "无法加载数据发布"}
          onRetry={() => refetch()}
        />
      ) : data && data.length > 0 ? (
        <div className="overflow-x-auto rounded-lg border border-border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>数据集</TableHead>
                <TableHead>版本</TableHead>
                <TableHead>数据范围</TableHead>
                <TableHead>周期</TableHead>
                <TableHead>复权</TableHead>
                <TableHead className="text-right">标的数</TableHead>
                <TableHead className="text-right">覆盖率</TableHead>
                <TableHead>质量</TableHead>
                <TableHead>发布 ID</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.map((rel) => (
                <TableRow key={rel.release_id}>
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
                    {rel.start_date} 至 {rel.end_date}
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
      ) : (
        <EmptyState
          icon={<Database className="h-8 w-8" />}
          title="暂无数据发布"
          description="研究数据发布后将在此列出，包含版本、覆盖率与质量状态。"
        />
      )}
    </div>
  );
}

function ManifestsTab() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    // 新版不可变发布登记写入 research_dataset_releases;旧 manifests
    // 表是历史同步管线遗留,在当前发布流程中不会产生记录。
    queryKey: ["dataset-releases", { limit: 50 }],
    queryFn: () => datasetApi.releases({ limit: 50 }),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data ? `共 ${data.length} 个已发布数据版本` : "加载中…"}
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
        <ErrorState
          message={error instanceof Error ? error.message : "无法加载数据集清单"}
          onRetry={() => refetch()}
        />
      ) : data && data.length > 0 ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {data.map((rel) => (
            <Card key={rel.release_id}>
              <CardHeader className="pb-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <CardTitle className="truncate text-base">
                      {rel.dataset_name}
                    </CardTitle>
                    <p className="mt-1 font-mono text-xs text-muted-foreground">
                      {rel.release_id}
                    </p>
                  </div>
                  <Badge variant="outline">{rel.version}</Badge>
                </div>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="flex items-center justify-between">
                  <Badge variant="info" className="font-mono">
                    {rel.source}
                  </Badge>
                  <StatusBadge status={rel.quality_status} />
                </div>

                <div className="grid grid-cols-2 gap-2 text-sm">
                  <div>
                    <p className="text-xs text-muted-foreground">标的数</p>
                    <p className="tabular-nums font-medium">
                      {formatNumber(rel.symbol_count, 0)}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">行数</p>
                    <p className="tabular-nums font-medium">
                      {formatNumber(rel.row_count, 0)}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">数据范围</p>
                    <p className="truncate tabular-nums font-medium">
                      {rel.start_date} 至 {rel.end_date}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">覆盖率</p>
                    <p className="tabular-nums font-medium">
                      {formatPercent(rel.coverage_pct, 1)}
                    </p>
                  </div>
                </div>

                <div>
                  <Progress
                    value={rel.coverage_pct * 100}
                    indicatorClassName={coverageColor(rel.coverage_pct * 100)}
                  />
                </div>
                <p className="font-mono text-xs text-muted-foreground">
                  checksum {rel.release_checksum.slice(0, 12)}
                </p>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={<Package className="h-8 w-8" />}
          title="暂无数据集清单"
          description="数据集清单记录了每个数据集的行数、标的数、覆盖率与质量。"
        />
      )}
    </div>
  );
}

function LifecyclePanel({ symbol }: { symbol: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["instrument-lifecycle", symbol],
    queryFn: () => datasetApi.lifecycle(symbol, { limit: 50 }),
  });

  if (isLoading) {
    return <LoadingState rows={3} />;
  }
  if (isError) {
    return (
      <p className="px-4 py-3 text-sm text-destructive">生命周期事件加载失败</p>
    );
  }
  if (!data || data.length === 0) {
    return (
      <p className="px-4 py-3 text-sm text-muted-foreground">
        该标的无生命周期事件记录。
      </p>
    );
  }

  return (
    <div className="px-4 pb-3">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>事件类型</TableHead>
            <TableHead>日期</TableHead>
            <TableHead>来源 / 版本</TableHead>
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
            placeholder="搜索代码或名称…"
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
            <SelectValue placeholder="市场" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部市场</SelectItem>
            <SelectItem value="a_share">A 股</SelectItem>
            <SelectItem value="hk">港股</SelectItem>
            <SelectItem value="us">美股</SelectItem>
            <SelectItem value="future">期货</SelectItem>
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
            <SelectValue placeholder="类型" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部类型</SelectItem>
            <SelectItem value="stock">股票</SelectItem>
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
            <SelectValue placeholder="状态" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部状态</SelectItem>
            <SelectItem value="active">正常</SelectItem>
            <SelectItem value="suspended">停牌</SelectItem>
            <SelectItem value="pending_delist">待确认退市</SelectItem>
            <SelectItem value="delisted">已退市</SelectItem>
          </SelectContent>
        </Select>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          刷新
        </Button>
        <span className="text-sm text-muted-foreground">
          {data
            ? `显示 ${page * pageSize + 1}-${Math.min((page + 1) * pageSize, data.total)} / ${data.total} 只标的`
            : ""}
        </span>
      </div>

      {instrumentSummary && (
        <div className="mb-4 space-y-3 rounded-lg border border-border bg-card p-4">
          <div className="flex items-center gap-1.5">
            <h3 className="text-sm font-semibold">标的字典概览</h3>
            <ResearchHint hint={RESEARCH_HINTS.data.instrumentMetadata} />
          </div>
          <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
            <div>
              <span className="text-muted-foreground">全部标的</span>
              <span className="ml-2 font-semibold tabular-nums">{instrumentSummary.total}</span>
            </div>
            <div>
              <span className="text-muted-foreground">当前活跃</span>
              <span className="ml-2 font-semibold tabular-nums">{instrumentSummary.active_total}</span>
            </div>
            {Object.entries(instrumentSummary.by_instrument_type).map(([key, count]) => (
              <div key={key}>
                <span className="text-muted-foreground">{INSTRUMENT_TYPE_LABELS[key] ?? key}</span>
                <span className="ml-2 font-semibold tabular-nums">{count}</span>
              </div>
            ))}
          </div>
          <div className="grid gap-3 border-t border-border pt-3 text-xs sm:grid-cols-3">
            <div>
              <p className="mb-1 font-medium text-muted-foreground">按状态</p>
              <div className="flex flex-wrap gap-x-3 gap-y-1">
                {Object.entries(instrumentSummary.by_status)
                  .sort(([a], [b]) => {
                    const ai = STATUS_ORDER.indexOf(a);
                    const bi = STATUS_ORDER.indexOf(b);
                    return (ai === -1 ? STATUS_ORDER.length : ai) - (bi === -1 ? STATUS_ORDER.length : bi);
                  })
                  .map(([key, count]) => (
                    <span key={key} className="text-muted-foreground">
                      {INSTRUMENT_STATUS_LABELS[key] ?? key} <span className="font-medium tabular-nums text-foreground">{count}</span>
                    </span>
                  ))}
              </div>
            </div>
            <div>
              <p className="mb-1 font-medium text-muted-foreground">按市场</p>
              <div className="flex flex-wrap gap-x-3 gap-y-1">
                {Object.entries(instrumentSummary.by_market).map(([key, count]) => (
                  <span key={key} className="text-muted-foreground">
                    {MARKET_LABELS[key] ?? key} <span className="font-medium tabular-nums text-foreground">{count}</span>
                  </span>
                ))}
              </div>
            </div>
            <div>
              <p className="mb-1 font-medium text-muted-foreground">ETF 口径</p>
              <p className="text-muted-foreground">
                当前活跃 ETF <span className="font-medium tabular-nums text-foreground">{instrumentSummary.active_etf_total}</span> 只
              </p>
            </div>
          </div>
        </div>
      )}

      {instrumentSummary && etfSummary && (
        <Alert className="mb-4 border-primary/25 bg-primary/5">
          <ResearchHint hint={RESEARCH_HINTS.data.instrumentCounts} className="mt-0.5 text-primary" />
          <AlertTitle>ETF 数量有两套统计口径</AlertTitle>
          <AlertDescription>
            当前活跃标的池有 {instrumentSummary.active_etf_total} 只 ETF，ETF 分类元数据目录有 {etfSummary.total} 条记录。
            后者来自基金目录同步，会保留历史、已退市或暂未进入当前标的池的记录；行情拉取和数据发布以标的字典中的活跃标的为准。
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
          <AlertTitle>元数据尚未覆盖现有行情缓存</AlertTitle>
          <AlertDescription className="flex flex-col items-start justify-between gap-3 sm:flex-row sm:items-center">
            <p>
              当前数据库只有 {data.total} 只匹配的活跃标的元数据，但本地已有{" "}
              {cacheStats.total} 条行情缓存。两者独立存储；缓存文件不会自动生成名称、
              市场、上市状态和生命周期资料。
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="shrink-0"
              onClick={onGoToFetch}
            >
              前往同步标的池
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {isLoading ? (
        <LoadingState rows={8} />
      ) : isError ? (
        <ErrorState
          message={error instanceof Error ? error.message : "无法加载标的元数据"}
          onRetry={() => refetch()}
        />
      ) : items.length > 0 ? (
        <div className="overflow-x-auto rounded-lg border border-border">
          <ScrollArea className="max-h-[600px]">
            <Table>
              <TableHeader className="sticky top-0 bg-card">
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>代码</TableHead>
                  <TableHead>名称</TableHead>
                  <TableHead>市场</TableHead>
                  <TableHead>类型</TableHead>
                  <TableHead>上市日期</TableHead>
                  <TableHead>状态</TableHead>
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
                          {MARKET_LABELS[inst.market] ?? inst.market}
                        </TableCell>
                        <TableCell className="text-muted-foreground">
                          {INSTRUMENT_TYPE_LABELS[inst.instrument_type] ?? inst.instrument_type}
                        </TableCell>
                        <TableCell className="tabular-nums text-muted-foreground">
                          {inst.list_date ?? "—"}
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center gap-1">
                            {inst.status === "suspended" ? (
                              <Badge variant="destructive">停牌</Badge>
                            ) : inst.status === "pending_delist" ? (
                              <Badge variant="warning">待确认退市</Badge>
                            ) : inst.status === "active" ? (
                              <Badge variant="success">正常</Badge>
                            ) : (
                              <StatusBadge status={INSTRUMENT_STATUS_LABELS[inst.status] ?? inst.status} />
                            )}
                          </div>
                        </TableCell>
                      </TableRow>
                      {isOpen && (
                        <TableRow key={`${inst.code}-detail`}>
                          <TableCell colSpan={7} className="bg-muted/30 p-0">
                            <div className="grid grid-cols-2 gap-x-6 gap-y-2 border-b border-border px-4 py-3 text-xs md:grid-cols-4">
                              <div>
                                <span className="text-muted-foreground">市场 / 类型</span>
                                <p className="mt-0.5 font-medium">
                                  {MARKET_LABELS[inst.market] ?? inst.market} · {INSTRUMENT_TYPE_LABELS[inst.instrument_type] ?? inst.instrument_type}
                                </p>
                              </div>
                              <div>
                                <span className="text-muted-foreground">状态</span>
                                <p className="mt-0.5 font-medium">{INSTRUMENT_STATUS_LABELS[inst.status] ?? inst.status}</p>
                              </div>
                              <div>
                                <span className="text-muted-foreground">交易所</span>
                                <p className="mt-0.5 font-medium">{inst.exchange ?? "—"}</p>
                              </div>
                              <div>
                                <span className="text-muted-foreground">行业 / 板块</span>
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
          title={search || market !== "all" || instrumentType !== "all" || status !== "all" ? "无匹配标的" : "暂无标的元数据"}
          description={
            search || market !== "all" || instrumentType !== "all" || status !== "all"
              ? "尝试调整搜索关键词或市场筛选条件。"
              : "先在行情拉取页同步标的池，名称、市场、类型和上市状态才会写入数据库。"
          }
          action={
            !search && market === "all" && instrumentType === "all" && status === "all" ? (
              <Button type="button" variant="outline" onClick={onGoToFetch}>
                前往同步标的池
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
            上一页
          </Button>
          <span className="text-sm text-muted-foreground tabular-nums">
            第 {page + 1} / {Math.ceil(data.total / pageSize)} 页
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={
              (page + 1) * pageSize >= data.total || isFetching
            }
            onClick={() => setPage((p) => p + 1)}
          >
            下一页
          </Button>
        </div>
      )}
    </div>
  );
}

export default function ResearchData() {
  const [activeTab, setActiveTab] = useState("fetch");
  const activeTabHint = {
    fetch: RESEARCH_HINTS.data.fetch,
    releases: RESEARCH_HINTS.data.releases,
    manifests: RESEARCH_HINTS.data.manifests,
    instruments: RESEARCH_HINTS.data.instrumentMetadata,
  }[activeTab];

  return (
    <div>
      <PageHeader
        title="数据与标的"
        description="行情数据拉取、研究数据发布、数据集清单与标的元数据"
      />
      <WorkflowIndicator currentPath="/research/data" />

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList className="h-auto max-w-full justify-start overflow-x-auto">
          <TabsTrigger value="fetch">
            <HardDriveDownload className="mr-1.5 h-4 w-4" />
            行情拉取
          </TabsTrigger>
          <TabsTrigger value="releases">数据发布</TabsTrigger>
          <TabsTrigger value="manifests">数据集清单</TabsTrigger>
          <TabsTrigger value="instruments">标的元数据</TabsTrigger>
        </TabsList>
        {activeTabHint && (
          <div className="mt-3 flex max-w-3xl items-start gap-2 text-sm text-muted-foreground">
            <ResearchHint hint={activeTabHint} className="mt-0.5 shrink-0" />
            <p>{activeTabHint.description}</p>
          </div>
        )}

        <TabsContent value="fetch">
          <Suspense fallback={<LoadingState rows={5} />}>
            <MarketDataTab />
          </Suspense>
        </TabsContent>
        <TabsContent value="releases">
          <ReleasesTab
            onGoToFetch={() => setActiveTab("fetch")}
            onGoToInstruments={() => setActiveTab("instruments")}
          />
        </TabsContent>
        <TabsContent value="manifests">
          <ManifestsTab />
        </TabsContent>
        <TabsContent value="instruments">
          <InstrumentsTab onGoToFetch={() => setActiveTab("fetch")} />
        </TabsContent>
      </Tabs>

      <NextStepCTA
        nextPath="/research/factors"
        nextLabel="因子实验室"
        description="基于已拉取的数据探索因子、创建因子实验"
      />
    </div>
  );
}
