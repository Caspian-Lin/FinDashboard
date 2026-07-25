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

  const { data: status, isLoading } = useQuery({
    queryKey: ["data-status"],
    queryFn: api.getDataStatus,
  });

  const { data: pool } = useQuery({
    queryKey: ["symbol-pool"],
    queryFn: api.getSymbolPool,
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

  const fetchAll = useMutation({
    mutationFn: () => api.fetchAllData(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["data-status"] }),
  });

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">行情数据</h1>

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
          <h2 className="text-lg font-semibold mb-3">批量拉取</h2>
          <p className="text-sm text-gray-500 mb-2">
            标的池: {pool?.symbols.length ?? 0} 个标的 ·
            回溯 {pool?.fetch_lookback_days ?? 0} 天
          </p>
          <div className="max-h-32 overflow-auto mb-3">
            {pool?.symbols.map((s) => (
              <span
                key={s.code}
                className="inline-block text-xs font-mono bg-gray-100 rounded px-2 py-1 mr-2 mb-1"
              >
                {s.code}
              </span>
            ))}
          </div>
          <button
            onClick={() => fetchAll.mutate()}
            disabled={fetchAll.isPending}
            className="w-full bg-green-600 text-white rounded py-2 text-sm font-medium hover:bg-green-700 disabled:opacity-50"
          >
            {fetchAll.isPending ? "批量拉取中..." : "批量拉取全部"}
          </button>
          {fetchAll.data && (
            <p className="text-sm text-green-600 mt-2">
              成功 {fetchAll.data.success}/{fetchAll.data.total}, 失败{" "}
              {fetchAll.data.failed}
            </p>
          )}
        </div>
      </div>

      {/* Cache status table */}
      <div className="bg-white rounded-lg shadow">
        <div className="flex items-center justify-between px-5 py-3 border-b">
          <h2 className="text-lg font-semibold">缓存状态</h2>
          <button
            onClick={() =>
              queryClient.invalidateQueries({ queryKey: ["data-status"] })
            }
            className="text-sm text-blue-600 hover:underline"
          >
            刷新
          </button>
        </div>
        {isLoading ? (
          <div className="p-8 text-center text-gray-400">加载中...</div>
        ) : !status || status.length === 0 ? (
          <div className="p-8 text-center text-gray-400">缓存为空</div>
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
