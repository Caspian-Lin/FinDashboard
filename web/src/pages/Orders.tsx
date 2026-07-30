import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { api, type OrderCreate, type Order } from "../lib/api";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table";
import { Alert, AlertDescription } from "@/components/ui/alert";

export default function Orders() {
  const qc = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [cancelTarget, setCancelTarget] = useState<Order | null>(null);
  const [placeConfirm, setPlaceConfirm] = useState<OrderCreate | null>(null);

  const { data } = useQuery({
    queryKey: ["orders", "all"],
    queryFn: () => api.getOrders({ limit: 200 }),
  });

  const cancelMut = useMutation({
    mutationFn: (cid: string) => api.cancelOrder(cid),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["orders"] });
      setCancelTarget(null);
    },
  });

  const placeMut = useMutation({
    mutationFn: (body: OrderCreate) => api.placeOrder(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["orders"] });
      setShowForm(false);
      setPlaceConfirm(null);
    },
  });

  const orders = data?.items ?? [];

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h1 className="text-2xl font-bold">订单</h1>
          <span className="flex items-center gap-1 rounded bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning">
            <AlertTriangle className="h-3 w-3" />
            实盘受控区域
          </span>
        </div>
        <Button onClick={() => setShowForm(!showForm)} variant={showForm ? "outline" : "default"}>
          {showForm ? "取消" : "手工下单"}
        </Button>
      </div>

      <Alert variant="warning" className="mb-4">
        <AlertTriangle className="h-4 w-4" />
        <AlertDescription>
          此页面的下单和撤单操作将影响真实账户。所有操作由交易内核鉴权和风控。
        </AlertDescription>
      </Alert>

      {placeMut.isError && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>下单失败: {placeMut.error?.message}</AlertDescription>
        </Alert>
      )}

      {showForm && (
        <OrderForm
          onSubmit={(b) => setPlaceConfirm(b)}
          loading={placeMut.isPending}
        />
      )}

      {orders.length === 0 ? (
        <div className="py-12 text-center text-muted-foreground">无订单</div>
      ) : (
        <div className="overflow-x-auto scrollbar-thin">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>标的</TableHead>
                <TableHead>方向</TableHead>
                <TableHead>类型</TableHead>
                <TableHead className="text-right">数量</TableHead>
                <TableHead className="text-right">价格</TableHead>
                <TableHead className="text-right">已成交</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>策略</TableHead>
                <TableHead>时间</TableHead>
                <TableHead>操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {orders.map((o) => (
                <TableRow key={o.client_order_id}>
                  <TableCell className="font-mono">{o.symbol}</TableCell>
                  <TableCell className={o.side === "buy" ? "text-red-500" : "text-green-500"}>
                    {o.side === "buy" ? "买入" : "卖出"}
                  </TableCell>
                  <TableCell>{o.order_type === "limit" ? "限价" : "市价"}</TableCell>
                  <TableCell className="text-right tabular-nums">{o.quantity}</TableCell>
                  <TableCell className="text-right tabular-nums">{o.price ?? "—"}</TableCell>
                  <TableCell className="text-right tabular-nums">{o.filled_quantity}</TableCell>
                  <TableCell>
                    <OrderStatusBadge status={o.status} />
                  </TableCell>
                  <TableCell className="text-muted-foreground">{o.strategy_id ?? "人工"}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {new Date(o.created_at).toLocaleTimeString()}
                  </TableCell>
                  <TableCell>
                    {o.is_active && (
                      <Button
                        size="sm"
                        variant="destructive"
                        onClick={() => setCancelTarget(o)}
                      >
                        撤单
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {/* Cancel confirmation */}
      <Dialog open={!!cancelTarget} onOpenChange={(v) => !v && setCancelTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>确认撤单</DialogTitle>
            <DialogDescription>
              此操作将撤销以下订单，由交易内核执行。不可撤销已完成订单。
            </DialogDescription>
          </DialogHeader>
          {cancelTarget && (
            <div className="space-y-2 rounded-lg bg-muted/50 p-3 text-sm">
              <div className="flex justify-between">
                <span className="text-muted-foreground">标的</span>
                <span className="font-mono">{cancelTarget.symbol}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">方向</span>
                <span className={cancelTarget.side === "buy" ? "text-red-500" : "text-green-500"}>
                  {cancelTarget.side === "buy" ? "买入" : "卖出"}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">数量</span>
                <span className="tabular-nums">{cancelTarget.quantity}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">订单 ID</span>
                <span className="font-mono text-xs">{cancelTarget.client_order_id}</span>
              </div>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setCancelTarget(null)}>
              取消
            </Button>
            <Button
              variant="destructive"
              onClick={() => cancelTarget && cancelMut.mutate(cancelTarget.client_order_id)}
              disabled={cancelMut.isPending}
            >
              {cancelMut.isPending ? "撤单中..." : "确认撤单"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Place order confirmation */}
      <Dialog open={!!placeConfirm} onOpenChange={(v) => !v && setPlaceConfirm(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>确认下单</DialogTitle>
            <DialogDescription>
              此操作将向券商发送真实订单。请仔细核对以下信息。
            </DialogDescription>
          </DialogHeader>
          {placeConfirm && (
            <div className="space-y-2 rounded-lg bg-muted/50 p-3 text-sm">
              <div className="flex justify-between">
                <span className="text-muted-foreground">标的</span>
                <span className="font-mono">{placeConfirm.symbol}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">方向</span>
                <span className={placeConfirm.side === "buy" ? "text-red-500" : "text-green-500"}>
                  {placeConfirm.side === "buy" ? "买入" : "卖出"}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">类型</span>
                <span>{placeConfirm.order_type === "limit" ? "限价" : "市价"}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">数量</span>
                <span className="tabular-nums">{placeConfirm.quantity}</span>
              </div>
              {placeConfirm.order_type === "limit" && placeConfirm.price && (
                <div className="flex justify-between">
                  <span className="text-muted-foreground">价格</span>
                  <span className="tabular-nums">{placeConfirm.price}</span>
                </div>
              )}
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setPlaceConfirm(null)}>
              取消
            </Button>
            <Button
              onClick={() => placeConfirm && placeMut.mutate(placeConfirm)}
              disabled={placeMut.isPending}
            >
              {placeMut.isPending ? "提交中..." : "确认下单"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function OrderStatusBadge({ status }: { status: string }) {
  const variantMap: Record<string, "default" | "success" | "destructive" | "warning" | "secondary"> = {
    filled: "success",
    cancelled: "secondary",
    canceled: "secondary",
    rejected: "destructive",
    unknown: "warning",
    working: "default",
    new: "default",
    partial: "warning",
  };
  return <Badge variant={variantMap[status] ?? "default"}>{status}</Badge>;
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
    <div className="mb-4 rounded-lg border border-border bg-card p-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-6">
        <input
          className="col-span-2 rounded-md border border-input bg-transparent px-3 py-1.5 text-sm placeholder:text-muted-foreground focus-visible:ring-1 focus-visible:ring-ring md:col-span-1"
          placeholder="标的 (如 510300.SH)"
          value={form.symbol}
          onChange={(e) => setForm({ ...form, symbol: e.target.value })}
        />
        <select
          className="rounded-md border border-input bg-transparent px-3 py-1.5 text-sm"
          value={form.side}
          onChange={(e) => setForm({ ...form, side: e.target.value as "buy" | "sell" })}
        >
          <option value="buy">买入</option>
          <option value="sell">卖出</option>
        </select>
        <select
          className="rounded-md border border-input bg-transparent px-3 py-1.5 text-sm"
          value={form.order_type}
          onChange={(e) => setForm({ ...form, order_type: e.target.value as "limit" | "market" })}
        >
          <option value="limit">限价</option>
          <option value="market">市价</option>
        </select>
        <input
          className="rounded-md border border-input bg-transparent px-3 py-1.5 text-sm placeholder:text-muted-foreground focus-visible:ring-1 focus-visible:ring-ring"
          placeholder="数量"
          value={form.quantity}
          onChange={(e) => setForm({ ...form, quantity: e.target.value })}
        />
        {form.order_type === "limit" && (
          <input
            className="rounded-md border border-input bg-transparent px-3 py-1.5 text-sm placeholder:text-muted-foreground focus-visible:ring-1 focus-visible:ring-ring"
            placeholder="价格"
            value={form.price}
            onChange={(e) => setForm({ ...form, price: e.target.value })}
          />
        )}
        <Button
          onClick={() => onSubmit(form)}
          disabled={loading || !form.symbol}
          className="col-span-2 md:col-span-1"
        >
          {loading ? "提交中..." : "提交"}
        </Button>
      </div>
    </div>
  );
}
