import { useQuery } from "@tanstack/react-query";
import { History } from "lucide-react";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table";
import { aiResearchApi } from "@/lib/ai";
import { formatDateTime } from "@/lib/utils";

export function AuditTab() {
  const { data: events, isLoading } = useQuery({
    queryKey: ["ai-audit"],
    queryFn: () => aiResearchApi.audit(),
  });

  if (isLoading) {
    return (
      <Card>
        <CardContent className="p-4">
          <Skeleton className="h-32 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (!events || events.length === 0) {
    return (
      <Card>
        <CardContent className="py-12">
          <div className="flex flex-col items-center text-center">
            <History className="mb-3 h-10 w-10 text-muted-foreground/30" />
            <p className="text-sm text-muted-foreground">暂无审计事件</p>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">AI 审计日志</CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <ScrollArea className="max-h-[600px]">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="text-xs">事件类型</TableHead>
                <TableHead className="text-xs">操作者</TableHead>
                <TableHead className="text-xs">假设 ID</TableHead>
                <TableHead className="text-xs">详情</TableHead>
                <TableHead className="text-xs">时间</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {events.map((e) => (
                <TableRow key={e.event_id}>
                  <TableCell>
                    <Badge variant="outline" className="text-[10px] font-mono">{e.event_type}</Badge>
                  </TableCell>
                  <TableCell className="text-xs">{e.actor}</TableCell>
                  <TableCell className="font-mono text-xs">{e.hypothesis_id ?? "—"}</TableCell>
                  <TableCell>
                    <code className="text-[10px] text-muted-foreground">
                      {JSON.stringify(e.payload).slice(0, 80)}
                    </code>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{formatDateTime(e.created_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </ScrollArea>
      </CardContent>
    </Card>
  );
}
