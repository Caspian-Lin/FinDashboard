import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
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
import { api, type BacktestResult } from "../lib/api";

export default function Backtest() {
  const [strategy, setStrategy] = useState("ma_cross");
  const [symbols, setSymbols] = useState("510300.SH");
  const [start, setStart] = useState("2024-01-01");
  const [end, setEnd] = useState(new Date().toISOString().slice(0, 10));
  const [capital, setCapital] = useState("100000");
  const [shortWindow, setShortWindow] = useState("5");
  const [longWindow, setLongWindow] = useState("20");

  const { data: strategies } = useQuery({
    queryKey: ["strategies"],
    queryFn: api.getStrategies,
  });

  const runBacktest = useMutation({
    mutationFn: () =>
      api.runBacktest({
        strategy,
        symbols: symbols.split(",").map((s) => s.trim()).filter(Boolean),
        start,
        end,
        capital,
        params:
          strategy === "ma_cross"
            ? {
                short_window: Number(shortWindow),
                long_window: Number(longWindow),
              }
            : {},
      }),
  });

  const result: BacktestResult | undefined = runBacktest.data;
  const m = result?.metrics;

  return (
    <div>
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
          <div className="col-span-2">
            <label className="block text-sm text-gray-600 mb-1">
              标的(逗号分隔)
            </label>
            <input
              value={symbols}
              onChange={(e) => setSymbols(e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm font-mono"
            />
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
            <label className="block text-sm text-gray-600 mb-1">
              初始资金(¥)
            </label>
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
                <label className="block text-sm text-gray-600 mb-1">
                  短期均线
                </label>
                <input
                  type="number"
                  value={shortWindow}
                  onChange={(e) => setShortWindow(e.target.value)}
                  className="w-full border rounded px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label className="block text-sm text-gray-600 mb-1">
                  长期均线
                </label>
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
        <button
          onClick={() => runBacktest.mutate()}
          disabled={runBacktest.isPending}
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
          {/* Metrics cards */}
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

          {/* Equity curve */}
          {result.equity_curve.length > 0 && (
            <div className="bg-white rounded-lg shadow p-5 mb-6">
              <h2 className="text-lg font-semibold mb-4">权益曲线</h2>
              <ResponsiveContainer width="100%" height={350}>
                <LineChart data={result.equity_curve}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
                  <XAxis
                    dataKey="date"
                    tick={{ fontSize: 11 }}
                    interval="preserveStartEnd"
                  />
                  <YAxis tick={{ fontSize: 11 }} />
                  <Tooltip
                    formatter={(v) =>
                      `¥${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
                    }
                  />
                  <Legend />
                  <Line
                    type="monotone"
                    dataKey="equity"
                    stroke="#2563eb"
                    name="策略"
                    dot={false}
                    strokeWidth={2}
                  />
                  {result.equity_curve.some((p) => p.benchmark !== null) && (
                    <Line
                      type="monotone"
                      dataKey="benchmark"
                      stroke="#9ca3af"
                      name="买入持有"
                      dot={false}
                      strokeWidth={1.5}
                      strokeDasharray="5 5"
                    />
                  )}
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* Fills table */}
          {result.fills.length > 0 && (
            <div className="bg-white rounded-lg shadow">
              <div className="px-5 py-3 border-b">
                <h2 className="text-lg font-semibold">
                  交易明细 ({result.fills.length})
                </h2>
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
                      <td
                        className={`px-4 py-2 ${
                          f.side === "buy" ? "text-red-500" : "text-green-500"
                        }`}
                      >
                        {f.side === "buy" ? "买入" : "卖出"}
                      </td>
                      <td className="px-4 py-2 text-right">{f.quantity}</td>
                      <td className="px-4 py-2 text-right font-mono">
                        {Number(f.price).toFixed(2)}
                      </td>
                      <td className="px-4 py-2 text-right text-gray-500">
                        {Number(f.commission).toFixed(2)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
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
    positive === undefined
      ? ""
      : positive
        ? "text-green-600"
        : "text-red-600";
  return (
    <div className="bg-white rounded-lg shadow p-4">
      <div className="text-gray-500 text-sm">{label}</div>
      <div className={`text-xl font-bold mt-1 ${color}`}>{value}</div>
    </div>
  );
}
