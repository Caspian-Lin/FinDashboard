import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { PageHeader } from "../components/ui/page-header";
import { PageContainer } from "../components/ui/page-container";
import { EmptyState } from "../components/ui/states";
import { Button } from "../components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";

export default function Positions() {
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
        title="持仓"
        description="本地持仓用于实时响应与风控;券商持仓为最终真实来源。"
        actions={
          <div className="flex gap-2" role="group" aria-label="持仓数据来源">
            {(["local", "broker"] as const).map((s) => (
              <Button
                key={s}
                variant={source === s ? "secondary" : "outline"}
                size="sm"
                onClick={() => setSource(s)}
                aria-pressed={source === s}
              >
                {s === "local" ? "本地持仓" : "券商持仓"}
              </Button>
            ))}
          </div>
        }
      />

      {positions.length === 0 ? (
        <EmptyState title="无持仓数据" description="切换数据来源或等待持仓查询完成。" />
      ) : (
        <div className="overflow-hidden rounded-lg border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>标的</TableHead>
                <TableHead>方向</TableHead>
                <TableHead className="text-right">总持仓</TableHead>
                <TableHead className="text-right">可用</TableHead>
                <TableHead className="text-right">冻结</TableHead>
                <TableHead className="text-right">均价</TableHead>
                <TableHead className="text-right">市值</TableHead>
                <TableHead className="text-right">浮盈亏</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {positions.map((p, i) => (
                <TableRow key={i}>
                  <TableCell className="font-mono">{p.symbol}</TableCell>
                  <TableCell>{p.position_side === "long" ? "多头" : "空头"}</TableCell>
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
