import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type OrderCreate } from "../lib/api";

export default function Orders() {
  const qc = useQueryClient();
  const [showForm, setShowForm] = useState(false);

  const { data } = useQuery({
    queryKey: ["orders", "all"],
    queryFn: () => api.getOrders({ limit: 200 }),
  });

  const cancelMut = useMutation({
    mutationFn: (cid: string) => api.cancelOrder(cid),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["orders"] }),
  });

  const placeMut = useMutation({
    mutationFn: (body: OrderCreate) => api.placeOrder(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["orders"] });
      setShowForm(false);
    },
  });

  const orders = data?.items ?? [];

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">订单</h1>
        <button
          onClick={() => setShowForm(!showForm)}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700"
        >
          {showForm ? "取消" : "手工下单"}
        </button>
      </div>

      {placeMut.isError && (
        <div className="bg-red-50 text-red-600 p-3 rounded mb-4 text-sm">
          下单失败: {placeMut.error?.message}
        </div>
      )}

      {showForm && <OrderForm onSubmit={(b) => placeMut.mutate(b)} loading={placeMut.isPending} />}

      {orders.length === 0 ? (
        <div className="text-gray-400 text-center py-12">无订单</div>
      ) : (
        <table className="w-full bg-white rounded-lg shadow text-sm">
          <thead className="bg-gray-100 text-gray-600">
            <tr>
              <th className="px-4 py-2 text-left">标的</th>
              <th className="px-4 py-2 text-left">方向</th>
              <th className="px-4 py-2 text-left">类型</th>
              <th className="px-4 py-2 text-right">数量</th>
              <th className="px-4 py-2 text-right">价格</th>
              <th className="px-4 py-2 text-right">已成交</th>
              <th className="px-4 py-2 text-left">状态</th>
              <th className="px-4 py-2 text-left">策略</th>
              <th className="px-4 py-2 text-left">时间</th>
              <th className="px-4 py-2">操作</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((o) => (
              <tr key={o.client_order_id} className="border-t">
                <td className="px-4 py-2 font-mono">{o.symbol}</td>
                <td className={`px-4 py-2 ${o.side === "buy" ? "text-red-500" : "text-green-500"}`}>
                  {o.side === "buy" ? "买入" : "卖出"}
                </td>
                <td className="px-4 py-2">{o.order_type === "limit" ? "限价" : "市价"}</td>
                <td className="px-4 py-2 text-right">{o.quantity}</td>
                <td className="px-4 py-2 text-right">{o.price ?? "—"}</td>
                <td className="px-4 py-2 text-right">{o.filled_quantity}</td>
                <td className="px-4 py-2">
                  <StatusBadge status={o.status} />
                </td>
                <td className="px-4 py-2 text-gray-400">{o.strategy_id ?? "人工"}</td>
                <td className="px-4 py-2 text-gray-500">{new Date(o.created_at).toLocaleTimeString()}</td>
                <td className="px-4 py-2">
                  {o.is_active && (
                    <button
                      onClick={() => cancelMut.mutate(o.client_order_id)}
                      className="text-red-500 hover:text-red-700 text-xs"
                    >
                      撤单
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    filled: "bg-green-100 text-green-700",
    cancelled: "bg-gray-100 text-gray-500",
    rejected: "bg-red-100 text-red-700",
    unknown: "bg-orange-100 text-orange-700",
  };
  return (
    <span className={`px-2 py-0.5 rounded text-xs ${colors[status] ?? "bg-blue-100 text-blue-700"}`}>
      {status}
    </span>
  );
}

function OrderForm({ onSubmit, loading }: { onSubmit: (b: OrderCreate) => void; loading: boolean }) {
  const [form, setForm] = useState<OrderCreate>({
    symbol: "",
    side: "buy",
    order_type: "limit",
    quantity: "100",
    price: "",
  });

  return (
    <div className="bg-white rounded-lg shadow p-4 mb-4">
      <div className="grid grid-cols-4 gap-3">
        <input
          className="border rounded px-3 py-1.5 text-sm"
          placeholder="标的 (如 510300.SH)"
          value={form.symbol}
          onChange={(e) => setForm({ ...form, symbol: e.target.value })}
        />
        <select
          className="border rounded px-3 py-1.5 text-sm"
          value={form.side}
          onChange={(e) => setForm({ ...form, side: e.target.value })}
        >
          <option value="buy">买入</option>
          <option value="sell">卖出</option>
        </select>
        <select
          className="border rounded px-3 py-1.5 text-sm"
          value={form.order_type}
          onChange={(e) => setForm({ ...form, order_type: e.target.value })}
        >
          <option value="limit">限价</option>
          <option value="market">市价</option>
        </select>
        <input
          className="border rounded px-3 py-1.5 text-sm"
          placeholder="数量"
          value={form.quantity}
          onChange={(e) => setForm({ ...form, quantity: e.target.value })}
        />
        {form.order_type === "limit" && (
          <input
            className="border rounded px-3 py-1.5 text-sm"
            placeholder="价格"
            value={form.price}
            onChange={(e) => setForm({ ...form, price: e.target.value })}
          />
        )}
        <button
          onClick={() => onSubmit(form)}
          disabled={loading || !form.symbol}
          className="bg-blue-600 text-white rounded px-4 py-1.5 text-sm hover:bg-blue-700 disabled:opacity-50"
        >
          {loading ? "提交中..." : "提交"}
        </button>
      </div>
    </div>
  );
}
