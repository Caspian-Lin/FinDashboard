import { Fragment, useEffect, useState, lazy, Suspense } from "react";
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
  type EtfCategory,
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

const ETF_CATEGORIES: {
  value: EtfCategory;
  label: string;
  execution: string;
}[] = [
  { value: "index", label: "指数 ETF", execution: "A 股宽基/行业指数，T+1" },
  { value: "equity", label: "股票 ETF", execution: "主动或非指数股票组合，T+1" },
  { value: "cross_border", label: "跨境 ETF", execution: "境外资产，T+0" },
  { value: "bond", label: "债券 ETF", execution: "固定收益资产，10 份/手" },
  { value: "money_market", label: "货币 ETF", execution: "现金管理类，T+0" },
  { value: "commodity", label: "商品 ETF", execution: "黄金/商品资产，T+0" },
];

function EtfMetadataEditor({
  symbol,
  onSaved,
}: {
  symbol: string;
  onSaved?: () => void;
}) {
  const queryClient = useQueryClient();
  const [category, setCategory] = useState<EtfCategory | "">("");
  const [underlyingIndex, setUnderlyingIndex] = useState("");
  const { data, isLoading, isError } = useQuery({
    queryKey: ["etf-metadata", symbol],
    queryFn: () => datasetApi.etfMetadata(symbol),
  });

  useEffect(() => {
    if (!data) return;
    setCategory(data.category);
    setUnderlyingIndex(data.underlying_index ?? "");
  }, [data]);

  const save = useMutation({
    mutationFn: () => {
      if (!category) {
        throw new Error("请选择 ETF 分类");
      }
      return datasetApi.updateEtfClassification(symbol, {
        category,
        underlying_index: underlyingIndex.trim() || null,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["etf-metadata", symbol] });
      queryClient.invalidateQueries({ queryKey: ["research-instruments"] });
      onSaved?.();
    },
  });

  const selectedCategory = ETF_CATEGORIES.find((item) => item.value === category);

  return (
    <div className="space-y-3 px-4 py-4">
      <div>
        <h3 className="text-sm font-semibold">ETF 研究分类</h3>
        <p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
          分类决定回测中的资产大类、交收周期和手数规则。请根据基金合同或交易所资料确认，
          保存不会启动研究运行，也不会修改实盘配置。
        </p>
      </div>
      {isLoading ? (
        <LoadingState rows={2} />
      ) : isError ? (
        <p className="text-sm text-destructive">ETF 元数据加载失败，请重试。</p>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-[minmax(220px,0.8fr)_minmax(240px,1fr)_auto] md:items-end">
            <div className="space-y-2">
              <Label htmlFor={`etf-category-${symbol}`}>ETF 分类</Label>
              <Select
                value={category}
                onValueChange={(value) => setCategory(value as EtfCategory)}
              >
                <SelectTrigger id={`etf-category-${symbol}`}>
                  <SelectValue placeholder="请选择分类" />
                </SelectTrigger>
                <SelectContent>
                  {ETF_CATEGORIES.map((item) => (
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
            <Button
              type="button"
              onClick={() => save.mutate()}
              disabled={!category || save.isPending}
            >
              {save.isPending ? "保存中…" : data ? "保存修改" : "补齐元数据"}
            </Button>
          </div>
          {selectedCategory && (
            <p className="text-xs text-muted-foreground">
              将按“{selectedCategory.label}”处理：{selectedCategory.execution}。
            </p>
          )}
          {save.isSuccess && (
            <Alert variant="success" aria-live="polite">
              <AlertTitle>ETF 元数据已保存</AlertTitle>
              <AlertDescription>
                {symbol} 已归类为 {selectedCategory?.label}，现在可以重新校验数据发布。
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

function initialReleaseNames() {
  const stamp = new Date().toISOString().slice(0, 10);
  const compact = stamp.replaceAll("-", "");
  return {
    version: `${stamp}-v1`,
    releaseId: `daily-bars-${compact}-v1`,
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

function ReleasePublisher({ onGoToFetch }: { onGoToFetch: () => void }) {
  const queryClient = useQueryClient();
  const defaults = initialReleaseNames();
  const [isOpen, setIsOpen] = useState(false);
  const [cacheSearch, setCacheSearch] = useState("");
  const [datasetName, setDatasetName] = useState("multi_asset_daily_bars");
  const [releaseId, setReleaseId] = useState(defaults.releaseId);
  const [version, setVersion] = useState(defaults.version);
  const [source, setSource] = useState<DatasetReleaseCreate["source"]>("akshare");
  const [adjustment, setAdjustment] =
    useState<DatasetReleaseCreate["adjustment"]>("qfq");
  const [scope, setScope] = useState<"focused" | "multi_asset">("focused");
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
  const [repairedEtf, setRepairedEtf] = useState<string | null>(null);
  const toggleSymbol = (item: CachedDataStatus) => {
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
        source,
        version: version.trim(),
        symbols: selectedItems.map((item) => item.symbol),
        start_date: startDate,
        end_date: endDate,
        adjustment,
        required_capabilities:
          scope === "multi_asset" ? MULTI_ASSET_CAPABILITIES : [],
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["dataset-releases"] });
      queryClient.invalidateQueries({ queryKey: ["dataset-manifests"] });
    },
  });

  const canPublish =
    releaseId.trim().length >= 3 &&
    datasetName.trim().length >= 3 &&
    version.trim().length > 0 &&
    selectedItems.length > 0 &&
    startDate !== "" &&
    endDate !== "" &&
    startDate <= endDate;
  const missingEtfSymbol =
    publish.isError && publish.error instanceof Error
      ? publish.error.message.match(
          /([A-Z0-9]+(?:\.[A-Z]+)?): ETF 缺少分类元数据/,
        )?.[1]
      : undefined;

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
            onClick={() => setIsOpen(true)}
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
            <Label htmlFor="release-source">
              <HintLabel hint={RESEARCH_HINTS.data.source}>数据来源</HintLabel>
            </Label>
            <Select
              value={source}
              onValueChange={(value) =>
                setSource(value as DatasetReleaseCreate["source"])
              }
            >
              <SelectTrigger id="release-source">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="akshare">akshare</SelectItem>
                <SelectItem value="yfinance">yfinance</SelectItem>
                <SelectItem value="tushare">tushare</SelectItem>
                <SelectItem value="manual">人工导入</SelectItem>
              </SelectContent>
            </Select>
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
          <div className="space-y-2">
            <Label htmlFor="release-scope">
              <HintLabel hint={RESEARCH_HINTS.data.releaseScope}>质量门范围</HintLabel>
            </Label>
            <Select
              value={scope}
              onValueChange={(value) =>
                setScope(value as "focused" | "multi_asset")
              }
            >
              <SelectTrigger id="release-scope">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="focused">选定标的研究</SelectItem>
                <SelectItem value="multi_asset">正式多资产研究</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        {scope === "multi_asset" && (
          <Alert variant="warning">
            <AlertTitle>多资产质量门会严格失败关闭</AlertTitle>
            <AlertDescription>
              必须同时包含股票、宽基/指数、跨境、商品和债券 ETF，且每个标的的元数据、
              可交易生命周期内的日期覆盖和质量检查均通过，否则不会生成发布记录。
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
            ) : cached && cached.items.length > 0 ? (
              <div className="divide-y divide-border">
                {cached.items.map((item) => {
                  const checkboxId = `release-symbol-${item.symbol.replaceAll(".", "-")}`;
                  return (
                    <label
                      key={`${item.symbol}-${item.period}-${item.adjust}`}
                      htmlFor={checkboxId}
                      className="flex min-h-11 cursor-pointer items-center gap-3 px-3 py-2 hover:bg-accent"
                    >
                      <Checkbox
                        id={checkboxId}
                        checked={Boolean(selected[item.symbol])}
                        onCheckedChange={() => toggleSymbol(item)}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="font-mono text-sm font-medium">
                          {item.symbol}
                        </span>
                        <span className="ml-3 text-xs text-muted-foreground">
                          {formatNumber(item.bar_count, 0)} 根 ·{" "}
                          {item.first_date ?? "—"} 至 {item.last_date ?? "—"}
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
              <p>
                {publish.error instanceof Error
                  ? publish.error.message
                  : "请检查缓存范围与质量门配置"}
              </p>
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

function ReleasesTab({ onGoToFetch }: { onGoToFetch: () => void }) {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["dataset-releases", { limit: 50 }],
    queryFn: () => datasetApi.releases({ limit: 50 }),
  });

  return (
    <div>
      <ReleasePublisher onGoToFetch={onGoToFetch} />
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
                    {formatPercent(rel.coverage_pct / 100, 1)}
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
    queryKey: ["dataset-manifests", { limit: 50 }],
    queryFn: () => datasetApi.manifests({ limit: 50 }),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data ? `共 ${data.length} 个数据集` : "加载中…"}
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
          {data.map((m) => (
            <Card key={`${m.dataset_name}-${m.version}`}>
              <CardHeader className="pb-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <CardTitle className="truncate text-base">
                      {m.dataset_name}
                    </CardTitle>
                    <p className="mt-1 font-mono text-xs text-muted-foreground">
                      {m.checksum.slice(0, 12)}
                    </p>
                  </div>
                  <Badge variant="outline">{m.version}</Badge>
                </div>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="flex items-center justify-between">
                  <Badge variant="info" className="font-mono">
                    {m.source}
                  </Badge>
                  <StatusBadge status={m.quality_status} />
                </div>

                <div className="grid grid-cols-2 gap-2 text-sm">
                  <div>
                    <p className="text-xs text-muted-foreground">标的数</p>
                    <p className="tabular-nums font-medium">
                      {formatNumber(m.symbol_count, 0)}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">行数</p>
                    <p className="tabular-nums font-medium">
                      {formatNumber(m.row_count, 0)}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">数据缺口</p>
                    <p className="tabular-nums font-medium">{m.gaps.length}</p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">覆盖率</p>
                    <p className="tabular-nums font-medium">
                      {formatPercent(m.coverage_pct / 100, 1)}
                    </p>
                  </div>
                </div>

                <div>
                  <Progress
                    value={m.coverage_pct}
                    indicatorClassName={coverageColor(m.coverage_pct)}
                  />
                </div>
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
  const [expandedRow, setExpandedRow] = useState<string | null>(null);

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["research-instruments", { search, market, limit: 200 }],
    queryFn: () =>
      datasetApi.instruments({
        q: search.trim() || undefined,
        market: market === "all" ? undefined : market,
        limit: 200,
      }),
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
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
          />
        </div>
        <Select value={market} onValueChange={setMarket}>
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
          {data ? `显示 ${items.length} / ${data.total} 只活跃标的` : ""}
        </span>
      </div>

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
                  <TableHead>标记</TableHead>
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
                          {inst.market}
                        </TableCell>
                        <TableCell className="text-muted-foreground">
                          {inst.instrument_type}
                        </TableCell>
                        <TableCell className="tabular-nums text-muted-foreground">
                          {inst.list_date ?? "—"}
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center gap-1">
                            {inst.status === "suspended" && (
                              <Badge variant="destructive">停牌</Badge>
                            )}
                            {inst.status === "pending_delist" && (
                              <Badge variant="warning">待确认退市</Badge>
                            )}
                            {inst.status === "active" && (
                              <Badge variant="success">正常</Badge>
                            )}
                            {!["active", "suspended", "pending_delist"].includes(
                              inst.status,
                            ) && <StatusBadge status={inst.status} />}
                          </div>
                        </TableCell>
                      </TableRow>
                      {isOpen && (
                        <TableRow key={`${inst.code}-detail`}>
                          <TableCell colSpan={7} className="bg-muted/30 p-0">
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
          title={search || market !== "all" ? "无匹配标的" : "暂无标的元数据"}
          description={
            search || market !== "all"
              ? "尝试调整搜索关键词或市场筛选条件。"
              : "先在行情拉取页同步标的池，名称、市场、类型和上市状态才会写入数据库。"
          }
          action={
            !search && market === "all" ? (
              <Button type="button" variant="outline" onClick={onGoToFetch}>
                前往同步标的池
              </Button>
            ) : undefined
          }
        />
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
          <ReleasesTab onGoToFetch={() => setActiveTab("fetch")} />
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
