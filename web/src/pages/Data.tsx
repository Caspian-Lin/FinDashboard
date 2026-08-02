import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import InfoHint, { HintLabel } from "../components/InfoHint";
import { api } from "../lib/api";
import type { QualityRepairResult, QualityReport } from "../lib/api";
import { INFO_HINTS, type InfoHintDefinition } from "../lib/infoHints";
import { cn } from "../lib/utils";

const BULK_PHASE_LABELS: Record<string, string> = {
  starting: "准备任务",
  checking_cache: "检查缓存",
  cache_hit: "缓存命中",
  fetching: "请求行情",
  reading_cache: "读取缓存",
  writing_cache: "写入缓存",
};

export default function Data() {
  const queryClient = useQueryClient();
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
  const [dlStart, setDlStart] = useState("2015-01-01");
  const [dlSource, setDlSource] = useState("");
  const [showQualityDetail, setShowQuality] = useState(false);
  const [repairSource, setRepairSource] = useState<"akshare" | "yfinance" | "tushare">("tushare");

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

  const { data: bulkStatus } = useQuery({
    queryKey: ["bulk-download-status"],
    queryFn: api.getBulkDownloadStatus,
    refetchInterval: 2000,
  });

  const { data: qualityReports, refetch: refetchQuality, isFetching: qualityFetching } = useQuery({
    queryKey: ["quality-reports"],
    queryFn: () => api.checkQuality(),
    enabled: false,
  });

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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["instruments"] });
      queryClient.invalidateQueries({ queryKey: ["instrument-count"] });
      queryClient.invalidateQueries({ queryKey: ["research-instruments"] });
    },
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
        start: dlStart,
        source: params?.source,
      }),
    onSuccess: (data) => {
      queryClient.setQueryData(["bulk-download-status"], data);
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
    onSuccess: async () => {
      await refetchQuality();
      await queryClient.invalidateQueries({ queryKey: ["data-status"] });
    },
  });

  const totalInstruments = databaseUniverse?.total ?? 0;
  const isDownloading = startDownload.isPending || bulkStatus?.status === "running";
  const phaseLabel = BULK_PHASE_LABELS[bulkStatus?.phase ?? ""];

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
    <div>
      <h1 className="text-2xl font-bold mb-6">行情数据</h1>

      {/* Stats row */}
      <div className="mb-6 grid grid-cols-2 gap-4 xl:grid-cols-4">
        <StatCard
          label="数据库标的数"
          value={String(totalInstruments)}
          hint={INFO_HINTS.data.databaseUniverse}
        />
        <StatCard label="缓存标的数" value={String(status?.total ?? 0)} />
        <StatCard label="A股" value={String(aShareStocks?.total ?? 0)} />
        <StatCard label="ETF" value={String(etfs?.total ?? 0)} />
      </div>

      {/* Sync + Single fetch */}
      <div className="mb-6 grid grid-cols-1 gap-6 xl:grid-cols-2">
        {/* Universe Sync */}
        <div className="bg-card rounded-lg shadow p-5">
          <div className="flex items-center justify-between mb-3">
            <h2 className="flex items-center gap-1 text-lg font-semibold">
              标的池同步
              <InfoHint content={INFO_HINTS.data.universeSync} />
              <span className="ml-2 rounded bg-blue-600/20 px-2 py-0.5 text-xs font-medium text-blue-400">
                akshare
              </span>
            </h2>
            {totalInstruments > 0 && (
              <span className="text-sm text-muted-foreground/70">已同步 {totalInstruments} 条</span>
            )}
          </div>
          <p className="text-sm text-muted-foreground mb-3">
            从 akshare 自动发现全市场 A 股(~5500) + ETF(~1600),写入数据库。
          </p>
          <button
            type="button"
            onClick={() => sync.mutate()}
            disabled={sync.isPending}
            className="w-full bg-indigo-600 text-white rounded py-2 text-sm font-medium hover:bg-indigo-700 disabled:opacity-50"
          >
            {sync.isPending ? "同步中..." : totalInstruments === 0 ? "同步标的池" : "刷新标的池"}
          </button>
          {sync.data && (
            <p className="text-sm text-success mt-2">
              同步完成: {sync.data.total} 条标的
            </p>
          )}
          {sync.error && (
            <p className="text-sm text-destructive mt-2">
              {(sync.error as Error).message}
            </p>
          )}
        </div>

        {/* Single fetch */}
        <div className="bg-card rounded-lg shadow p-5">
          <h2 className="text-lg font-semibold mb-3">单标的拉取</h2>
          <div className="space-y-3">
            <div>
              <HintLabel htmlFor="data-symbol" hint={INFO_HINTS.data.symbol}>
                标的
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
                  开始
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
                  结束
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
                数据源
              </label>
              <select
                id="data-fetch-source"
                value={fetchSource}
                onChange={(e) => setFetchSource(e.target.value)}
                className="w-full border border-input bg-card text-foreground rounded px-3 py-2 text-sm"
              >
                <option value="">默认 ({defaultProvider})</option>
                <option value="tushare">tushare（A股股票）</option>
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
              className="w-full bg-primary text-white rounded py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
            >
              {fetchOne.isPending ? "拉取中..." : "拉取"}
            </button>
            {fetchOne.data && (
              <p className="text-sm text-success">
                已获取 {fetchOne.data.bar_count} 根日线（来源 {fetchOne.data.source ?? "未记录"}
                {fetchOne.data.fallback_used
                  ? `，主源失败，已切换 ${fetchOne.data.fallback_source}`
                  : ""}；停复牌事件新增 {fetchOne.data.lifecycle_events}
                {fetchOne.data.lifecycle_sync_failed
                  ? `，事件同步失败（${fetchOne.data.lifecycle_sync_error ?? "未知错误"}）`
                  : ""}）
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
      <div className="bg-card rounded-lg shadow p-5 mb-6">
        <h2 className="mb-4 flex items-center gap-1 text-lg font-semibold">
          批量拉取
          <InfoHint content={INFO_HINTS.data.bulkDownload} />
        </h2>
        <div className="mb-4 grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-5">
          <div>
            <HintLabel htmlFor="data-bulk-market" hint={INFO_HINTS.data.market}>
              市场
            </HintLabel>
            <select
              id="data-bulk-market"
              value={dlMarket}
              onChange={(e) => setDlMarket(e.target.value)}
              disabled={isDownloading}
              className="w-full border border-input bg-card text-foreground rounded px-3 py-2 text-sm"
            >
              <option value="a_share">A股</option>
              <option value="hk">港股</option>
              <option value="us">美股</option>
            </select>
          </div>
          <div>
            <HintLabel
              htmlFor="data-bulk-type"
              hint={INFO_HINTS.data.instrumentType}
            >
              类型
            </HintLabel>
            <select
              id="data-bulk-type"
              value={dlType}
              onChange={(e) => setDlType(e.target.value)}
              disabled={isDownloading}
              className="w-full border border-input bg-card text-foreground rounded px-3 py-2 text-sm"
            >
              <option value="" disabled={tushareBulk}>全部</option>
              <option value="stock">股票</option>
              <option value="etf" disabled={tushareBulk}>ETF</option>
              <option value="index" disabled={tushareBulk}>指数</option>
            </select>
          </div>
          <div>
            <HintLabel
              htmlFor="data-bulk-start"
              hint={INFO_HINTS.data.bulkStartDate}
            >
              起始日期
            </HintLabel>
            <input
              id="data-bulk-start"
              type="date"
              value={dlStart}
              onChange={(e) => setDlStart(e.target.value)}
              disabled={isDownloading}
              className="w-full border border-input bg-card text-foreground rounded px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label htmlFor="data-bulk-source" className="block text-sm font-medium text-foreground mb-1">
              数据源
            </label>
            <select
              id="data-bulk-source"
              value={dlSource}
              onChange={(e) => {
                const nextSource = e.target.value;
                setDlSource(nextSource);
                if ((nextSource || defaultProvider) === "tushare") {
                  setDlType("stock");
                }
              }}
              disabled={isDownloading}
              className="w-full border border-input bg-card text-foreground rounded px-3 py-2 text-sm"
            >
              <option value="">默认 ({defaultProvider})</option>
              <option value="tushare">tushare（A股股票）</option>
              <option value="akshare">akshare</option>
              <option value="yfinance">yfinance</option>
            </select>
          </div>
          <div className="flex items-end">
            <button
              onClick={() =>
                startDownload.mutate({ source: dlSource || undefined })
              }
              disabled={isDownloading || startDownload.isPending}
              className="w-full bg-primary text-primary-foreground rounded py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
            >
              {isDownloading ? "拉取中..." : "开始批量拉取"}
            </button>
          </div>
        </div>

        <p className="mb-4 text-sm text-muted-foreground">
          {tushareBulk
            ? "Tushare 任务只拉取 A 股股票，并保持缓存为单一来源；失败标的可重跑，不会自动换源。"
            : "ETF 与其他资产请单独拉取。发布时可与 Tushare 股票缓存组合为多资产混合来源数据集。"}
        </p>

        {tushareBulk && (
          <div
            className="mb-4 rounded-lg border border-primary/25 bg-primary/5 p-4"
            aria-live="polite"
          >
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h3 className="text-sm font-semibold">Tushare 请求预算</h3>
                <p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
                  每次 daily、adj_factor、停复牌请求及重试都会计入本地保护预算。每日上限按北京时间每天 00:00 自动重置，100,000 次是单日额度，不是累计总额度。这里显示的是本应用配置和本地用量，不是 Tushare 账户后台的实时权限。
                </p>
              </div>
              <span className="rounded bg-primary/15 px-2 py-1 text-xs font-medium text-primary">
                RPM {tushareQuota?.requests_per_minute ?? "…"}
              </span>
            </div>
            {tushareQuotaLoading && !tushareQuota ? (
              <p className="mt-3 text-sm text-muted-foreground">正在读取今日预算…</p>
            ) : tushareQuota ? (
              <>
                <div className="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
                  <div>
                    <span className="text-muted-foreground">统计日期（北京时间）</span>
                    <p className="mt-1 font-medium tabular-nums">{tushareQuota.date}</p>
                  </div>
                  <div>
                    <span className="text-muted-foreground">今日已用</span>
                    <p className="mt-1 font-medium tabular-nums">
                      {tushareQuota.used.toLocaleString()} 次
                    </p>
                  </div>
                  <div>
                    <span className="text-muted-foreground">每日上限（00:00刷新）</span>
                    <p className="mt-1 font-medium tabular-nums">
                      {tushareQuota.daily_limit.toLocaleString()} 次
                    </p>
                  </div>
                  <div>
                    <span className="text-muted-foreground">剩余预算</span>
                    <p className={cn(
                      "mt-1 font-medium tabular-nums",
                      tushareQuota.remaining === 0 ? "text-destructive" : "text-success",
                    )}>
                      {tushareQuota.remaining.toLocaleString()} 次
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
                    今日本地预算已使用 {tushareQuotaPercent.toFixed(1)}%，继续批量拉取可能提前触发保护阈值。
                  </p>
                )}
              </>
            ) : (
              <p className="mt-3 text-sm text-destructive">预算读取失败，请检查 API 服务和配置文件。</p>
            )}
          </div>
        )}

        {/* Progress bar */}
        {isDownloading && bulkStatus && (
          <div className="mt-4" aria-live="polite">
            <div className="flex justify-between text-sm text-muted-foreground mb-1">
              <span>进度: {bulkStatus.done} / {bulkStatus.total}</span>
              <span>{bulkStatus.total > 0 ? `${(bulkStatus.done * 100 / bulkStatus.total).toFixed(1)}%` : ""}</span>
            </div>
            <div
              className="w-full bg-muted rounded-full h-3 overflow-hidden"
              role="progressbar"
              aria-label="批量行情拉取进度"
              aria-valuemin={0}
              aria-valuemax={bulkStatus.total}
              aria-valuenow={bulkStatus.done}
            >
              <div
                className="bg-green-500 h-full rounded-full transition-all duration-500"
                style={{
                  width: `${bulkStatus.total > 0 ? (bulkStatus.done * 100 / bulkStatus.total) : 0}%`,
                }}
              />
            </div>
            {(phaseLabel || bulkStatus.current_symbol) && (
              <p className="mt-2 text-xs text-muted-foreground font-mono">
                {phaseLabel ?? "处理中"}
                {bulkStatus.current_symbol ? ` · ${bulkStatus.current_symbol}` : ""}
              </p>
            )}
          </div>
        )}

        {/* Download result */}
        {bulkStatus?.status === "done" && (
          <div className="mt-3 space-y-1 text-sm">
            <p className="text-success">
              拉取完成: 成功 {bulkStatus.success} / {bulkStatus.total}, 失败 {bulkStatus.failed}
            </p>
            <p className="text-muted-foreground">
              缓存命中 {bulkStatus.cache_hits ?? 0} 个，需联网更新 {bulkStatus.cache_misses ?? 0} 个；已有日期不会因普通增量更新重复请求。
            </p>
            {bulkStatus.quality_reports && bulkStatus.quality_reports.length > 0 && (
              <p className="text-muted-foreground">
                质量校验: 通过 {bulkStatus.quality_passed ?? 0}, 失败 {bulkStatus.quality_failed ?? 0},
                换源修复 {bulkStatus.fallback_used ?? 0}
              </p>
            )}
            <p className="text-muted-foreground">
              停复牌事件新增 {bulkStatus.lifecycle_events ?? 0}，同步失败标的 {bulkStatus.lifecycle_sync_failed ?? 0}
            </p>
          </div>
        )}
        {bulkStatus?.status === "error" && (
          <p className="mt-3 text-sm text-destructive">错误: {bulkStatus.error}</p>
        )}
        {startDownload.error && (
          <p className="mt-3 text-sm text-destructive">
            {(startDownload.error as Error).message}
          </p>
        )}
      </div>

      {/* Instruments table */}
      <div className="bg-card rounded-lg shadow mb-6">
        <div className="flex items-center justify-between px-5 py-3 border-b">
          <h2 className="flex items-center gap-1 text-lg font-semibold">
            标的列表 ({listedInstrumentTotal})
            <InfoHint content={INFO_HINTS.data.instrumentList} />
          </h2>
          <div className="flex gap-3">
            <input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="搜索代码/名称"
              className="border border-input bg-card text-foreground rounded px-3 py-1 text-sm w-40"
            />
            <select
              value={marketFilter}
              onChange={(e) => setMarketFilter(e.target.value)}
              className="border border-input bg-card text-foreground rounded px-2 py-1 text-sm"
            >
              <option value="">全部市场</option>
              <option value="a_share">A股</option>
              <option value="hk">港股</option>
              <option value="us">美股</option>
            </select>
            <select
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
              className="border border-input bg-card text-foreground rounded px-2 py-1 text-sm"
            >
              <option value="">全部类型</option>
              <option value="stock">股票</option>
              <option value="etf">ETF</option>
            </select>
          </div>
        </div>
        <table className="w-full text-sm">
          <thead className="bg-background text-muted-foreground">
            <tr>
              <th className="px-4 py-2 text-left">代码</th>
              <th className="px-4 py-2 text-left">名称</th>
              <th className="px-4 py-2 text-left">市场</th>
              <th className="px-4 py-2 text-left">类型</th>
              <th className="px-4 py-2 text-left">交易所</th>
            </tr>
          </thead>
          <tbody>
            {pagedInstruments.map((ins) => (
                <tr
                  key={ins.code}
                  className="cursor-pointer border-t hover:bg-accent"
                  onClick={() => setFetchSymbol(ins.code)}
                >
                  <td className="px-4 py-2 font-mono">{ins.code}</td>
                  <td className="px-4 py-2">{ins.name}</td>
                  <td className="px-4 py-2">{ins.market}</td>
                  <td className="px-4 py-2">{ins.instrument_type}</td>
                  <td className="px-4 py-2 text-muted-foreground">{ins.exchange ?? "—"}</td>
                </tr>
              ))}
          </tbody>
        </table>
        {pagedInstruments.length === 0 && (
          <div className="p-8 text-center text-muted-foreground/70">
            {totalInstruments === 0
              ? '标的池为空 — 点击上方"同步标的池"自动发现'
              : "没有匹配当前搜索或筛选条件的活跃标的"}
          </div>
        )}
        {listedInstrumentTotal > PAGE_SIZE && (
          <div className="flex items-center justify-between border-t px-5 py-3 text-sm">
            <span className="text-muted-foreground">
              第 {instPage}/{instTotalPages} 页 · 共 {listedInstrumentTotal} 条
            </span>
            <div className="flex gap-2">
              <button
                onClick={() => setInstPage((p) => Math.max(1, p - 1))}
                disabled={instPage <= 1}
                className="rounded border border-input px-3 py-1 hover:bg-accent disabled:opacity-40"
              >
                上一页
              </button>
              <button
                onClick={() => setInstPage((p) => Math.min(instTotalPages, p + 1))}
                disabled={instPage >= instTotalPages}
                className="rounded border border-input px-3 py-1 hover:bg-accent disabled:opacity-40"
              >
                下一页
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Cache status table */}
      <div className="bg-card rounded-lg shadow">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b px-5 py-3">
          <div className="flex items-center gap-1">
            <h2 className="text-lg font-semibold">已缓存数据 ({status?.total ?? 0})</h2>
            <InfoHint content={INFO_HINTS.data.cachedData} />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={repairSource}
              onChange={(event) => setRepairSource(event.target.value as "akshare" | "yfinance" | "tushare")}
              className="rounded border border-input bg-card px-2 py-1.5 text-sm text-foreground"
              aria-label="批量换源修复数据源"
            >
              <option value="tushare">备用源: tushare（仅A股股票）</option>
              <option value="akshare">备用源: akshare</option>
              <option value="yfinance">备用源: yfinance</option>
            </select>
            <button
              onClick={() => refetchQuality()}
              disabled={qualityFetching || repairQuality.isPending}
              className="rounded border border-input px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
            >
              {qualityFetching ? "检查中..." : "检查全部缓存"}
            </button>
            <button
              onClick={() => repairQuality.mutate()}
              disabled={
                repairQuality.isPending ||
                !qualityReports?.some((report) => !report.passed)
              }
              className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {repairQuality.isPending
                ? "批量修复中..."
                : `批量换源修复 (${qualityReports?.filter((report) => !report.passed).length ?? 0})`}
            </button>
          </div>
        </div>
        <CacheQualitySummary
          reports={qualityReports}
          expanded={showQualityDetail}
          onToggle={() => setShowQuality((current) => !current)}
          repairResult={repairQuality.data}
          repairError={repairQuality.error as Error | null}
        />
        {!status || status.items.length === 0 ? (
          <div className="p-8 text-center text-muted-foreground/70">缓存为空</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-background text-muted-foreground">
              <tr>
                <th className="px-4 py-2 text-left">标的</th>
                <th className="px-4 py-2 text-right">Bar 数</th>
                <th className="px-4 py-2 text-left">范围</th>
                <th className="px-4 py-2 text-left">来源</th>
                <th className="px-4 py-2 text-right">最新收盘</th>
              </tr>
            </thead>
            <tbody>
              {status.items.map((s) => (
                <tr key={`${s.symbol}-${s.period}-${s.adjust}`} className="border-t">
                  <td className="px-4 py-2 font-mono">{s.symbol}</td>
                  <td className="px-4 py-2 text-right">{s.bar_count}</td>
                  <td className="px-4 py-2 text-muted-foreground">
                    {s.first_date ?? "—"} ~ {s.last_date ?? "—"}
                  </td>
                  <td className="px-4 py-2 text-muted-foreground">{s.source ?? "未记录"}</td>
                  <td className="px-4 py-2 text-right font-mono">
                    {s.last_close ? Number(s.last_close).toFixed(2) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {cacheTotal > PAGE_SIZE && (
          <div className="flex items-center justify-between border-t px-5 py-3 text-sm">
            <span className="text-muted-foreground">
              第 {cachePage}/{cacheTotalPages} 页 · 共 {cacheTotal} 条
            </span>
            <div className="flex gap-2">
              <button
                onClick={() => setCachePage((p) => Math.max(1, p - 1))}
                disabled={cachePage <= 1}
                className="rounded border border-input px-3 py-1 hover:bg-accent disabled:opacity-40"
              >
                上一页
              </button>
              <button
                onClick={() => setCachePage((p) => Math.min(cacheTotalPages, p + 1))}
                disabled={cachePage >= cacheTotalPages}
                className="rounded border border-input px-3 py-1 hover:bg-accent disabled:opacity-40"
              >
                下一页
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function CacheQualitySummary({
  reports,
  expanded,
  onToggle,
  repairResult,
  repairError,
}: {
  reports: QualityReport[] | undefined;
  expanded: boolean;
  onToggle: () => void;
  repairResult: QualityRepairResult | undefined;
  repairError: Error | null;
}) {
  if (!reports) {
    return (
      <div className="border-b px-5 py-3 text-sm text-muted-foreground">
        点击“检查全部缓存”后显示存量数据质量结果。
      </div>
    );
  }
  if (reports.length === 0) {
    return <div className="border-b px-5 py-3 text-sm text-muted-foreground">无缓存数据。</div>;
  }

  const failed = reports.filter((report) => !report.passed);
  return (
    <div className="border-b px-5 py-3">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <span>
          已检查 <strong>{reports.length}</strong> 个缓存标的
        </span>
        <span className="text-success">通过 {reports.length - failed.length}</span>
        <span className={failed.length ? "text-destructive" : "text-muted-foreground"}>
          异常 {failed.length}
        </span>
        {failed.length > 0 && (
          <button onClick={onToggle} className="text-blue-400 hover:underline">
            {expanded ? "收起" : "展开"}异常详情
          </button>
        )}
      </div>
      {repairResult && (
        <p className="mt-2 text-sm text-muted-foreground">
          最近批量修复: 成功 {repairResult.repaired}/{repairResult.total}, 修正 {repairResult.corrected_bars} 根 bar
        </p>
      )}
      {repairError && (
        <p className="mt-2 text-sm text-destructive">批量修复失败: {repairError.message}</p>
      )}
      {expanded && failed.length > 0 && (
        <div className="mt-3 max-h-80 overflow-auto rounded border">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-muted">
              <tr>
                <th className="px-3 py-2 text-left">标的</th>
                <th className="px-3 py-2 text-right">异常数</th>
                <th className="px-3 py-2 text-right">重复</th>
                <th className="px-3 py-2 text-left">数据源</th>
                <th className="px-3 py-2 text-left">异常日期 / 原因</th>
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
                  <td className="px-3 py-1.5">{report.sources.join(",") || "未记录"}</td>
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
    <div className="bg-card rounded-lg shadow p-4">
      <div className="flex items-center gap-1 text-sm text-muted-foreground">
        {label}
        {hint && <InfoHint content={hint} />}
      </div>
      <div className="text-xl font-bold mt-1">{value}</div>
    </div>
  );
}
