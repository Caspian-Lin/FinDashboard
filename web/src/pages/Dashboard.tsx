import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { PageHeader } from "../components/ui/page-header";
import { PageContainer } from "../components/ui/page-container";
import { StatCard } from "../components/ui/stat-card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";

export default function Dashboard() {
  const { data: health } = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 5000,
  });
  const { data: account } = useQuery({
    queryKey: ["account"],
    queryFn: api.getAccount,
    refetchInterval: 5000,
  });
  const { data: orders } = useQuery({
    queryKey: ["orders", "active"],
    queryFn: () => api.getOrders({ limit: 500 }),
    refetchInterval: 5000,
  });

  const activeOrders = orders?.items.filter((o) => o.is_active) ?? [];

  return (
    <PageContainer>
      <PageHeader
        title="仪表盘"
        description="账户、内核与活动订单概览;数据每 5 秒刷新。"
      />
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="内核状态"
          value={health?.kernel_ready ? "就绪" : "未就绪"}
          trend={health?.kernel_ready ? { value: "在线", positive: true } : { value: "离线", positive: false }}
        />
        <StatCard
          label="Kill Switch"
          value={health?.kill_switch_level ?? "—"}
          trend={
            health?.kill_switch_level === "off"
              ? { value: "未触发", positive: true }
              : { value: "已触发", positive: false }
          }
        />
        <StatCard label="活动订单" value={String(activeOrders.length)} />
        <StatCard label="总资产" value={account ? `¥${Number(account.total_asset).toLocaleString()}` : "—"} />
        <StatCard label="可用资金" value={account ? `¥${Number(account.cash).toLocaleString()}` : "—"} />
        <StatCard label="冻结资金" value={account ? `¥${Number(account.frozen_cash).toLocaleString()}` : "—"} />
        <StatCard label="券商" value={account?.broker_kind ?? "—"} />
        <StatCard label="账户" value={account?.account_id ?? "—"} />
      </div>

      {activeOrders.length > 0 && (
        <section aria-label="活动订单">
          <h2 className="mb-3 text-lg font-semibold">活动订单</h2>
          <div className="overflow-hidden rounded-lg border border-border bg-card">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>标的</TableHead>
                  <TableHead>方向</TableHead>
                  <TableHead className="text-right">数量</TableHead>
                  <TableHead className="text-right">价格</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>下单时间</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {activeOrders.map((o) => (
                  <TableRow key={o.client_order_id}>
                    <TableCell className="font-mono">{o.symbol}</TableCell>
                    <TableCell className={o.side === "buy" ? "text-up" : "text-down"}>
                      {o.side === "buy" ? "买入" : "卖出"}
                    </TableCell>
                    <TableCell className="text-right">{o.quantity}</TableCell>
                    <TableCell className="text-right">{o.price ?? "—"}</TableCell>
                    <TableCell>{o.status}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {new Date(o.created_at).toLocaleTimeString()}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </section>
      )}
    </PageContainer>
  );
}
