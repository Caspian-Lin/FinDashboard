import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";

export default function Fills() {
  const { data } = useQuery({ queryKey: ["fills"], queryFn: () => api.getFills(200) });
  const fills = data?.items ?? [];

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">成交</h1>
      {fills.length === 0 ? (
        <div className="text-muted-foreground/70 text-center py-12">无成交记录</div>
      ) : (
        <table className="w-full bg-card rounded-lg shadow text-sm">
          <thead className="bg-secondary text-muted-foreground">
            <tr>
              <th className="px-4 py-2 text-left">成交编号</th>
              <th className="px-4 py-2 text-left">标的</th>
              <th className="px-4 py-2 text-left">方向</th>
              <th className="px-4 py-2 text-right">数量</th>
              <th className="px-4 py-2 text-right">价格</th>
              <th className="px-4 py-2 text-right">金额</th>
              <th className="px-4 py-2 text-right">手续费</th>
              <th className="px-4 py-2 text-left">时间</th>
            </tr>
          </thead>
          <tbody>
            {fills.map((f) => (
              <tr key={f.fill_id} className="border-t">
                <td className="px-4 py-2 font-mono text-xs text-muted-foreground">{f.fill_id}</td>
                <td className="px-4 py-2 font-mono">{f.symbol}</td>
                <td className={`px-4 py-2 ${f.side === "buy" ? "text-red-500" : "text-green-500"}`}>
                  {f.side === "buy" ? "买入" : "卖出"}
                </td>
                <td className="px-4 py-2 text-right">{f.quantity}</td>
                <td className="px-4 py-2 text-right">{f.price}</td>
                <td className="px-4 py-2 text-right">
                  {(Number(f.quantity) * Number(f.price)).toFixed(2)}
                </td>
                <td className="px-4 py-2 text-right text-muted-foreground">{f.commission}</td>
                <td className="px-4 py-2 text-muted-foreground">{new Date(f.filled_at).toLocaleTimeString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
