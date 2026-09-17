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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { useT } from "@/i18n";

export default function Orders() {
  const { t } = useT();
  const qc = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [cancelTarget, setCancelTarget] = useState<Order | null>(null);
  const [placeConfirm, setPlaceConfirm] = useState<OrderCreate | null>(null);

  const { data } = useQuery({
    queryKey: ["orders", "all"],
    queryFn: () => api.getOrders({ limit: 200 }),
    refetchInterval: 5000,
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
    <div className="mx-auto w-full max-w-7xl space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-bold tracking-tight text-foreground">{t("orders.title")}</h1>
            <span className="flex items-center gap-1 rounded bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning">
              <AlertTriangle className="h-3 w-3" />
              {t("orders.liveControlled")}
            </span>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {t("orders.description")}
          </p>
        </div>
        <Button onClick={() => setShowForm(!showForm)} variant={showForm ? "outline" : "default"}>
          {showForm ? t("common.cancel") : t("orders.manualOrder")}
        </Button>
      </div>

      {placeMut.isError && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>{t("orders.placeFailed")}: {placeMut.error?.message}</AlertDescription>
        </Alert>
      )}

      {showForm && (
        <OrderForm
          onSubmit={(b) => setPlaceConfirm(b)}
          loading={placeMut.isPending}
        />
      )}

      {orders.length === 0 ? (
        <div className="py-12 text-center text-muted-foreground">{t("orders.noOrders")}</div>
      ) : (
        <div className="overflow-x-auto scrollbar-thin">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("common.symbol")}</TableHead>
                <TableHead>{t("common.direction")}</TableHead>
                <TableHead>{t("common.type")}</TableHead>
                <TableHead className="text-right">{t("common.quantity")}</TableHead>
                <TableHead className="text-right">{t("common.price")}</TableHead>
                <TableHead className="text-right">{t("orders.filled")}</TableHead>
                <TableHead>{t("common.status")}</TableHead>
                <TableHead>{t("orders.strategy")}</TableHead>
                <TableHead>{t("common.time")}</TableHead>
                <TableHead>{t("common.actions")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {orders.map((o) => (
                <TableRow key={o.client_order_id}>
                  <TableCell className="font-mono">{o.symbol}</TableCell>
                  <TableCell className={o.side === "buy" ? "text-up" : "text-down"}>
                    {o.side === "buy" ? t("common.buy") : t("common.sell")}
                  </TableCell>
                  <TableCell>{o.order_type === "limit" ? t("orders.limit") : t("orders.market")}</TableCell>
                  <TableCell className="text-right tabular-nums">{o.quantity}</TableCell>
                  <TableCell className="text-right tabular-nums">{o.price ?? "—"}</TableCell>
                  <TableCell className="text-right tabular-nums">{o.filled_quantity}</TableCell>
                  <TableCell>
                    <OrderStatusBadge status={o.status} />
                  </TableCell>
                  <TableCell className="text-muted-foreground">{o.strategy_id ?? t("orders.manual")}</TableCell>
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
                        {t("orders.cancel")}
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
            <DialogTitle>{t("orders.cancelConfirmTitle")}</DialogTitle>
            <DialogDescription>
              {t("orders.cancelConfirmDesc")}
            </DialogDescription>
          </DialogHeader>
          {cancelTarget && (
            <div className="space-y-2 rounded-lg bg-muted/50 p-3 text-sm">
              <div className="flex justify-between">
                <span className="text-muted-foreground">{t("common.symbol")}</span>
                <span className="font-mono">{cancelTarget.symbol}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">{t("common.direction")}</span>
                <span className={cancelTarget.side === "buy" ? "text-up" : "text-down"}>
                  {cancelTarget.side === "buy" ? t("common.buy") : t("common.sell")}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">{t("common.quantity")}</span>
                <span className="tabular-nums">{cancelTarget.quantity}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">{t("orders.orderId")}</span>
                <span className="font-mono text-xs">{cancelTarget.client_order_id}</span>
              </div>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setCancelTarget(null)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="destructive"
              onClick={() => cancelTarget && cancelMut.mutate(cancelTarget.client_order_id)}
              disabled={cancelMut.isPending}
            >
              {cancelMut.isPending ? t("orders.cancelling") : t("orders.cancelConfirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Place order confirmation */}
      <Dialog open={!!placeConfirm} onOpenChange={(v) => !v && setPlaceConfirm(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("orders.placeConfirmTitle")}</DialogTitle>
            <DialogDescription>
              {t("orders.placeConfirmDesc")}
            </DialogDescription>
          </DialogHeader>
          {placeConfirm && (
            <div className="space-y-2 rounded-lg bg-muted/50 p-3 text-sm">
              <div className="flex justify-between">
                <span className="text-muted-foreground">{t("common.symbol")}</span>
                <span className="font-mono">{placeConfirm.symbol}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">{t("common.direction")}</span>
                <span className={placeConfirm.side === "buy" ? "text-up" : "text-down"}>
                  {placeConfirm.side === "buy" ? t("common.buy") : t("common.sell")}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">{t("common.type")}</span>
                <span>{placeConfirm.order_type === "limit" ? t("orders.limit") : t("orders.market")}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">{t("common.quantity")}</span>
                <span className="tabular-nums">{placeConfirm.quantity}</span>
              </div>
              {placeConfirm.order_type === "limit" && placeConfirm.price && (
                <div className="flex justify-between">
                  <span className="text-muted-foreground">{t("common.price")}</span>
                  <span className="tabular-nums">{placeConfirm.price}</span>
                </div>
              )}
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setPlaceConfirm(null)}>
              {t("common.cancel")}
            </Button>
            <Button
              onClick={() => placeConfirm && placeMut.mutate(placeConfirm)}
              disabled={placeMut.isPending}
            >
              {placeMut.isPending ? t("common.submitting") : t("orders.placeConfirm")}
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
  const { t } = useT();
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
        <div className="col-span-2 space-y-1 md:col-span-1">
          <Label htmlFor="order-symbol">{t("common.symbol")}</Label>
          <Input
            id="order-symbol"
            placeholder={t("orders.symbolPlaceholder")}
            value={form.symbol}
            onChange={(e) => setForm({ ...form, symbol: e.target.value })}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="order-side">{t("common.direction")}</Label>
          <select
            id="order-side"
            className="h-9 w-full rounded-md border border-input bg-transparent px-3 py-1.5 text-sm"
            value={form.side}
            onChange={(e) => setForm({ ...form, side: e.target.value as "buy" | "sell" })}
          >
            <option value="buy">{t("common.buy")}</option>
            <option value="sell">{t("common.sell")}</option>
          </select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="order-type">{t("common.type")}</Label>
          <select
            id="order-type"
            className="h-9 w-full rounded-md border border-input bg-transparent px-3 py-1.5 text-sm"
            value={form.order_type}
            onChange={(e) => setForm({ ...form, order_type: e.target.value as "limit" | "market" })}
          >
            <option value="limit">{t("orders.limit")}</option>
            <option value="market">{t("orders.market")}</option>
          </select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="order-quantity">{t("common.quantity")}</Label>
          <Input
            id="order-quantity"
            placeholder={t("common.quantity")}
            value={form.quantity}
            onChange={(e) => setForm({ ...form, quantity: e.target.value })}
          />
        </div>
        {form.order_type === "limit" && (
          <div className="space-y-1">
            <Label htmlFor="order-price">{t("common.price")}</Label>
            <Input
              id="order-price"
              placeholder={t("common.price")}
              value={form.price}
              onChange={(e) => setForm({ ...form, price: e.target.value })}
            />
          </div>
        )}
        <Button
          onClick={() => onSubmit(form)}
          disabled={loading || !form.symbol}
          className="col-span-2 self-end md:col-span-1"
        >
          {loading ? t("common.submitting") : t("common.submit")}
        </Button>
      </div>
    </div>
  );
}
