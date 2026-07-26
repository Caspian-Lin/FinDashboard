import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";

export default function Dashboard() {
  const { data: health } = useQuery({ queryKey: ["health"], queryFn: api.health });
  const { data: account } = useQuery({ queryKey: ["account"], queryFn: api.getAccount });
  const { data: orders } = useQuery({
    queryKey: ["orders", "active"],
    queryFn: () => api.getOrders({ limit: 500 }),
  });

  const activeOrders = orders?.items.filter((o) => o.is_active) ?? [];

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">仪表盘</h1>
      <div className="grid grid-cols-4 gap-4">
        <StatCard label="内核状态" value={health?.kernel_ready ? "就绪" : "未就绪"} color={health?.kernel_ready ? "green" : "red"} />
        <StatCard label="Kill Switch" value={health?.kill_switch_level ?? "—"} color={health?.kill_switch_level === "off" ? "green" : "red"} />
        <StatCard label="活动订单" value={String(activeOrders.length)} />
        <StatCard label="总资产" value={account ? `¥${Number(account.total_asset).toLocaleString()}` : "—"} />
        <StatCard label="可用资金" value={account ? `¥${Number(account.cash).toLocaleString()}` : "—"} />
        <StatCard label="冻结资金" value={account ? `¥${Number(account.frozen_cash).toLocaleString()}` : "—"} />
        <StatCard label="券商" value={account?.broker_kind ?? "—"} />
        <StatCard label="账户" value={account?.account_id ?? "—"} />
      </div>

      {activeOrders.length > 0 && (
        <div className="mt-8">
          <h2 className="text-lg font-semibold mb-3">活动订单</h2>
          <table className="w-full bg-white rounded-lg shadow text-sm">
            <thead className="bg-gray-100 text-gray-600">
              <tr>
                <th className="px-4 py-2 text-left">标的</th>
                <th className="px-4 py-2 text-left">方向</th>
                <th className="px-4 py-2 text-right">数量</th>
                <th className="px-4 py-2 text-right">价格</th>
                <th className="px-4 py-2 text-left">状态</th>
                <th className="px-4 py-2 text-left">下单时间</th>
              </tr>
            </thead>
            <tbody>
              {activeOrders.map((o) => (
                <tr key={o.client_order_id} className="border-t">
                  <td className="px-4 py-2 font-mono">{o.symbol}</td>
                  <td className={`px-4 py-2 ${o.side === "buy" ? "text-red-500" : "text-green-500"}`}>
                    {o.side === "buy" ? "买入" : "卖出"}
                  </td>
                  <td className="px-4 py-2 text-right">{o.quantity}</td>
                  <td className="px-4 py-2 text-right">{o.price ?? "—"}</td>
                  <td className="px-4 py-2">{o.status}</td>
                  <td className="px-4 py-2 text-gray-500">{new Date(o.created_at).toLocaleTimeString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function StatCard({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="bg-white rounded-lg shadow p-4">
      <div className="text-gray-500 text-sm">{label}</div>
      <div className={`text-xl font-bold mt-1 ${color === "green" ? "text-green-600" : color === "red" ? "text-red-600" : ""}`}>
        {value}
      </div>
    </div>
  );
}
