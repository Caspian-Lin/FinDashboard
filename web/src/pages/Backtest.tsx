import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import { api, type BacktestResult, type BacktestHistoryItem } from "../lib/api";

const MARKETS = [
  { value: "", label: "全部市场" },
  { value: "a_share", label: "A股" },
  { value: "hk", label: "港股" },
  { value: "us", label: "美股" },
];
const TYPES = [
  { value: "", label: "全部类型" },
  { value: "stock", label: "股票" },
  { value: "etf", label: "ETF" },
];

export default function Backtest() {
  const qc = useQueryClient();
  const [strategy, setStrategy] = useState("ma_cross");
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>(["510300.SH"]);
  const [start, setStart] = useState("2024-01-01");
  const [end, setEnd] = useState(new Date().toISOString().slice(0, 10));
  const [capital, setCapital] = useState("100000");
  const [shortWindow, setShortWindow] = useState("5");
  const [longWindow, setLongWindow] = useState("20");

  // 标的搜索
  const [searchQuery, setSearchQuery] = useState("");
  const [marketFilter, setMarketFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");

  // 结果视图: 当前结果或历史记录
  const [activeResult, setActiveResult] = useState<BacktestResult | null>(null);
  const [activeHistoryId, setActiveHistoryId] = useState<number | null>(null);

  const { data: strategies } = useQuery({
    queryKey: ["strategies"],
    queryFn: api.getStrategies,
  });

  const { data: history } = useQuery({
    queryKey: ["backtest-history"],
    queryFn: () => api.getBacktestHistory(30),
    refetchInterval: false,
  });

  // 搜索标的
  const { data: searchResults } = useQuery({
    queryKey: ["instrument-search", searchQuery],
    queryFn: () => api.searchInstruments(searchQuery),
    enabled: searchQuery.length >= 2,
  });

  // 筛选标的(无搜索时按市场/类型拉取)
  const { data: filterData } = useQuery({
    queryKey: ["instruments-filter", marketFilter, typeFilter],
    queryFn: () =>
      api.getInstruments({
        market: marketFilter || undefined,
        instrument_type: typeFilter || undefined,
        limit: 500,
      }),
    enabled: searchQuery.length < 2,
  });

  const candidateList = useMemo(() => {
    if (searchQuery.length >= 2) return searchResults ?? [];
    return filterData?.items ?? [];
  }, [searchQuery.length, searchResults, filterData]);

  const runBacktest = useMutation({
    mutationFn: () =>
      api.runBacktest({
        strategy,
        symbols: selectedSymbols,
        start,
        end,
        capital,
        params:
          strategy === "ma_cross"
            ? { short_window: Number(shortWindow), long_window: Number(longWindow) }
            : {},
      }),
    onSuccess: (data) => {
      setActiveResult(data);
      setActiveHistoryId(data.run_id);
      qc.invalidateQueries({ queryKey: ["backtest-history"] });
    },
  });

  const loadHistory = useMutation({
    mutationFn: (id: number) => api.getBacktestHistoryDetail(id),
    onSuccess: (detail) => {
      setActiveResult({
        metrics: detail.metrics,
        equity_curve: detail.equity_curve,
        fills: detail.fills,
        summary: detail.summary,
        run_id: detail.id,
      });
      setActiveHistoryId(detail.id);
      setStrategy(detail.strategy);
      setSelectedSymbols(detail.symbols);
      setStart(detail.start);
      setEnd(detail.end);
      setCapital(detail.capital);
    },
  });

  const deleteHistory = useMutation({
    mutationFn: (id: number) => api.deleteBacktestHistory(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["backtest-history"] }),
  });

  const toggleSymbol = (code: string) => {
    setSelectedSymbols((prev) =>
      prev.includes(code) ? prev.filter((s) => s !== code) : [...prev, code],
    );
  };

  const selectAllCandidates = () => {
    const codes = candidateList.map((c) => c.code);
    setSelectedSymbols((prev) => Array.from(new Set([...prev, ...codes])));
  };

  const result = activeResult;
  const m = result?.metrics;

  return (
    <div className="flex gap-6">
      {/* Main column */}
      <div className="flex-1 min-w-0">
        <h1 className="text-2xl font-bold mb-6">回测</h1>

        {/* Config form */}
        <div className="bg-white rounded-lg shadow p-5 mb-6">
          <h2 className="text-lg font-semibold mb-4">回测配置</h2>
          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="block text-sm text-gray-600 mb-1">策略</label>
              <select
                value={strategy}
                onChange={(e) => setStrategy(e.target.value)}
                className="w-full border rounded px-3 py-2 text-sm"
              >
                {strategies?.map((s) => (
                  <option key={s.kind} value={s.kind}>
                    {s.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">开始</label>
              <input
                type="date"
                value={start}
                onChange={(e) => setStart(e.target.value)}
                className="w-full border rounded px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">结束</label>
              <input
                type="date"
                value={end}
                onChange={(e) => setEnd(e.target.value)}
                className="w-full border rounded px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">初始资金(¥)</label>
              <input
                type="number"
                value={capital}
                onChange={(e) => setCapital(e.target.value)}
                className="w-full border rounded px-3 py-2 text-sm"
              />
            </div>
            {strategy === "ma_cross" && (
              <>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">短期均线</label>
                  <input
                    type="number"
                    value={shortWindow}
                    onChange={(e) => setShortWindow(e.target.value)}
                    className="w-full border rounded px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">长期均线</label>
                  <input
                    type="number"
                    value={longWindow}
                    onChange={(e) => setLongWindow(e.target.value)}
                    className="w-full border rounded px-3 py-2 text-sm"
                  />
                </div>
              </>
            )}
          </div>

          {/* Symbol selector */}
          <SymbolSelector
            searchQuery={searchQuery}
            setSearchQuery={setSearchQuery}
            marketFilter={marketFilter}
            setMarketFilter={setMarketFilter}
            typeFilter={typeFilter}
            setTypeFilter={setTypeFilter}
            candidates={candidateList}
            selectedSymbols={selectedSymbols}
            toggleSymbol={toggleSymbol}
            selectAllCandidates={selectAllCandidates}
            clearSelection={() => setSelectedSymbols([])}
            onSymbolsLoaded={(codes) => setSelectedSymbols(codes)}
          />

          <button
            onClick={() => runBacktest.mutate()}
            disabled={runBacktest.isPending || selectedSymbols.length === 0}
            className="mt-4 bg-blue-600 text-white rounded px-6 py-2 text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
          >
            {runBacktest.isPending ? "回测中..." : "运行回测"}
          </button>
          {runBacktest.error && (
            <p className="mt-2 text-sm text-red-600">
              {(runBacktest.error as Error).message}
            </p>
          )}
        </div>

        {/* Results */}
        {m && (
          <>
            <div className="grid grid-cols-4 gap-4 mb-6">
              <MetricCard label="总收益" value={`${(m.total_return * 100).toFixed(2)}%`} positive={m.total_return >= 0} />
              <MetricCard label="年化" value={`${(m.annualized_return * 100).toFixed(2)}%`} positive={m.annualized_return >= 0} />
              <MetricCard label="夏普" value={m.sharpe_ratio.toFixed(2)} />
              <MetricCard label="最大回撤" value={`${(m.max_drawdown * 100).toFixed(2)}%`} positive={false} />
              <MetricCard label="胜率" value={`${(m.win_rate * 100).toFixed(1)}%`} />
              <MetricCard label="交易次数" value={String(m.trade_count)} />
              <MetricCard label="换手率" value={m.turnover.toFixed(2)} />
              <MetricCard label="超额收益" value={`${(m.excess_return * 100).toFixed(2)}%`} positive={m.excess_return >= 0} />
            </div>

            {result.equity_curve.length > 0 && (
              <div className="bg-white rounded-lg shadow p-5 mb-6">
                <h2 className="text-lg font-semibold mb-4">权益曲线</h2>
                <ResponsiveContainer width="100%" height={350}>
                  <LineChart data={result.equity_curve}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
                    <XAxis dataKey="date" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                    <YAxis tick={{ fontSize: 11 }} />
                    <Tooltip
                      formatter={(v) =>
                        `¥${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
                      }
                    />
                    <Legend />
                    <Line type="monotone" dataKey="equity" stroke="#2563eb" name="策略" dot={false} strokeWidth={2} />
                    {result.equity_curve.some((p) => p.benchmark !== null) && (
                      <Line type="monotone" dataKey="benchmark" stroke="#9ca3af" name="买入持有" dot={false} strokeWidth={1.5} strokeDasharray="5 5" />
                    )}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}

            {result.fills.length > 0 && (
              <div className="bg-white rounded-lg shadow">
                <div className="px-5 py-3 border-b">
                  <h2 className="text-lg font-semibold">交易明细 ({result.fills.length})</h2>
                </div>
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 text-gray-600">
                    <tr>
                      <th className="px-4 py-2 text-left">日期</th>
                      <th className="px-4 py-2 text-left">标的</th>
                      <th className="px-4 py-2 text-left">方向</th>
                      <th className="px-4 py-2 text-right">数量</th>
                      <th className="px-4 py-2 text-right">价格</th>
                      <th className="px-4 py-2 text-right">佣金</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.fills.map((f, i) => (
                      <tr key={i} className="border-t">
                        <td className="px-4 py-2 text-gray-500">{f.date}</td>
                        <td className="px-4 py-2 font-mono">{f.symbol}</td>
                        <td className={`px-4 py-2 ${f.side === "buy" ? "text-red-500" : "text-green-500"}`}>
                          {f.side === "buy" ? "买入" : "卖出"}
                        </td>
                        <td className="px-4 py-2 text-right">{f.quantity}</td>
                        <td className="px-4 py-2 text-right font-mono">{Number(f.price).toFixed(2)}</td>
                        <td className="px-4 py-2 text-right text-gray-500">{Number(f.commission).toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>

      {/* History sidebar */}
      <div className="w-72 shrink-0">
        <h2 className="text-sm font-semibold text-gray-700 mb-3">回测历史</h2>
        <div className="space-y-2">
          {history && history.length === 0 && (
            <p className="text-xs text-gray-400">暂无历史记录</p>
          )}
          {history?.map((h) => (
            <HistoryCard
              key={h.id}
              item={h}
              active={h.id === activeHistoryId}
              loading={loadHistory.isPending && loadHistory.variables === h.id}
              onClick={() => loadHistory.mutate(h.id)}
              onDelete={() => deleteHistory.mutate(h.id)}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

// ---- Symbol selector with search + filter + watchlist ----
function SymbolSelector({
  searchQuery,
  setSearchQuery,
  marketFilter,
  setMarketFilter,
  typeFilter,
  setTypeFilter,
  candidates,
  selectedSymbols,
  toggleSymbol,
  selectAllCandidates,
  clearSelection,
  onSymbolsLoaded,
}: {
  searchQuery: string;
  setSearchQuery: (v: string) => void;
  marketFilter: string;
  setMarketFilter: (v: string) => void;
  typeFilter: string;
  setTypeFilter: (v: string) => void;
  candidates: { code: string; name: string; market: string; instrument_type: string }[];
  selectedSymbols: string[];
  toggleSymbol: (code: string) => void;
  selectAllCandidates: () => void;
  clearSelection: () => void;
  onSymbolsLoaded: (codes: string[]) => void;
}) {
  const qc = useQueryClient();
  const [showWatchlist, setShowWatchlist] = useState(false);
  const [newWlName, setNewWlName] = useState("");

  const { data: watchlists } = useQuery({
    queryKey: ["watchlists"],
    queryFn: api.getWatchlists,
    enabled: showWatchlist,
  });

  const createWl = useMutation({
    mutationFn: (name: string) =>
      api.createWatchlist({ name }),
    onSuccess: (wl) => {
      if (selectedSymbols.length > 0) {
        return api.addWatchlistSymbols(wl.id, selectedSymbols);
      }
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["watchlists"] });
      setNewWlName("");
    },
  });

  const addToWl = useMutation({
    mutationFn: ({ id, codes }: { id: number; codes: string[] }) =>
      api.addWatchlistSymbols(id, codes),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlists"] }),
  });

  const loadWl = useMutation({
    mutationFn: (id: number) => api.getWatchlist(id),
    onSuccess: (detail) => onSymbolsLoaded(detail.symbols),
  });

  return (
    <div className="mt-4 border rounded-lg p-4 bg-gray-50">
      <div className="flex items-center justify-between mb-3">
        <label className="text-sm font-medium text-gray-700">
          标的选择 <span className="text-gray-400 font-normal">({selectedSymbols.length} 个)</span>
        </label>
        <div className="flex gap-2">
          <button
            onClick={selectAllCandidates}
            className="text-xs text-blue-600 hover:underline"
          >
            全选结果
          </button>
          {selectedSymbols.length > 0 && (
            <button onClick={clearSelection} className="text-xs text-gray-500 hover:underline">
              清空
            </button>
          )}
          <button
            onClick={() => setShowWatchlist((v) => !v)}
            className="text-xs text-blue-600 hover:underline"
          >
            标的组
          </button>
        </div>
      </div>

      {/* Search + filter row */}
      <div className="flex gap-2 mb-2">
        <input
          placeholder="搜索代码/名称(至少2字符)..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="flex-1 border rounded px-3 py-1.5 text-sm"
        />
        <select
          value={marketFilter}
          onChange={(e) => setMarketFilter(e.target.value)}
          className="border rounded px-2 py-1.5 text-sm"
        >
          {MARKETS.map((m) => (
            <option key={m.value} value={m.value}>{m.label}</option>
          ))}
        </select>
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          className="border rounded px-2 py-1.5 text-sm"
        >
          {TYPES.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
      </div>

      {/* Candidate list */}
      <div className="max-h-40 overflow-y-auto border rounded bg-white">
        {candidates.length === 0 && (
          <p className="text-xs text-gray-400 p-3 text-center">
            {searchQuery.length >= 2 ? "无匹配结果" : "输入搜索或选择筛选条件"}
          </p>
        )}
        {candidates.slice(0, 200).map((c) => {
          const checked = selectedSymbols.includes(c.code);
          return (
            <label
              key={c.code}
              className="flex items-center gap-2 px-3 py-1.5 hover:bg-blue-50 cursor-pointer text-sm"
            >
              <input type="checkbox" checked={checked} onChange={() => toggleSymbol(c.code)} />
              <span className="font-mono text-xs">{c.code}</span>
              <span className="text-gray-500 text-xs truncate">{c.name}</span>
            </label>
          );
        })}
      </div>

      {/* Selected symbols chips */}
      {selectedSymbols.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-2">
          {selectedSymbols.map((code) => (
            <span
              key={code}
              className="inline-flex items-center gap-1 bg-blue-100 text-blue-700 rounded px-2 py-0.5 text-xs font-mono"
            >
              {code}
              <button onClick={() => toggleSymbol(code)} className="text-blue-400 hover:text-blue-600">
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      {/* Watchlist panel */}
      {showWatchlist && (
        <div className="mt-3 border-t pt-3 space-y-2">
          <div className="flex gap-1">
            <input
              placeholder="新标的组名称..."
              value={newWlName}
              onChange={(e) => setNewWlName(e.target.value)}
              className="flex-1 border rounded px-2 py-1 text-sm"
            />
            <button
              onClick={() => newWlName.trim() && createWl.mutate(newWlName.trim())}
              disabled={!newWlName.trim() || createWl.isPending}
              className="text-xs bg-green-600 text-white rounded px-3 py-1 hover:bg-green-700 disabled:opacity-50"
            >
              存当前选择
            </button>
          </div>
          {watchlists?.map((wl) => (
            <div key={wl.id} className="flex items-center justify-between text-sm bg-white border rounded px-2 py-1">
              <span className="truncate">
                {wl.name} <span className="text-gray-400">({wl.item_count})</span>
              </span>
              <div className="flex gap-2 shrink-0">
                <button
                  onClick={() => loadWl.mutate(wl.id)}
                  className="text-xs text-blue-600 hover:underline"
                >
                  载入
                </button>
                <button
                  onClick={() =>
                    selectedSymbols.length > 0 &&
                    addToWl.mutate({ id: wl.id, codes: selectedSymbols })
                  }
                  disabled={selectedSymbols.length === 0}
                  className="text-xs text-green-600 hover:underline disabled:opacity-30"
                >
                  追加
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---- History card ----
function HistoryCard({
  item,
  active,
  loading,
  onClick,
  onDelete,
}: {
  item: BacktestHistoryItem;
  active: boolean;
  loading: boolean;
  onClick: () => void;
  onDelete: () => void;
}) {
  const ret = item.metrics?.total_return;
  return (
    <div
      className={`border rounded-lg p-3 cursor-pointer transition ${
        active ? "border-blue-500 bg-blue-50" : "bg-white hover:border-gray-400"
      }`}
      onClick={onClick}
    >
      <div className="flex items-start justify-between">
        <div className="min-w-0">
          <div className="text-xs font-mono text-gray-700 truncate">{item.strategy}</div>
          <div className="text-xs text-gray-400 mt-0.5">
            {item.symbols.length} 标的 · {item.start.slice(0, 10)} ~ {item.end.slice(0, 10)}
          </div>
          {ret !== undefined && (
            <div className={`text-sm font-bold mt-1 ${ret >= 0 ? "text-green-600" : "text-red-600"}`}>
              {(ret * 100).toFixed(2)}%
            </div>
          )}
        </div>
        <div className="flex flex-col items-end gap-1 shrink-0">
          <span className="text-xs text-gray-300">
            {new Date(item.created_at).toLocaleDateString("zh-CN", { month: "short", day: "numeric" })}
          </span>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
            className="text-xs text-gray-300 hover:text-red-500"
          >
            删除
          </button>
        </div>
      </div>
      {loading && <div className="text-xs text-blue-400 mt-1">加载中...</div>}
    </div>
  );
}

function MetricCard({
  label,
  value,
  positive,
}: {
  label: string;
  value: string;
  positive?: boolean;
}) {
  const color =
    positive === undefined ? "" : positive ? "text-green-600" : "text-red-600";
  return (
    <div className="bg-white rounded-lg shadow p-4">
      <div className="text-gray-500 text-sm">{label}</div>
      <div className={`text-xl font-bold mt-1 ${color}`}>{value}</div>
    </div>
  );
}
