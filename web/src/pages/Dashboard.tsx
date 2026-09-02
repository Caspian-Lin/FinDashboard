import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { PageHeader } from "../components/ui/page-header";
import { PageContainer } from "../components/ui/page-container";
import { StatCard } from "../components/ui/stat-card";
import { useT } from "@/i18n";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";

export default function Dashboard() {
  const { t } = useT();
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
        title={t("dashboard.title")}
        description={t("dashboard.description")}
      />
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label={t("dashboard.kernelStatus")}
          value={health?.kernel_ready ? t("dashboard.ready") : t("dashboard.notReady")}
          trend={health?.kernel_ready ? { value: t("dashboard.online"), positive: true } : { value: t("dashboard.offline"), positive: false }}
        />
        <StatCard
          label={t("dashboard.killSwitch")}
          value={health?.kill_switch_level ?? "—"}
          trend={
            health?.kill_switch_level === "off"
              ? { value: t("dashboard.ksNotTriggered"), positive: true }
              : { value: t("dashboard.ksTriggered"), positive: false }
          }
        />
        <StatCard label={t("dashboard.activeOrders")} value={String(activeOrders.length)} />
        <StatCard label={t("dashboard.totalAsset")} value={account ? `¥${Number(account.total_asset).toLocaleString()}` : "—"} />
        <StatCard label={t("dashboard.availableCash")} value={account ? `¥${Number(account.cash).toLocaleString()}` : "—"} />
        <StatCard label={t("dashboard.frozenCash")} value={account ? `¥${Number(account.frozen_cash).toLocaleString()}` : "—"} />
        <StatCard label={t("dashboard.broker")} value={account?.broker_kind ?? "—"} />
        <StatCard label={t("dashboard.account")} value={account?.account_id ?? "—"} />
      </div>

      {activeOrders.length > 0 && (
        <section aria-label={t("dashboard.activeOrders")}>
          <h2 className="mb-3 text-lg font-semibold">{t("dashboard.activeOrders")}</h2>
          <div className="overflow-hidden rounded-lg border border-border bg-card">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("common.symbol")}</TableHead>
                  <TableHead>{t("common.direction")}</TableHead>
                  <TableHead className="text-right">{t("common.quantity")}</TableHead>
                  <TableHead className="text-right">{t("common.price")}</TableHead>
                  <TableHead>{t("common.status")}</TableHead>
                  <TableHead>{t("dashboard.orderTime")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {activeOrders.map((o) => (
                  <TableRow key={o.client_order_id}>
                    <TableCell className="font-mono">{o.symbol}</TableCell>
                    <TableCell className={o.side === "buy" ? "text-up" : "text-down"}>
                      {o.side === "buy" ? t("common.buy") : t("common.sell")}
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
