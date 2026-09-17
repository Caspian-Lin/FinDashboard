import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "../components/ui/dialog";
import InfoHint, { HintLabel } from "../components/InfoHint";
import { api } from "../lib/api";
import type { JobOut, QualityReport } from "../lib/api";
import { isJobRunning } from "../lib/api";
import { INFO_HINTS, type InfoHintDefinition } from "../lib/infoHints";
import { cn } from "../lib/utils";
import { useT, useLanguage, type LocalizedText } from "@/i18n";

const BULK_PHASE_LABELS: Record<string, LocalizedText> = {
  starting: { zh: "准备任务", en: "Starting job" },
  checking_cache: { zh: "检查缓存", en: "Checking cache" },
  cache_hit: { zh: "缓存命中", en: "Cache hit" },
  fetching: { zh: "请求行情", en: "Fetching bars" },
  reading_cache: { zh: "读取缓存", en: "Reading cache" },
  writing_cache: { zh: "写入缓存", en: "Writing cache" },
};

function formatDuration(sec: number, lang: "zh" | "en"): string {
  if (!isFinite(sec) || sec <= 0) return "—";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  if (lang === "en") {
    if (h > 0) return `${h}h ${m}m ${s}s`;
    return `${m}m ${s}s`;
  }
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export default function Data({ embedded = false }: { embedded?: boolean }) {
  const { tl } = useT();
  const { lang } = useLanguage();
  const queryClient = useQueryClient();
  // 缓存预览对话框选中的标的(null=关闭)
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null);
  const [fetchSymbol, setFetchSymbol] = useState("000001.SZ");
  const [fetchStart, setFetchStart] = useState("2024-01-01");
  const [fetchEnd, setFetchEnd] = useState(
    new Date().toISOString().slice(0, 10),
  );
  const [fetchSource, setFetchSource] = useState("");
  const [marketFilter, setMarketFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [instPage, setInstPage] = useState(1);
  const [cachePage, setCachePage] = useState(1);

  // bulk download form
  const [dlMarket, setDlMarket] = useState("a_share");
  const [dlType, setDlType] = useState("stock");
  const [dlBoard, setDlBoard] = useState("");
  const [dlStart, setDlStart] = useState("2015-01-01");
  const [dlSource, setDlSource] = useState("");
  const [showQualityDetail, setShowQuality] = useState(false);
  const [repairSource, setRepairSource] = useState<"akshare" | "yfinance" | "tushare">("tushare");
  const [nowTick, setNowTick] = useState(() => Date.now());

  // 统一任务队列(#144):批量拉取/同步/修复都 enqueue 一个 BJ- 任务,
  // 前端轮询 /api/jobs/{job_id} 直到终态再读 progress_done/total/phase/result_ref。
  const [bulkJobId, setBulkJobId] = useState<string | null>(null);
  const [syncJobId, setSyncJobId] = useState<string | null>(null);
  const [repairJobId, setRepairJobId] = useState<string | null>(null);

  const PAGE_SIZE = 10;
  const isSearching = searchQuery.length >= 2;

  const { data: status } = useQuery({
    queryKey: ["data-status", cachePage],
    queryFn: () =>
      api.getDataStatusPage(PAGE_SIZE, (cachePage - 1) * PAGE_SIZE),
  });

  const { data: instruments } = useQuery({
    queryKey: ["instruments", marketFilter, typeFilter, instPage],
    queryFn: () =>
      api.getInstruments({
        market: marketFilter || undefined,
        instrument_type: typeFilter || undefined,
        limit: PAGE_SIZE,
        offset: (instPage - 1) * PAGE_SIZE,
      }),
    enabled: !isSearching,
  });
  const { data: databaseUniverse } = useQuery({
    queryKey: ["instrument-count", "all"],
    queryFn: () => api.getInstruments({ limit: 1 }),
  });
  const { data: aShareStocks } = useQuery({
    queryKey: ["instrument-count", "a_share", "stock"],
    queryFn: () =>
      api.getInstruments({
        market: "a_share",
        instrument_type: "stock",
        limit: 1,
      }),
  });
  const { data: etfs } = useQuery({
    queryKey: ["instrument-count", "a_share", "etf"],
    queryFn: () =>
      api.getInstruments({
        market: "a_share",
        instrument_type: "etf",
        limit: 1,
      }),
  });

  const { data: searchResults } = useQuery({
    queryKey: ["instrument-search", searchQuery],
    queryFn: () => api.searchInstruments(searchQuery),
    enabled: isSearching,
  });

  const { data: bulkJob } = useQuery({
    queryKey: ["job", bulkJobId],
    queryFn: () => api.getJob(bulkJobId as string),
    enabled: Boolean(bulkJobId),
    refetchInterval: (query) => (isJobRunning(query.state.data) ? 2000 : false),
  });

  const { data: syncJob } = useQuery({
    queryKey: ["job", syncJobId],
    queryFn: () => api.getJob(syncJobId as string),
    enabled: Boolean(syncJobId),
    refetchInterval: (query) => (isJobRunning(query.state.data) ? 2000 : false),
  });

  const { data: repairJob } = useQuery({
    queryKey: ["job", repairJobId],
    queryFn: () => api.getJob(repairJobId as string),
    enabled: Boolean(repairJobId),
    refetchInterval: (query) => (isJobRunning(query.state.data) ? 2000 : false),
  });

  const { data: qualityReports, refetch: refetchQuality, isFetching: qualityFetching } = useQuery({
    queryKey: ["quality-reports"],
    queryFn: () => api.checkQuality(),
    enabled: false,
  });

  // 同步 / 修复任务到达终态后失效相关缓存查询,使列表刷新。
  useEffect(() => {
    if (syncJob && !isJobRunning(syncJob) && syncJob.status !== undefined) {
      queryClient.invalidateQueries({ queryKey: ["instruments"] });
      queryClient.invalidateQueries({ queryKey: ["instrument-count"] });
      queryClient.invalidateQueries({ queryKey: ["research-instruments"] });
    }
  }, [syncJob, queryClient]);

  useEffect(() => {
    if (repairJob && !isJobRunning(repairJob) && repairJob.status !== undefined) {
      refetchQuality();
      queryClient.invalidateQueries({ queryKey: ["data-status"] });
    }
  }, [repairJob, queryClient, refetchQuality]);

  const { data: config } = useQuery({
    queryKey: ["scheduler-config"],
    queryFn: api.getConfig,
  });
  const defaultProvider = config?.data_provider ?? "akshare";
  const effectiveBulkProvider = dlSource || defaultProvider;
  const tushareBulk = effectiveBulkProvider === "tushare";

  const { data: tushareQuota, isLoading: tushareQuotaLoading } = useQuery({
    queryKey: ["tushare-quota"],
    queryFn: api.getTushareQuota,
    enabled: tushareBulk,
    refetchInterval: tushareBulk ? 5000 : false,
  });

  const sync = useMutation({
    mutationFn: () => api.syncUniverse(),
    onSuccess: (job) => setSyncJobId(job.job_id),
  });

  const fetchOne = useMutation({
    mutationFn: (body: {
      symbol: string;
      start: string;
      end: string;
      adjust?: string;
      source?: string;
    }) => api.fetchData(body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["data-status"] }),
  });

  const startDownload = useMutation({
    mutationFn: (params?: { source?: string }) =>
      api.startBulkDownload({
        market: dlMarket,
        instrument_type: dlType || undefined,
        listing_boards: dlBoard ? [dlBoard] : undefined,
        start: dlStart,
        source: params?.source,
      }),
    onSuccess: (job) => {
      setBulkJobId(job.job_id);
      if (tushareBulk) {
        queryClient.invalidateQueries({ queryKey: ["tushare-quota"] });
      }
    },
  });

  const repairQuality = useMutation({
    mutationFn: () =>
      api.repairQuality({
        symbols: (qualityReports ?? []).filter((report) => !report.passed).map((report) => report.symbol),
        source: repairSource,
      }),
    onSuccess: (job) => setRepairJobId(job.job_id),
  });

  const totalInstruments = databaseUniverse?.total ?? 0;
  const isDownloading = startDownload.isPending || isJobRunning(bulkJob);
  const phaseLabel = bulkJob?.phase
    ? BULK_PHASE_LABELS[bulkJob.phase]
      ? tl(BULK_PHASE_LABELS[bulkJob.phase])
      : bulkJob.phase
    : undefined;

  useEffect(() => {
    if (!isDownloading) return;
    const id = setInterval(() => setNowTick(Date.now()), 1000);
    return () => clearInterval(id);
  }, [isDownloading]);

  const startedAtMs = bulkJob?.started_at ? Date.parse(bulkJob.started_at) : 0;
  const elapsedSec = startedAtMs > 0 ? Math.max(1, (nowTick - startedAtMs) / 1000) : 0;
  const bulkDone = bulkJob?.progress_done ?? 0;
  const bulkTotal = bulkJob?.progress_total ?? 0;
  const bulkSpeed = elapsedSec > 0 ? (bulkDone / elapsedSec) * 60 : 0;
  const bulkEtaSec = bulkSpeed > 0 ? ((bulkTotal - bulkDone) / bulkSpeed) * 60 : 0;

  useEffect(() => {
    setInstPage(1);
  }, [searchQuery, marketFilter, typeFilter]);

  // 搜索模式:前端分页搜索结果;非搜索模式:直接用服务端返回的当前页
  const searchAll = searchResults ?? [];
  const searchTotalPages = Math.max(1, Math.ceil(searchAll.length / PAGE_SIZE));
  const pagedInstruments = isSearching
    ? searchAll.slice((instPage - 1) * PAGE_SIZE, instPage * PAGE_SIZE)
    : (instruments?.items ?? []);
  const listedInstrumentTotal = isSearching
    ? searchAll.length
    : (instruments?.total ?? 0);
  const instTotalPages = isSearching
    ? searchTotalPages
    : Math.max(1, Math.ceil(listedInstrumentTotal / PAGE_SIZE));

  const cacheTotal = status?.total ?? 0;
  const cacheTotalPages = Math.max(1, Math.ceil(cacheTotal / PAGE_SIZE));
  const tushareQuotaPercent = tushareQuota
    ? Math.min(100, (tushareQuota.used / Math.max(1, tushareQuota.daily_limit)) * 100)
    : 0;

  return (
    <div className="mx-auto w-full max-w-7xl space-y-6">
      {!embedded && (
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="text-2xl font-bold tracking-tight text-foreground">{tl({ zh: "行情数据", en: "Market Data" })}</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {tl({
                zh: "标的池、单标的拉取与缓存管理;耗时任务进入统一队列,可在「任务中心」跟踪。",
                en: "Universe, single-symbol fetch and cache management; long-running jobs enter the unified queue and can be tracked in the Task Center.",
              })}
            </p>
          </div>
        </div>
      )}

      {/* Stats row */}
      <div>
        <p className="mb-2 text-xs text-muted-foreground">
          {tl({
            zh: "数据库标的数 / A股 / ETF 来自标的字典元数据;缓存标的数是本地行情缓存条数——两者独立存储,缓存不会自动生成名称、市场与上市状态。",
            en: "Instruments in DB / A-shares / ETFs come from instrument dictionary metadata, while Cached symbols counts local bar cache entries. They are stored independently: cache files do not gain name, market or listing status.",
          })}
        </p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <StatCard
            label={tl({ zh: "数据库标的数", en: "Instruments in DB" })}
            value={String(totalInstruments)}
            hint={INFO_HINTS.data.databaseUniverse}
          />
          <StatCard
            label={tl({ zh: "缓存标的数", en: "Cached symbols" })}
            value={String(status?.total ?? 0)}
            hint={INFO_HINTS.data.statsScope}
          />
          <StatCard label={tl({ zh: "A股", en: "A-shares" })} value={String(aShareStocks?.total ?? 0)} />
          <StatCard label="ETF" value={String(etfs?.total ?? 0)} />
        </div>
      </div>

      {/* Sync + Single fetch */}
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        {/* Universe Sync */}
        <div className="rounded-lg border border-border bg-card p-5">
          <div className="flex items-center justify-between mb-3">
            <h2 className="flex items-center gap-1 text-lg font-semibold">
              {tl({ zh: "标的池同步", en: "Universe Sync" })}
              <InfoHint content={INFO_HINTS.data.universeSync} />
              <span className="ml-2 rounded-md bg-primary/15 px-2 py-0.5 text-xs font-medium text-primary">
                akshare
              </span>
            </h2>
            {totalInstruments > 0 && (
              <span className="text-sm text-muted-foreground/70">
                {tl({ zh: `已同步 ${totalInstruments} 条`, en: `${totalInstruments} synced` })}
              </span>
            )}
          </div>
          <p className="text-sm text-muted-foreground mb-3">
            {tl({
              zh: "从 akshare 自动发现全市场 A 股(~5500) + ETF(~1600),写入数据库。",
              en: "Auto-discover all A-share (~5,500) and ETF (~1,600) instruments from akshare and write them to the database.",
            })}
          </p>
          <button
            type="button"
            onClick={() => sync.mutate()}
            disabled={sync.isPending}
            className="w-full rounded-md bg-primary text-primary-foreground py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
          >
            {sync.isPending
              ? tl({ zh: "同步中...", en: "Syncing..." })
              : totalInstruments === 0
                ? tl({ zh: "同步标的池", en: "Sync universe" })
                : tl({ zh: "刷新标的池", en: "Refresh universe" })}
          </button>
          {sync.isPending && (
            <p className="text-sm text-muted-foreground mt-2">
              {tl({ zh: "已提交同步任务,等待队列调度…", en: "Sync job submitted, waiting for the queue…" })}
            </p>
          )}
          {syncJob && !isJobRunning(syncJob) && syncJob.status === "succeeded" && (
            <p className="text-sm text-success mt-2">{tl({ zh: "标的池同步完成", en: "Universe sync completed" })}</p>
          )}
          {syncJob && !isJobRunning(syncJob) && syncJob.status !== "succeeded" && (
            <p className="text-sm text-destructive mt-2">
              {tl({ zh: "同步失败: ", en: "Sync failed: " })}
              {syncJob.error_summary ?? syncJob.status}
            </p>
          )}
          {sync.error && (
            <p className="text-sm text-destructive mt-2">
              {(sync.error as Error).message}
            </p>
          )}
        </div>

        {/* Single fetch */}
        <div className="rounded-lg border border-border bg-card p-5">
          <h2 className="text-lg font-semibold mb-3">{tl({ zh: "单标的拉取", en: "Single-Symbol Fetch" })}</h2>
          <div className="space-y-3">
            <div>
              <HintLabel htmlFor="data-symbol" hint={INFO_HINTS.data.symbol}>
                {tl({ zh: "标的", en: "Symbol" })}
              </HintLabel>
              <input
                id="data-symbol"
                value={fetchSymbol}
                onChange={(e) => setFetchSymbol(e.target.value)}
                className="w-full border border-input bg-card text-foreground rounded px-3 py-2 text-sm font-mono"
                placeholder="510300.SH"
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <HintLabel htmlFor="data-fetch-start" hint={INFO_HINTS.data.dateRange}>
                  {tl({ zh: "开始", en: "Start" })}
                </HintLabel>
                <input
                  id="data-fetch-start"
                  type="date"
                  value={fetchStart}
                  onChange={(e) => setFetchStart(e.target.value)}
                  className="w-full border border-input bg-card text-foreground rounded px-3 py-2 text-sm"
                />
              </div>
              <div>
                <HintLabel htmlFor="data-fetch-end" hint={INFO_HINTS.data.dateRange}>
                  {tl({ zh: "结束", en: "End" })}
                </HintLabel>
                <input
                  id="data-fetch-end"
                  type="date"
                  value={fetchEnd}
                  onChange={(e) => setFetchEnd(e.target.value)}
                  className="w-full border border-input bg-card text-foreground rounded px-3 py-2 text-sm"
                />
              </div>
            </div>
            <div>
              <label htmlFor="data-fetch-source" className="block text-sm font-medium text-foreground mb-1">
                {tl({ zh: "数据源", en: "Data provider" })}
              </label>
              <select
                id="data-fetch-source"
                value={fetchSource}
                onChange={(e) => setFetchSource(e.target.value)}
                className="w-full border border-input bg-card text-foreground rounded px-3 py-2 text-sm"
              >
                <option value="">{tl({ zh: `默认 (${defaultProvider})`, en: `Default (${defaultProvider})` })}</option>
                <option value="tushare">{tl({ zh: "tushare（A股股票/指数）", en: "tushare (A-share stocks & indices)" })}</option>
                <option value="akshare">akshare</option>
                <option value="yfinance">yfinance</option>
              </select>
            </div>
            <button
              onClick={() =>
                fetchOne.mutate({
                  symbol: fetchSymbol,
                  start: fetchStart,
                  end: fetchEnd,
                  source: fetchSource || undefined,
                })
              }
              disabled={fetchOne.isPending}
              className="w-full bg-primary text-primary-foreground rounded-md py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
            >
              {fetchOne.isPending ? tl({ zh: "拉取中...", en: "Fetching..." }) : tl({ zh: "拉取", en: "Fetch" })}
            </button>
            {fetchOne.data && (
              <p className="text-sm text-success">
                {tl({
                  zh: `已获取 ${fetchOne.data.bar_count} 根日线（来源 ${fetchOne.data.source ?? "未记录"}${
                    fetchOne.data.fallback_used ? `，主源失败，已切换 ${fetchOne.data.fallback_source}` : ""
                  }；停复牌事件新增 ${fetchOne.data.lifecycle_events}${
                    fetchOne.data.lifecycle_sync_failed ? `，事件同步失败（${fetchOne.data.lifecycle_sync_error ?? "未知错误"}）` : ""
                  }）`,
                  en: `Fetched ${fetchOne.data.bar_count} daily bars (source ${fetchOne.data.source ?? "not recorded"}${
                    fetchOne.data.fallback_used ? `, primary source failed, switched to ${fetchOne.data.fallback_source}` : ""
                  }; ${fetchOne.data.lifecycle_events} new halt/resume events${
                    fetchOne.data.lifecycle_sync_failed ? `, event sync failed (${fetchOne.data.lifecycle_sync_error ?? "unknown error"})` : ""
                  })`,
                })}
              </p>
            )}
            {fetchOne.error && (
              <p className="text-sm text-destructive">
                {(fetchOne.error as Error).message}
              </p>
            )}
          </div>
        </div>
      </div>

      {/* Bulk Download */}
      <div className="rounded-lg border border-border bg-card p-5">
        <h2 className="mb-4 flex items-center gap-1 text-lg font-semibold">
          {tl({ zh: "批量拉取", en: "Bulk Download" })}
          <InfoHint content={INFO_HINTS.data.bulkDownload} />
        </h2>
        <div className="mb-4 grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-6">
          <div>
            <HintLabel htmlFor="data-bulk-market" hint={INFO_HINTS.data.market}>
              {tl({ zh: "市场", en: "Market" })}
            </HintLabel>
            <select
              id="data-bulk-market"
              value={dlMarket}
              onChange={(e) => setDlMarket(e.target.value)}
              disabled={isDownloading}
              className="h-10 w-full rounded border border-input bg-card px-3 text-sm text-foreground"
            >
              <option value="a_share">{tl({ zh: "A股", en: "A-shares" })}</option>
              <option value="hk">{tl({ zh: "港股", en: "HK stocks" })}</option>
              <option value="us">{tl({ zh: "美股", en: "US stocks" })}</option>
            </select>
          </div>
          <div>
            <HintLabel
              htmlFor="data-bulk-type"
              hint={INFO_HINTS.data.instrumentType}
            >
              {tl({ zh: "类型", en: "Type" })}
            </HintLabel>
            <select
              id="data-bulk-type"
              value={dlType}
              onChange={(e) => setDlType(e.target.value)}
              disabled={isDownloading}
              className="h-10 w-full rounded border border-input bg-card px-3 text-sm text-foreground"
            >
              <option value="" disabled={tushareBulk}>{tl({ zh: "全部", en: "All" })}</option>
              <option value="stock">{tl({ zh: "股票", en: "Stocks" })}</option>
              <option value="etf" disabled={tushareBulk}>ETF</option>
              <option value="index">{tl({ zh: "指数", en: "Index" })}</option>
            </select>
          </div>
          <div>
            <HintLabel
              htmlFor="data-bulk-start"
              hint={INFO_HINTS.data.bulkStartDate}
            >
              {tl({ zh: "起始日期", en: "Start date" })}
            </HintLabel>
            <input
              id="data-bulk-start"
              type="date"
              value={dlStart}
              onChange={(e) => setDlStart(e.target.value)}
              disabled={isDownloading}
              className="h-10 w-full rounded border border-input bg-card px-3 text-sm text-foreground"
            />
          </div>
          <div>
            <HintLabel
              htmlFor="data-bulk-source"
              hint={INFO_HINTS.data.bulkSource}
            >
              {tl({ zh: "数据源", en: "Data provider" })}
            </HintLabel>
            <select
              id="data-bulk-source"
              value={dlSource}
              onChange={(e) => {
                const nextSource = e.target.value;
                setDlSource(nextSource);
                if ((nextSource || defaultProvider) === "tushare" && dlType === "etf") {
                  setDlType("stock");
                }
              }}
              disabled={isDownloading}
              className="h-10 w-full rounded border border-input bg-card px-3 text-sm text-foreground"
            >
              <option value="">{tl({ zh: `默认 (${defaultProvider})`, en: `Default (${defaultProvider})` })}</option>
              <option value="tushare">{tl({ zh: "tushare（A股股票/指数）", en: "tushare (A-share stocks & indices)" })}</option>
              <option value="akshare">akshare</option>
              <option value="yfinance">yfinance</option>
            </select>
          </div>
          <div>
            <label htmlFor="data-bulk-board" className="mb-1 block text-sm text-muted-foreground">
              {tl({ zh: "上市板块", en: "Listing board" })}
            </label>
            <select
              id="data-bulk-board"
              value={dlBoard}
              onChange={(e) => setDlBoard(e.target.value)}
              disabled={isDownloading || dlMarket !== "a_share" || dlType !== "stock"}
              className="h-10 w-full rounded border border-input bg-card px-3 text-sm text-foreground"
            >
              <option value="">{tl({ zh: "全部板块", en: "All boards" })}</option>
              <option value="sse_main">{tl({ zh: "沪市主板", en: "SSE Main Board" })}</option>
              <option value="szse_main">{tl({ zh: "深市主板", en: "SZSE Main Board" })}</option>
              <option value="chinext">{tl({ zh: "创业板", en: "ChiNext" })}</option>
              <option value="star">{tl({ zh: "科创板", en: "STAR Market" })}</option>
              <option value="bse">{tl({ zh: "北交所", en: "BSE" })}</option>
              <option value="cdr">CDR</option>
            </select>
          </div>
          <div className="flex items-end">
            <button
              onClick={() =>
                startDownload.mutate({ source: dlSource || undefined })
              }
              disabled={isDownloading || startDownload.isPending}
              className="h-10 w-full rounded bg-primary text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {isDownloading ? tl({ zh: "拉取中...", en: "Downloading..." }) : tl({ zh: "开始批量拉取", en: "Start bulk download" })}
            </button>
          </div>
        </div>

        {bulkJobId && (
          <p className="mb-4 text-sm text-muted-foreground">
            {tl({ zh: "任务 ID：", en: "Job ID: " })}
            <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-foreground">
              {bulkJobId}
            </code>{" "}
            <Link
              to={`/jobs?job=${encodeURIComponent(bulkJobId)}`}
              className="text-primary underline underline-offset-4 hover:text-primary/80"
            >
              {tl({ zh: "在任务中心查看", en: "View in Jobs" })}
            </Link>
          </p>
        )}

        <p className="mb-4 text-sm text-muted-foreground">
          {tushareBulk
            ? tl({
                zh: "Tushare 任务支持 A 股股票与指数，并保持缓存为单一来源；失败标的可重跑，不会自动换源。ETF 因复权口径对齐仍在设计中（#341），请暂用 akshare 拉取。",
                en: "Tushare jobs cover A-share stocks and indices and keep the cache single-source; failed symbols can be re-run and never switch sources automatically. ETF is still akshare-only while adjustment semantics are being aligned (#341).",
              })
            : tl({
                zh: "ETF 与其他资产请单独拉取。发布时可与 Tushare 股票缓存组合为多资产混合来源数据集。",
                en: "Fetch ETFs and other asset types separately. At publish time they can be combined with the Tushare stock cache into a multi-asset mixed-source dataset.",
              })}
        </p>

        {tushareBulk && (
          <div
            className="mb-4 rounded-lg border border-primary/25 bg-primary/5 p-4"
            aria-live="polite"
          >
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h3 className="text-sm font-semibold">{tl({ zh: "Tushare 请求预算", en: "Tushare Request Budget" })}</h3>
                <p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
                  {tl({
                    zh: "每次 daily、adj_factor、停复牌请求及重试都会计入本地保护预算。每日上限按北京时间每天 00:00 自动重置，100,000 次是单日额度，不是累计总额度。这里显示的是本应用配置和本地用量，不是 Tushare 账户后台的实时权限。",
                    en: "Every daily, adj_factor and halt/resume request, including retries, counts against the local protection budget. The daily limit resets automatically at 00:00 Beijing time each day; 100,000 calls is a per-day quota, not a cumulative allowance. What is shown here is this app's configuration and local usage, not the live entitlement of your Tushare account console.",
                  })}
                </p>
              </div>
              <span className="rounded bg-primary/15 px-2 py-1 text-xs font-medium text-primary">
                RPM {tushareQuota?.requests_per_minute ?? "…"}
              </span>
            </div>
            {tushareQuotaLoading && !tushareQuota ? (
              <p className="mt-3 text-sm text-muted-foreground">{tl({ zh: "正在读取今日预算…", en: "Reading today's budget…" })}</p>
            ) : tushareQuota ? (
              <>
                <div className="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
                  <div>
                    <span className="text-muted-foreground">{tl({ zh: "统计日期（北京时间）", en: "Stats date (Beijing time)" })}</span>
                    <p className="mt-1 font-medium tabular-nums">{tushareQuota.date}</p>
                  </div>
                  <div>
                    <span className="text-muted-foreground">{tl({ zh: "今日已用", en: "Used today" })}</span>
                    <p className="mt-1 font-medium tabular-nums">
                      {tl({ zh: `${tushareQuota.used.toLocaleString()} 次`, en: `${tushareQuota.used.toLocaleString()} calls` })}
                    </p>
                  </div>
                  <div>
                    <span className="text-muted-foreground">{tl({ zh: "每日上限（00:00刷新）", en: "Daily limit (resets at 00:00)" })}</span>
                    <p className="mt-1 font-medium tabular-nums">
                      {tl({ zh: `${tushareQuota.daily_limit.toLocaleString()} 次`, en: `${tushareQuota.daily_limit.toLocaleString()} calls` })}
                    </p>
                  </div>
                  <div>
                    <span className="text-muted-foreground">{tl({ zh: "剩余预算", en: "Remaining budget" })}</span>
                    <p className={cn(
                      "mt-1 font-medium tabular-nums",
                      tushareQuota.remaining === 0 ? "text-destructive" : "text-success",
                    )}>
                      {tl({ zh: `${tushareQuota.remaining.toLocaleString()} 次`, en: `${tushareQuota.remaining.toLocaleString()} calls` })}
                    </p>
                  </div>
                </div>
                <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted">
                  <div
                    className={cn(
                      "h-full rounded-full transition-[width] duration-300",
                      tushareQuotaPercent >= 90 ? "bg-destructive" : tushareQuotaPercent >= 75 ? "bg-warning" : "bg-primary",
                    )}
                    style={{ width: `${tushareQuotaPercent}%` }}
                  />
                </div>
                {tushareQuotaPercent >= 75 && (
                  <p className="mt-2 text-xs text-warning">
                    {tl({
                      zh: `今日本地预算已使用 ${tushareQuotaPercent.toFixed(1)}%，继续批量拉取可能提前触发保护阈值。`,
                      en: `Today's local budget is ${tushareQuotaPercent.toFixed(1)}% used; continuing bulk downloads may trigger the protection threshold early.`,
                    })}
                  </p>
                )}
              </>
            ) : (
              <p className="mt-3 text-sm text-destructive">
                {tl({ zh: "预算读取失败，请检查 API 服务和配置文件。", en: "Failed to read the budget; check the API service and configuration file." })}
              </p>
            )}
          </div>
        )}

        {/* Progress bar */}
        {isDownloading && bulkJob && (
          <div className="mt-4" aria-live="polite">
            <div className="flex justify-between text-sm text-muted-foreground mb-1">
              <span>{tl({ zh: `进度: ${bulkDone} / ${bulkTotal}`, en: `Progress: ${bulkDone} / ${bulkTotal}` })}</span>
              <span>{bulkTotal > 0 ? `${(bulkDone * 100 / bulkTotal).toFixed(1)}%` : ""}</span>
            </div>
            <div
              className="w-full bg-muted rounded-full h-3 overflow-hidden"
              role="progressbar"
              aria-label={tl({ zh: "批量行情拉取进度", en: "Bulk market data download progress" })}
              aria-valuemin={0}
              aria-valuemax={bulkTotal}
              aria-valuenow={bulkDone}
            >
              <div
                className="bg-success h-full rounded-full transition-all duration-500"
                style={{
                  width: `${bulkTotal > 0 ? (bulkDone * 100 / bulkTotal) : 0}%`,
                }}
              />
            </div>
            {startedAtMs > 0 && bulkDone > 0 && (
              <div className="mt-1 flex justify-between text-xs text-muted-foreground tabular-nums">
                <span>{tl({ zh: `速度: ${bulkSpeed.toFixed(1)} 标的/分`, en: `Speed: ${bulkSpeed.toFixed(1)} symbols/min` })}</span>
                <span>{tl({ zh: `预计剩余: ${formatDuration(bulkEtaSec, lang)}`, en: `ETA: ${formatDuration(bulkEtaSec, lang)}` })}</span>
              </div>
            )}
            {phaseLabel && (
              <p className="mt-2 text-xs text-muted-foreground font-mono">
                {phaseLabel}
              </p>
            )}
            <p className="mt-1 text-xs text-muted-foreground">
              {tl({
                zh: "统一任务队列已合并逐标的日志/质量报告(#144),完成后再检查缓存质量查看明细。",
                en: "The unified job queue merges per-symbol logs/quality reports (#144); after completion, run a cache quality check to see the details.",
              })}
            </p>
          </div>
        )}

        {/* Download result */}
        {bulkJob && !isJobRunning(bulkJob) && bulkJob.status === "succeeded" && (
          <p className="mt-3 text-sm text-success">
            {tl({ zh: `拉取完成: ${bulkDone} / ${bulkTotal}。`, en: `Download completed: ${bulkDone} / ${bulkTotal}.` })}
          </p>
        )}
        {bulkJob && !isJobRunning(bulkJob) && bulkJob.status !== "succeeded" && (
          <p className="mt-3 text-sm text-destructive">
            {tl({ zh: "错误: ", en: "Error: " })}
            {bulkJob.error_summary ?? bulkJob.status}
          </p>
        )}
        {startDownload.error && (
          <p className="mt-3 text-sm text-destructive">
            {(startDownload.error as Error).message}
          </p>
        )}
      </div>

      {/* Instruments table */}
      <div className="rounded-lg border border-border bg-card">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b px-5 py-3">
          <h2 className="flex items-center gap-1 text-lg font-semibold">
            {tl({ zh: `标的列表 (${listedInstrumentTotal})`, en: `Instruments (${listedInstrumentTotal})` })}
            <InfoHint content={INFO_HINTS.data.instrumentList} />
          </h2>
          <div className="flex flex-wrap items-center gap-3">
            <div className="space-y-1">
              <label htmlFor="data-instrument-search" className="text-xs text-muted-foreground">
                {tl({ zh: "搜索代码/名称", en: "Search code/name" })}
              </label>
              <input
                id="data-instrument-search"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder={tl({ zh: "如 510300", en: "e.g. 510300" })}
                className="w-40 rounded-md border border-input bg-card px-3 py-1 text-sm text-foreground placeholder:text-muted-foreground"
              />
            </div>
            <div className="space-y-1">
              <label htmlFor="data-instrument-market" className="text-xs text-muted-foreground">
                {tl({ zh: "市场", en: "Market" })}
              </label>
              <select
                id="data-instrument-market"
                value={marketFilter}
                onChange={(e) => setMarketFilter(e.target.value)}
                className="rounded-md border border-input bg-card px-2 py-1 text-sm text-foreground"
              >
                <option value="">{tl({ zh: "全部市场", en: "All markets" })}</option>
                <option value="a_share">{tl({ zh: "A股", en: "A-shares" })}</option>
                <option value="hk">{tl({ zh: "港股", en: "HK stocks" })}</option>
                <option value="us">{tl({ zh: "美股", en: "US stocks" })}</option>
              </select>
            </div>
            <div className="space-y-1">
              <label htmlFor="data-instrument-type" className="text-xs text-muted-foreground">
                {tl({ zh: "类型", en: "Type" })}
              </label>
              <select
                id="data-instrument-type"
                value={typeFilter}
                onChange={(e) => setTypeFilter(e.target.value)}
                className="rounded-md border border-input bg-card px-2 py-1 text-sm text-foreground"
              >
                <option value="">{tl({ zh: "全部类型", en: "All types" })}</option>
                <option value="stock">{tl({ zh: "股票", en: "Stocks" })}</option>
                <option value="etf">ETF</option>
              </select>
            </div>
          </div>
        </div>
        <div className="overflow-x-auto scrollbar-thin">
          <table className="w-full text-sm">
            <thead className="bg-background text-muted-foreground">
              <tr>
                <th className="px-4 py-2 text-left">{tl({ zh: "代码", en: "Code" })}</th>
                <th className="px-4 py-2 text-left">{tl({ zh: "名称", en: "Name" })}</th>
                <th className="px-4 py-2 text-left">{tl({ zh: "市场", en: "Market" })}</th>
                <th className="px-4 py-2 text-left">{tl({ zh: "类型", en: "Type" })}</th>
                <th className="px-4 py-2 text-left">{tl({ zh: "交易所", en: "Exchange" })}</th>
                <th className="px-4 py-2 text-right">{tl({ zh: "操作", en: "Actions" })}</th>
              </tr>
            </thead>
            <tbody>
              {pagedInstruments.map((ins) => (
                  <tr
                    key={ins.code}
                    className="border-t hover:bg-accent"
                  >
                    <td className="px-4 py-2 font-mono">{ins.code}</td>
                    <td className="px-4 py-2">{ins.name}</td>
                    <td className="px-4 py-2">{ins.market}</td>
                    <td className="px-4 py-2">{ins.instrument_type}</td>
                    <td className="px-4 py-2 text-muted-foreground">{ins.exchange ?? "—"}</td>
                    <td className="px-4 py-2 text-right">
                      <button
                        type="button"
                        onClick={() => setFetchSymbol(ins.code)}
                        className="rounded-md border border-input px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
                        aria-label={tl({ zh: `选择 ${ins.code} 到单标的拉取`, en: `Select ${ins.code} for single fetch` })}
                      >
                        {tl({ zh: "选择", en: "Select" })}
                      </button>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
        {pagedInstruments.length === 0 && (
          <div className="p-8 text-center text-muted-foreground/70">
            {totalInstruments === 0
              ? tl({ zh: '标的池为空 — 点击上方"同步标的池"自动发现', en: 'Universe is empty — click "Sync universe" above to auto-discover' })
              : tl({ zh: "没有匹配当前搜索或筛选条件的活跃标的", en: "No active instruments match the current search or filters" })}
          </div>
        )}
        {listedInstrumentTotal > PAGE_SIZE && (
          <div className="flex items-center justify-between border-t px-5 py-3 text-sm">
            <span className="text-muted-foreground">
              {tl({ zh: `第 ${instPage}/${instTotalPages} 页 · 共 ${listedInstrumentTotal} 条`, en: `Page ${instPage}/${instTotalPages} · ${listedInstrumentTotal} total` })}
            </span>
            <div className="flex gap-2">
              <button
                onClick={() => setInstPage((p) => Math.max(1, p - 1))}
                disabled={instPage <= 1}
                className="rounded border border-input px-3 py-1 hover:bg-accent disabled:opacity-40"
              >
                {tl({ zh: "上一页", en: "Previous" })}
              </button>
              <button
                onClick={() => setInstPage((p) => Math.min(instTotalPages, p + 1))}
                disabled={instPage >= instTotalPages}
                className="rounded border border-input px-3 py-1 hover:bg-accent disabled:opacity-40"
              >
                {tl({ zh: "下一页", en: "Next" })}
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Cache status table */}
      <div className="rounded-lg border border-border bg-card">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b px-5 py-3">
          <div className="flex items-center gap-1">
            <h2 className="text-lg font-semibold">{tl({ zh: `已缓存数据 (${status?.total ?? 0})`, en: `Cached Data (${status?.total ?? 0})` })}</h2>
            <InfoHint content={INFO_HINTS.data.cachedData} />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={repairSource}
              onChange={(event) => setRepairSource(event.target.value as "akshare" | "yfinance" | "tushare")}
              className="rounded border border-input bg-card px-2 py-1.5 text-sm text-foreground"
              aria-label={tl({ zh: "批量换源修复数据源", en: "Bulk source-switch repair provider" })}
            >
              <option value="tushare">{tl({ zh: "备用源: tushare（仅A股股票）", en: "Fallback: tushare (A-share stocks only)" })}</option>
              <option value="akshare">{tl({ zh: "备用源: akshare", en: "Fallback: akshare" })}</option>
              <option value="yfinance">{tl({ zh: "备用源: yfinance", en: "Fallback: yfinance" })}</option>
            </select>
            <button
              onClick={() => refetchQuality()}
              disabled={qualityFetching || repairQuality.isPending}
              className="rounded border border-input px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
            >
              {qualityFetching ? tl({ zh: "检查中...", en: "Checking..." }) : tl({ zh: "检查全部缓存", en: "Check all caches" })}
            </button>
            <button
              onClick={() => repairQuality.mutate()}
              disabled={
                repairQuality.isPending ||
                !qualityReports?.some((report) => !report.passed)
              }
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {repairQuality.isPending
                ? tl({ zh: "批量修复中...", en: "Repairing..." })
                : tl({
                    zh: `批量换源修复 (${qualityReports?.filter((report) => !report.passed).length ?? 0})`,
                    en: `Bulk source-switch repair (${qualityReports?.filter((report) => !report.passed).length ?? 0})`,
                  })}
            </button>
          </div>
        </div>
        <CacheQualitySummary
          reports={qualityReports}
          expanded={showQualityDetail}
          onToggle={() => setShowQuality((current) => !current)}
          repairJob={repairJob}
          repairPending={repairQuality.isPending}
          repairError={repairQuality.error as Error | null}
        />
        {!status || status.items.length === 0 ? (
          <div className="p-8 text-center text-muted-foreground/70">{tl({ zh: "缓存为空", en: "Cache is empty" })}</div>
        ) : (
          <div className="overflow-x-auto scrollbar-thin">
          <table className="w-full text-sm">
            <thead className="bg-background text-muted-foreground">
              <tr>
                <th className="px-4 py-2 text-left">{tl({ zh: "标的", en: "Symbol" })}</th>
                <th className="px-4 py-2 text-right">{tl({ zh: "Bar 数", en: "Bars" })}</th>
                <th className="px-4 py-2 text-left">{tl({ zh: "范围", en: "Range" })}</th>
                <th className="px-4 py-2 text-left">{tl({ zh: "来源", en: "Source" })}</th>
                <th className="px-4 py-2 text-right">{tl({ zh: "预览", en: "Preview" })}</th>
              </tr>
            </thead>
            <tbody>
              {status.items.map((s) => (
                <tr key={`${s.symbol}-${s.period}-${s.adjust}`} className="cursor-pointer border-t hover:bg-muted/50" onClick={() => setPreviewSymbol(s.symbol)}>
                  <td className="px-4 py-2 font-mono">{s.symbol}</td>
                  <td className="px-4 py-2 text-right">{s.bar_count}</td>
                  <td className="px-4 py-2 text-muted-foreground">
                    {s.first_date ?? "—"} ~ {s.last_date ?? "—"}
                  </td>
                  <td className="px-4 py-2 text-muted-foreground">{s.source ?? tl({ zh: "未记录", en: "Not recorded" })}</td>
                  <td className="px-4 py-2 text-right text-xs text-primary">{tl({ zh: "查看", en: "View" })}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
        {cacheTotal > PAGE_SIZE && (
          <div className="flex items-center justify-between border-t px-5 py-3 text-sm">
            <span className="text-muted-foreground">
              {tl({ zh: `第 ${cachePage}/${cacheTotalPages} 页 · 共 ${cacheTotal} 条`, en: `Page ${cachePage}/${cacheTotalPages} · ${cacheTotal} total` })}
            </span>
            <div className="flex gap-2">
              <button
                onClick={() => setCachePage((p) => Math.max(1, p - 1))}
                disabled={cachePage <= 1}
                className="rounded border border-input px-3 py-1 hover:bg-accent disabled:opacity-40"
              >
                {tl({ zh: "上一页", en: "Previous" })}
              </button>
              <button
                onClick={() => setCachePage((p) => Math.min(cacheTotalPages, p + 1))}
                disabled={cachePage >= cacheTotalPages}
                className="rounded border border-input px-3 py-1 hover:bg-accent disabled:opacity-40"
              >
                {tl({ zh: "下一页", en: "Next" })}
              </button>
            </div>
          </div>
        )}
      </div>

      <CachePreviewDialog symbol={previewSymbol} onClose={() => setPreviewSymbol(null)} />
    </div>
  );
}

/** 缓存数据预览对话框:只读展示本地 parquet 尾部 bar(GET /data/cache/preview)。 */
function CachePreviewDialog({ symbol, onClose }: { symbol: string | null; onClose: () => void }) {
  const { tl } = useT();
  const previewQuery = useQuery({
    queryKey: ["cache-preview", symbol],
    queryFn: () => api.previewCacheBars(symbol as string, 20),
    enabled: symbol !== null,
  });

  return (
    <Dialog open={symbol !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="font-mono text-sm">{symbol ?? ""}</DialogTitle>
          <DialogDescription>
            {tl({
              zh: "本地缓存尾部 20 根 bar(只读);数据以本地缓存为准,非券商口径。",
              en: "Last 20 bars of the local cache (read-only); data reflects the local cache, not the broker.",
            })}
          </DialogDescription>
        </DialogHeader>
        {previewQuery.isLoading ? (
          <p className="py-6 text-center text-sm text-muted-foreground">{tl({ zh: "加载中…", en: "Loading…" })}</p>
        ) : previewQuery.isError ? (
          <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {previewQuery.error instanceof Error ? previewQuery.error.message : tl({ zh: "预览加载失败", en: "Failed to load preview" })}
          </p>
        ) : previewQuery.data ? (
          <div className="space-y-2">
            <p className="text-xs text-muted-foreground">
              {previewQuery.data.label} ·{" "}
              {tl({
                zh: `尾部 ${previewQuery.data.rows.length} 行 / 共 ${previewQuery.data.total_rows} 行`,
                en: `last ${previewQuery.data.rows.length} of ${previewQuery.data.total_rows} rows`,
              })}
            </p>
            <div className="max-h-80 overflow-auto rounded-md border border-border scrollbar-thin">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-background text-muted-foreground">
                  <tr>
                    {previewQuery.data.columns.map((column) => (
                      <th key={column} className="whitespace-nowrap px-2 py-1.5 text-left font-mono">
                        {column}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {previewQuery.data.rows.map((row, i) => (
                    <tr key={i} className="border-t">
                      {previewQuery.data.columns.map((column) => (
                        <td key={column} className="whitespace-nowrap px-2 py-1.5 font-mono">
                          {row[column] === null || row[column] === undefined ? (
                            <span className="text-muted-foreground/50">null</span>
                          ) : (
                            String(row[column])
                          )}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function CacheQualitySummary({
  reports,
  expanded,
  onToggle,
  repairJob,
  repairPending,
  repairError,
}: {
  reports: QualityReport[] | undefined;
  expanded: boolean;
  onToggle: () => void;
  repairJob: JobOut | undefined;
  repairPending: boolean;
  repairError: Error | null;
}) {
  const { tl } = useT();

  if (!reports) {
    return (
      <div className="border-b px-5 py-3 text-sm text-muted-foreground">
        {tl({ zh: "点击“检查全部缓存”后显示存量数据质量结果。", en: 'Run "Check all caches" to show quality results for the cached data.' })}
      </div>
    );
  }
  if (reports.length === 0) {
    return <div className="border-b px-5 py-3 text-sm text-muted-foreground">{tl({ zh: "无缓存数据。", en: "No cached data." })}</div>;
  }

  const failed = reports.filter((report) => !report.passed);
  return (
    <div className="border-b px-5 py-3">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <span>
          {tl({ zh: "已检查", en: "Checked" })} <strong>{reports.length}</strong> {tl({ zh: "个缓存标的", en: "cached symbols" })}
        </span>
        <span className="text-success">{tl({ zh: `通过 ${reports.length - failed.length}`, en: `Passed ${reports.length - failed.length}` })}</span>
        <span className={failed.length ? "text-destructive" : "text-muted-foreground"}>
          {tl({ zh: `异常 ${failed.length}`, en: `Failed ${failed.length}` })}
        </span>
        {failed.length > 0 && (
          <button onClick={onToggle} className="text-primary hover:underline">
            {expanded
              ? tl({ zh: "收起异常详情", en: "Collapse failure details" })
              : tl({ zh: "展开异常详情", en: "Expand failure details" })}
          </button>
        )}
      </div>
      {repairPending && (
        <p className="mt-2 text-sm text-muted-foreground">
          {tl({ zh: "批量修复任务已提交,等待队列调度…", en: "Bulk repair job submitted, waiting for the queue…" })}
        </p>
      )}
      {repairJob && isJobRunning(repairJob) && (
        <p className="mt-2 text-sm text-muted-foreground">
          {tl({
            zh: `批量修复进行中: ${repairJob.progress_done} / ${repairJob.progress_total}${repairJob.phase ? ` · ${repairJob.phase}` : ""}`,
            en: `Bulk repair in progress: ${repairJob.progress_done} / ${repairJob.progress_total}${repairJob.phase ? ` · ${repairJob.phase}` : ""}`,
          })}
        </p>
      )}
      {repairJob && !isJobRunning(repairJob) && repairJob.status === "succeeded" && (
        <p className="mt-2 text-sm text-success">
          {tl({
            zh: "批量修复完成(逐标的报告已随 #144 降级,请重新检查缓存查看明细)。",
            en: "Bulk repair completed (per-symbol reports were reduced in #144; re-check the cache to see the details).",
          })}
        </p>
      )}
      {repairJob && !isJobRunning(repairJob) && repairJob.status !== "succeeded" && (
        <p className="mt-2 text-sm text-destructive">
          {tl({ zh: "批量修复失败: ", en: "Bulk repair failed: " })}
          {repairJob.error_summary ?? repairJob.status}
        </p>
      )}
      {repairError && (
        <p className="mt-2 text-sm text-destructive">{tl({ zh: "批量修复失败: ", en: "Bulk repair failed: " })}{repairError.message}</p>
      )}
      {expanded && failed.length > 0 && (
        <div className="mt-3 max-h-80 overflow-auto rounded border">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-muted">
              <tr>
                <th className="px-3 py-2 text-left">{tl({ zh: "标的", en: "Symbol" })}</th>
                <th className="px-3 py-2 text-right">{tl({ zh: "异常数", en: "Anomalies" })}</th>
                <th className="px-3 py-2 text-right">{tl({ zh: "重复", en: "Duplicates" })}</th>
                <th className="px-3 py-2 text-left">{tl({ zh: "数据源", en: "Data provider" })}</th>
                <th className="px-3 py-2 text-left">{tl({ zh: "异常日期 / 原因", en: "Anomaly date / reason" })}</th>
              </tr>
            </thead>
            <tbody>
              {failed.map((report) => (
                <tr key={report.symbol} className="border-t">
                  <td className="px-3 py-1.5 font-mono">{report.symbol}</td>
                  <td className="px-3 py-1.5 text-right font-semibold text-destructive">
                    {report.anomaly_count}
                  </td>
                  <td className="px-3 py-1.5 text-right">{report.duplicate_count || "-"}</td>
                  <td className="px-3 py-1.5">{report.sources.join(",") || tl({ zh: "未记录", en: "Not recorded" })}</td>
                  <td className="px-3 py-1.5">
                    {report.anomalies
                      .slice(0, 3)
                      .map((anomaly) => `${anomaly.date}(${anomaly.reasons.join(",")})`)
                      .join("; ")}
                    {report.anomalies.length > 3 && ` +${report.anomalies.length - 3}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function StatCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: InfoHintDefinition;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="flex items-center gap-1 text-sm text-muted-foreground">
        {label}
        {hint && <InfoHint content={hint} />}
      </div>
      <div className="text-xl font-bold mt-1">{value}</div>
    </div>
  );
}
