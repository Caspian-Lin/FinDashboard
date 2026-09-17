import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { PageHeader } from "../components/ui/page-header";
import { PageContainer } from "../components/ui/page-container";
import { EmptyState } from "../components/ui/states";
import { useT } from "@/i18n";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";

export default function Fills() {
  const { t } = useT();
  const { data } = useQuery({
    queryKey: ["fills"],
    queryFn: () => api.getFills(200),
    refetchInterval: 5000,
  });
  const fills = data?.items ?? [];

  return (
    <PageContainer>
      <PageHeader title={t("fills.title")} description={t("fills.description")} />
      {fills.length === 0 ? (
        <EmptyState title={t("fills.emptyTitle")} description={t("fills.emptyDesc")} />
      ) : (
        <div className="overflow-hidden rounded-lg border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("fills.fillId")}</TableHead>
                <TableHead>{t("common.symbol")}</TableHead>
                <TableHead>{t("common.direction")}</TableHead>
                <TableHead className="text-right">{t("common.quantity")}</TableHead>
                <TableHead className="text-right">{t("common.price")}</TableHead>
                <TableHead className="text-right">{t("common.amount")}</TableHead>
                <TableHead className="text-right">{t("fills.commission")}</TableHead>
                <TableHead>{t("common.time")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {fills.map((f) => (
                <TableRow key={f.fill_id}>
                  <TableCell className="font-mono text-xs text-muted-foreground">{f.fill_id}</TableCell>
                  <TableCell className="font-mono">{f.symbol}</TableCell>
                  <TableCell className={f.side === "buy" ? "text-up" : "text-down"}>
                    {f.side === "buy" ? t("common.buy") : t("common.sell")}
                  </TableCell>
                  <TableCell className="text-right">{f.quantity}</TableCell>
                  <TableCell className="text-right">{f.price}</TableCell>
                  <TableCell className="text-right">
                    {(Number(f.quantity) * Number(f.price)).toFixed(2)}
                  </TableCell>
                  <TableCell className="text-right text-muted-foreground">{f.commission}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {new Date(f.filled_at).toLocaleTimeString()}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </PageContainer>
  );
}
