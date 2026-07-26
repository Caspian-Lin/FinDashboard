import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";

const BULK_PHASE_LABELS: Record<string, string> = {
  starting: "准备任务",
  checking_cache: "检查缓存",
  fetching: "请求行情",
  reading_cache: "读取缓存",
  writing_cache: "写入缓存",
};

export default function Data() {
  const queryClient = useQueryClient();
  const [fetchSymbol, setFetchSymbol] = useState("510300.SH");
  const [fetchStart, setFetchStart] = useState("2024-01-01");
  const [fetchEnd, setFetchEnd] = useState(
    new Date().toISOString().slice(0, 10),
  );
  const [marketFilter, setMarketFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [searchQuery, setSearchQuery] = useState("");

  // bulk download form
  const [dlMarket, setDlMarket] = useState("a_share");
  const [dlType, setDlType] = useState("");
  const [dlStart, setDlStart] = useState("2015-01-01");

  const { data: status } = useQuery({
    queryKey: ["data-status"],
    queryFn: () => api.getDataStatusPage(),
  });

  const { data: instruments } = useQuery({
    queryKey: ["instruments", marketFilter, typeFilter],
    queryFn: () =>
      api.getInstruments({
        market: marketFilter || undefined,
        instrument_type: typeFilter || undefined,
        limit: 500,
      }),
  });

  const { data: searchResults } = useQuery({
    queryKey: ["instrument-search", searchQuery],
    queryFn: () => api.searchInstruments(searchQuery),
    enabled: searchQuery.length >= 2,
  });

  const { data: bulkStatus } = useQuery({
    queryKey: ["bulk-download-status"],
    queryFn: api.getBulkDownloadStatus,
    refetchInterval: 2000,
  });

  const sync = useMutation({
    mutationFn: () => api.syncUniverse(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["instruments"] });
    },
  });

  const fetchOne = useMutation({
    mutationFn: (body: {
      symbol: string;
      start: string;
      end: string;
      adjust?: string;
    }) => api.fetchData(body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["data-status"] }),
  });

  const startDownload = useMutation({
    mutationFn: () =>
      api.startBulkDownload({
        market: dlMarket,
        instrument_type: dlType || undefined,
        start: dlStart,
      }),
    onSuccess: (data) => {
      queryClient.setQueryData(["bulk-download-status"], data);
    },
  });

  const totalInstruments = instruments?.total ?? 0;
  const isDownloading = startDownload.isPending || bulkStatus?.status === "running";
  const phaseLabel = BULK_PHASE_LABELS[bulkStatus?.phase ?? ""];

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">行情数据</h1>

      {/* Stats row */}
      <div className="grid grid-cols-4 gap-4 mb-6">
        <StatCard label="数据库标的数" value={String(totalInstruments)} />
        <StatCard label="缓存标的数" value={String(status?.total ?? 0)} />
        <StatCard
          label="A股"
          value={String(
            instruments?.items.filter(
              (i) => i.market === "a_share" && i.instrument_type === "stock",
            ).length ?? 0,
          )}
        />
        <StatCard
          label="ETF"
          value={String(
            instruments?.items.filter((i) => i.instrument_type === "etf")
              .length ?? 0,
          )}
        />
      </div>

      {/* Sync + Single fetch */}
      <div className="grid grid-cols-2 gap-6 mb-6">
        {/* Universe Sync */}
        <div className="bg-white rounded-lg shadow p-5">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-lg font-semibold">标的池同步</h2>
            {totalInstruments > 0 && (
              <span className="text-sm text-gray-400">已同步 {totalInstruments} 条</span>
            )}
          </div>
          <p className="text-sm text-gray-500 mb-3">
            从 akshare 自动发现全市场 A 股(~5500) + ETF(~1600),写入数据库。
          </p>
          <button
            onClick={() => sync.mutate()}
            disabled={sync.isPending}
            className="w-full bg-indigo-600 text-white rounded py-2 text-sm font-medium hover:bg-indigo-700 disabled:opacity-50"
          >
            {sync.isPending ? "同步中..." : totalInstruments === 0 ? "同步标的池" : "刷新标的池"}
          </button>
          {sync.data && (
            <p className="text-sm text-green-600 mt-2">
              同步完成: {sync.data.total} 条标的
            </p>
          )}
          {sync.error && (
            <p className="text-sm text-red-600 mt-2">
              {(sync.error as Error).message}
            </p>
          )}
        </div>

        {/* Single fetch */}
        <div className="bg-white rounded-lg shadow p-5">
          <h2 className="text-lg font-semibold mb-3">单标的拉取</h2>
          <div className="space-y-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">标的</label>
              <input
                value={fetchSymbol}
                onChange={(e) => setFetchSymbol(e.target.value)}
                className="w-full border rounded px-3 py-2 text-sm font-mono"
                placeholder="510300.SH"
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-sm text-gray-600 mb-1">开始</label>
                <input
                  type="date"
                  value={fetchStart}
                  onChange={(e) => setFetchStart(e.target.value)}
                  className="w-full border rounded px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label className="block text-sm text-gray-600 mb-1">结束</label>
                <input
                  type="date"
                  value={fetchEnd}
                  onChange={(e) => setFetchEnd(e.target.value)}
                  className="w-full border rounded px-3 py-2 text-sm"
                />
              </div>
            </div>
            <button
              onClick={() =>
                fetchOne.mutate({ symbol: fetchSymbol, start: fetchStart, end: fetchEnd })
              }
              disabled={fetchOne.isPending}
              className="w-full bg-blue-600 text-white rounded py-2 text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
            >
              {fetchOne.isPending ? "拉取中..." : "拉取"}
            </button>
            {fetchOne.data && (
              <p className="text-sm text-green-600">
                已获取 {fetchOne.data.bar_count} 根日线
              </p>
            )}
            {fetchOne.error && (
              <p className="text-sm text-red-600">
                {(fetchOne.error as Error).message}
              </p>
            )}
          </div>
        </div>
      </div>

      {/* Bulk Download */}
      <div className="bg-white rounded-lg shadow p-5 mb-6">
        <h2 className="text-lg font-semibold mb-4">批量拉取</h2>
        <div className="grid grid-cols-4 gap-4 mb-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">市场</label>
            <select
              value={dlMarket}
              onChange={(e) => setDlMarket(e.target.value)}
              disabled={isDownloading}
              className="w-full border rounded px-3 py-2 text-sm"
            >
              <option value="a_share">A股</option>
              <option value="hk">港股</option>
              <option value="us">美股</option>
            </select>
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">类型</label>
            <select
              value={dlType}
              onChange={(e) => setDlType(e.target.value)}
              disabled={isDownloading}
              className="w-full border rounded px-3 py-2 text-sm"
            >
              <option value="">全部</option>
              <option value="stock">股票</option>
              <option value="etf">ETF</option>
              <option value="index">指数</option>
            </select>
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">起始日期</label>
            <input
              type="date"
              value={dlStart}
              onChange={(e) => setDlStart(e.target.value)}
              disabled={isDownloading}
              className="w-full border rounded px-3 py-2 text-sm"
            />
          </div>
          <div className="flex items-end">
            <button
              onClick={() => startDownload.mutate()}
              disabled={isDownloading || startDownload.isPending}
              className="w-full bg-green-600 text-white rounded py-2 text-sm font-medium hover:bg-green-700 disabled:opacity-50"
            >
              {isDownloading ? "拉取中..." : "开始批量拉取"}
            </button>
          </div>
        </div>

        {/* Progress bar */}
        {isDownloading && bulkStatus && (
          <div className="mt-4" aria-live="polite">
            <div className="flex justify-between text-sm text-gray-600 mb-1">
              <span>进度: {bulkStatus.done} / {bulkStatus.total}</span>
              <span>{bulkStatus.total > 0 ? `${(bulkStatus.done * 100 / bulkStatus.total).toFixed(1)}%` : ""}</span>
            </div>
            <div
              className="w-full bg-gray-200 rounded-full h-3 overflow-hidden"
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
              <p className="mt-2 text-xs text-gray-500 font-mono">
                {phaseLabel ?? "处理中"}
                {bulkStatus.current_symbol ? ` · ${bulkStatus.current_symbol}` : ""}
              </p>
            )}
          </div>
        )}

        {/* Download result */}
        {bulkStatus?.status === "done" && (
          <p className="mt-3 text-sm text-green-600">
            完成: 成功 {bulkStatus.success} / {bulkStatus.total}, 失败 {bulkStatus.failed}
          </p>
        )}
        {bulkStatus?.status === "error" && (
          <p className="mt-3 text-sm text-red-600">错误: {bulkStatus.error}</p>
        )}
        {startDownload.error && (
          <p className="mt-3 text-sm text-red-600">
            {(startDownload.error as Error).message}
          </p>
        )}
      </div>

      {/* Instruments table */}
      <div className="bg-white rounded-lg shadow mb-6">
        <div className="flex items-center justify-between px-5 py-3 border-b">
          <h2 className="text-lg font-semibold">标的列表 ({totalInstruments})</h2>
          <div className="flex gap-3">
            <input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="搜索代码/名称"
              className="border rounded px-3 py-1 text-sm w-40"
            />
            <select
              value={marketFilter}
              onChange={(e) => setMarketFilter(e.target.value)}
              className="border rounded px-2 py-1 text-sm"
            >
              <option value="">全部市场</option>
              <option value="a_share">A股</option>
              <option value="hk">港股</option>
              <option value="us">美股</option>
            </select>
            <select
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
              className="border rounded px-2 py-1 text-sm"
            >
              <option value="">全部类型</option>
              <option value="stock">股票</option>
              <option value="etf">ETF</option>
            </select>
          </div>
        </div>
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-gray-600">
            <tr>
              <th className="px-4 py-2 text-left">代码</th>
              <th className="px-4 py-2 text-left">名称</th>
              <th className="px-4 py-2 text-left">市场</th>
              <th className="px-4 py-2 text-left">类型</th>
              <th className="px-4 py-2 text-left">交易所</th>
            </tr>
          </thead>
          <tbody>
            {(searchQuery.length >= 2
              ? (searchResults ?? [])
              : (instruments?.items ?? [])
            )
              .slice(0, 200)
              .map((ins) => (
                <tr
                  key={ins.code}
                  className="border-t hover:bg-blue-50 cursor-pointer"
                  onClick={() => setFetchSymbol(ins.code)}
                >
                  <td className="px-4 py-2 font-mono">{ins.code}</td>
                  <td className="px-4 py-2">{ins.name}</td>
                  <td className="px-4 py-2">{ins.market}</td>
                  <td className="px-4 py-2">{ins.instrument_type}</td>
                  <td className="px-4 py-2 text-gray-500">{ins.exchange ?? "—"}</td>
                </tr>
              ))}
          </tbody>
        </table>
        {totalInstruments === 0 && searchQuery.length < 2 && (
          <div className="p-8 text-center text-gray-400">
            标的池为空 — 点击上方"同步标的池"按钮自动发现
          </div>
        )}
      </div>

      {/* Cache status table */}
      <div className="bg-white rounded-lg shadow">
        <div className="px-5 py-3 border-b">
          <h2 className="text-lg font-semibold">已缓存数据 ({status?.total ?? 0})</h2>
        </div>
        {!status || status.items.length === 0 ? (
          <div className="p-8 text-center text-gray-400">缓存为空</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-600">
              <tr>
                <th className="px-4 py-2 text-left">标的</th>
                <th className="px-4 py-2 text-right">Bar 数</th>
                <th className="px-4 py-2 text-left">范围</th>
                <th className="px-4 py-2 text-right">最新收盘</th>
              </tr>
            </thead>
            <tbody>
              {status.items.map((s) => (
                <tr key={`${s.symbol}-${s.period}-${s.adjust}`} className="border-t">
                  <td className="px-4 py-2 font-mono">{s.symbol}</td>
                  <td className="px-4 py-2 text-right">{s.bar_count}</td>
                  <td className="px-4 py-2 text-gray-500">
                    {s.first_date ?? "—"} ~ {s.last_date ?? "—"}
                  </td>
                  <td className="px-4 py-2 text-right font-mono">
                    {s.last_close ? Number(s.last_close).toFixed(2) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {status && status.total > status.items.length && (
          <p className="px-5 py-3 border-t text-xs text-gray-500">
            当前显示前 {status.items.length} 条，共 {status.total} 条缓存记录
          </p>
        )}
      </div>
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-white rounded-lg shadow p-4">
      <div className="text-gray-500 text-sm">{label}</div>
      <div className="text-xl font-bold mt-1">{value}</div>
    </div>
  );
}
