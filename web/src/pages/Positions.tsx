import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { PageHeader } from "../components/ui/page-header";
import { PageContainer } from "../components/ui/page-container";
import { EmptyState } from "../components/ui/states";
import { Button } from "../components/ui/button";
import { useT } from "@/i18n";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";

export default function Positions() {
  const { t } = useT();
  const [source, setSource] = useState<"local" | "broker">("local");
  const { data } = useQuery({
    queryKey: ["positions", source],
    queryFn: () => api.getPositions(source),
    refetchInterval: 5000,
  });

  const positions = data?.items ?? [];

  return (
    <PageContainer>
      <PageHeader
        title={t("positions.title")}
        description={t("positions.description")}
        actions={
          <div className="flex gap-2" role="group" aria-label={t("positions.sourceGroup")}>
            {(["local", "broker"] as const).map((s) => (
              <Button
                key={s}
                variant={source === s ? "secondary" : "outline"}
                size="sm"
                onClick={() => setSource(s)}
                aria-pressed={source === s}
              >
                {s === "local" ? t("positions.local") : t("positions.broker")}
              </Button>
            ))}
          </div>
        }
      />

      {positions.length === 0 ? (
        <EmptyState title={t("positions.emptyTitle")} description={t("positions.emptyDesc")} />
      ) : (
        <div className="overflow-hidden rounded-lg border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("common.symbol")}</TableHead>
                <TableHead>{t("common.direction")}</TableHead>
                <TableHead className="text-right">{t("positions.totalQty")}</TableHead>
                <TableHead className="text-right">{t("positions.available")}</TableHead>
                <TableHead className="text-right">{t("positions.frozen")}</TableHead>
                <TableHead className="text-right">{t("positions.avgPrice")}</TableHead>
                <TableHead className="text-right">{t("positions.marketValue")}</TableHead>
                <TableHead className="text-right">{t("positions.unrealizedPnl")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {positions.map((p, i) => (
                <TableRow key={i}>
                  <TableCell className="font-mono">{p.symbol}</TableCell>
                  <TableCell>{p.position_side === "long" ? t("common.long") : t("common.short")}</TableCell>
                  <TableCell className="text-right">{p.total_quantity}</TableCell>
                  <TableCell className="text-right">{p.available_quantity}</TableCell>
                  <TableCell className="text-right">{p.frozen_quantity}</TableCell>
                  <TableCell className="text-right">{p.average_price}</TableCell>
                  <TableCell className="text-right">{p.market_value}</TableCell>
                  <TableCell className={`text-right ${Number(p.unrealized_pnl) >= 0 ? "text-up" : "text-down"}`}>
                    {p.unrealized_pnl}
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
