import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";

export default function Data() {
  const queryClient = useQueryClient();
  const [fetchSymbol, setFetchSymbol] = useState("510300.SH");
  const [fetchStart, setFetchStart] = useState("2024-01-01");
  const [fetchEnd, setFetchEnd] = useState(
    new Date().toISOString().slice(0, 10),
  );
  const [marketFilter, setMarketFilter] = useState<string>("");
  const [typeFilter, setTypeFilter] = useState<string>("");
  const [searchQuery, setSearchQuery] = useState("");

  const { data: status } = useQuery({
    queryKey: ["data-status"],
    queryFn: api.getDataStatus,
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

  const fetchOne = useMutation({
    mutationFn: (body: {
      symbol: string;
      start: string;
      end: string;
      adjust?: string;
    }) => api.fetchData(body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["data-status"] }),
  });

  const totalInstruments = instruments?.total ?? 0;

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">行情数据</h1>

      {/* Stats row */}
      <div className="grid grid-cols-4 gap-4 mb-6">
        <div className="bg-white rounded-lg shadow p-4">
          <div className="text-gray-500 text-sm">数据库标的数</div>
          <div className="text-xl font-bold mt-1">{totalInstruments}</div>
        </div>
        <div className="bg-white rounded-lg shadow p-4">
          <div className="text-gray-500 text-sm">缓存标的数</div>
          <div className="text-xl font-bold mt-1">{status?.length ?? 0}</div>
        </div>
        <div className="bg-white rounded-lg shadow p-4">
          <div className="text-gray-500 text-sm">A股</div>
          <div className="text-xl font-bold mt-1">
            {instruments?.items.filter((i) => i.market === "a_share" && i.instrument_type === "stock").length ?? 0}
          </div>
        </div>
        <div className="bg-white rounded-lg shadow p-4">
          <div className="text-gray-500 text-sm">ETF</div>
          <div className="text-xl font-bold mt-1">
            {instruments?.items.filter((i) => i.instrument_type === "etf").length ?? 0}
          </div>
        </div>
      </div>

      {/* Fetch section */}
      <div className="grid grid-cols-2 gap-6 mb-6">
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
                fetchOne.mutate({
                  symbol: fetchSymbol,
                  start: fetchStart,
                  end: fetchEnd,
                })
              }
              disabled={fetchOne.isPending}
              className="w-full bg-blue-600 text-white rounded py-2 text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
            >
              {fetchOne.isPending ? "拉取中..." : "拉取"}
            </button>
            {fetchOne.data && (
              <p className="text-sm text-green-600">
                已获取 {fetchOne.data.bar_count} 根日线 (
                {fetchOne.data.first_date} ~ {fetchOne.data.last_date})
              </p>
            )}
            {fetchOne.error && (
              <p className="text-sm text-red-600">
                {(fetchOne.error as Error).message}
              </p>
            )}
          </div>
        </div>

        <div className="bg-white rounded-lg shadow p-5">
          <h2 className="text-lg font-semibold mb-3">标的搜索</h2>
          <input
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="输入代码或名称(至少2字)"
            className="w-full border rounded px-3 py-2 text-sm mb-3"
          />
          <div className="max-h-64 overflow-auto">
            {searchResults?.map((item) => (
              <div
                key={item.code}
                className="flex items-center justify-between py-1.5 px-2 hover:bg-gray-50 rounded cursor-pointer"
                onClick={() => setFetchSymbol(item.code)}
              >
                <span className="font-mono text-sm">{item.code}</span>
                <span className="text-sm text-gray-600">{item.name}</span>
              </div>
            ))}
            {searchQuery.length >= 2 && !searchResults?.length && (
              <p className="text-sm text-gray-400 text-center py-4">无结果</p>
            )}
            {searchQuery.length < 2 && (
              <p className="text-sm text-gray-400 text-center py-4">
                输入代码或名称搜索
              </p>
            )}
          </div>
        </div>
      </div>

      {/* Instruments table */}
      <div className="bg-white rounded-lg shadow mb-6">
        <div className="flex items-center justify-between px-5 py-3 border-b">
          <h2 className="text-lg font-semibold">标的列表 ({totalInstruments})</h2>
          <div className="flex gap-3">
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
              <option value="index">指数</option>
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
            {(instruments?.items ?? []).slice(0, 200).map((ins) => (
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
        {totalInstruments > 200 && (
          <div className="px-5 py-2 text-sm text-gray-400 text-center">
            显示前 200 条,共 {totalInstruments} 条 — 使用搜索缩小范围
          </div>
        )}
      </div>

      {/* Cache status table */}
      <div className="bg-white rounded-lg shadow">
        <div className="flex items-center justify-between px-5 py-3 border-b">
          <h2 className="text-lg font-semibold">已缓存数据</h2>
          <button
            onClick={() =>
              queryClient.invalidateQueries({ queryKey: ["data-status"] })
            }
            className="text-sm text-blue-600 hover:underline"
          >
            刷新
          </button>
        </div>
        {!status || status.length === 0 ? (
          <div className="p-8 text-center text-gray-400">
            缓存为空 — 使用 CLI <code className="bg-gray-100 px-1 rounded">finboard data bulk-download</code> 批量拉取
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-600">
              <tr>
                <th className="px-4 py-2 text-left">标的</th>
                <th className="px-4 py-2 text-left">周期</th>
                <th className="px-4 py-2 text-left">复权</th>
                <th className="px-4 py-2 text-right">Bar 数</th>
                <th className="px-4 py-2 text-left">起始</th>
                <th className="px-4 py-2 text-left">结束</th>
                <th className="px-4 py-2 text-right">最新收盘</th>
              </tr>
            </thead>
            <tbody>
              {status.map((s) => (
                <tr key={`${s.symbol}-${s.period}-${s.adjust}`} className="border-t">
                  <td className="px-4 py-2 font-mono">{s.symbol}</td>
                  <td className="px-4 py-2">{s.period}</td>
                  <td className="px-4 py-2">{s.adjust}</td>
                  <td className="px-4 py-2 text-right">{s.bar_count}</td>
                  <td className="px-4 py-2 text-gray-500">{s.first_date ?? "—"}</td>
                  <td className="px-4 py-2 text-gray-500">{s.last_date ?? "—"}</td>
                  <td className="px-4 py-2 text-right font-mono">
                    {s.last_close ? Number(s.last_close).toFixed(2) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
