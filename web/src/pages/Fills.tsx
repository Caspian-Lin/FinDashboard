import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { PageHeader } from "../components/ui/page-header";
import { PageContainer } from "../components/ui/page-container";
import { EmptyState } from "../components/ui/states";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";

export default function Fills() {
  const { data } = useQuery({
    queryKey: ["fills"],
    queryFn: () => api.getFills(200),
    refetchInterval: 5000,
  });
  const fills = data?.items ?? [];

  return (
    <PageContainer>
      <PageHeader title="成交" description="最近 200 笔成交回报。" />
      {fills.length === 0 ? (
        <EmptyState title="无成交记录" description="有成交回报后此处会展示明细。" />
      ) : (
        <div className="overflow-hidden rounded-lg border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>成交编号</TableHead>
                <TableHead>标的</TableHead>
                <TableHead>方向</TableHead>
                <TableHead className="text-right">数量</TableHead>
                <TableHead className="text-right">价格</TableHead>
                <TableHead className="text-right">金额</TableHead>
                <TableHead className="text-right">手续费</TableHead>
                <TableHead>时间</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {fills.map((f) => (
                <TableRow key={f.fill_id}>
                  <TableCell className="font-mono text-xs text-muted-foreground">{f.fill_id}</TableCell>
                  <TableCell className="font-mono">{f.symbol}</TableCell>
                  <TableCell className={f.side === "buy" ? "text-up" : "text-down"}>
                    {f.side === "buy" ? "买入" : "卖出"}
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
