import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";

export default function Positions() {
  const [source, setSource] = useState<"local" | "broker">("local");
  const { data } = useQuery({
    queryKey: ["positions", source],
    queryFn: () => api.getPositions(source),
  });

  const positions = data?.items ?? [];

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">持仓</h1>
        <div className="flex gap-2">
          <button
            onClick={() => setSource("local")}
            className={`px-3 py-1.5 rounded text-sm ${
              source === "local" ? "bg-slate-800 text-white" : "bg-white border"
            }`}
          >
            本地持仓
          </button>
          <button
            onClick={() => setSource("broker")}
            className={`px-3 py-1.5 rounded text-sm ${
              source === "broker" ? "bg-slate-800 text-white" : "bg-white border"
            }`}
          >
            券商持仓
          </button>
        </div>
      </div>

      {positions.length === 0 ? (
        <div className="text-gray-400 text-center py-12">无持仓数据</div>
      ) : (
        <table className="w-full bg-white rounded-lg shadow text-sm">
          <thead className="bg-gray-100 text-gray-600">
            <tr>
              <th className="px-4 py-2 text-left">标的</th>
              <th className="px-4 py-2 text-left">方向</th>
              <th className="px-4 py-2 text-right">总持仓</th>
              <th className="px-4 py-2 text-right">可用</th>
              <th className="px-4 py-2 text-right">冻结</th>
              <th className="px-4 py-2 text-right">均价</th>
              <th className="px-4 py-2 text-right">市值</th>
              <th className="px-4 py-2 text-right">浮盈亏</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p, i) => (
              <tr key={i} className="border-t">
                <td className="px-4 py-2 font-mono">{p.symbol}</td>
                <td className="px-4 py-2">{p.position_side === "long" ? "多头" : "空头"}</td>
                <td className="px-4 py-2 text-right">{p.total_quantity}</td>
                <td className="px-4 py-2 text-right">{p.available_quantity}</td>
                <td className="px-4 py-2 text-right">{p.frozen_quantity}</td>
                <td className="px-4 py-2 text-right">{p.average_price}</td>
                <td className="px-4 py-2 text-right">{p.market_value}</td>
                <td className={`px-4 py-2 text-right ${Number(p.unrealized_pnl) >= 0 ? "text-red-500" : "text-green-500"}`}>
                  {p.unrealized_pnl}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
