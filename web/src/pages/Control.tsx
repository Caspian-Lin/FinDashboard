import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { PageHeader } from "../components/ui/page-header";
import { PageContainer } from "../components/ui/page-container";
import { Label } from "../components/ui/label";
import { Input } from "../components/ui/input";
import { Button } from "../components/ui/button";
import { Alert, AlertDescription } from "../components/ui/alert";
import { EmptyState } from "../components/ui/states";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";

const KS_LEVELS = [
  { value: "off", label: "恢复正常", variant: "success" as const },
  { value: "no_new_orders", label: "暂停新单", variant: "warning" as const },
  { value: "reduce_only", label: "仅减仓", variant: "warning" as const },
  { value: "cancel_all", label: "撤全部", variant: "destructive" as const },
  { value: "halt", label: "全局停止", variant: "destructive" as const },
];

export default function Control() {
  const qc = useQueryClient();
  const [reason, setReason] = useState("manual");
  const { data: ks } = useQuery({
    queryKey: ["kill-switch"],
    queryFn: api.getKillSwitch,
    refetchInterval: 5000,
  });
  const { data: logs } = useQuery({
    queryKey: ["audit-logs"],
    queryFn: () => api.getAuditLogs(50),
    refetchInterval: 10000,
  });

  const ksMut = useMutation({
    mutationFn: ({ level, reason }: { level: string; reason: string }) =>
      api.activateKillSwitch(level, reason),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["kill-switch"] }),
  });

  const reconMut = useMutation({
    mutationFn: () => api.triggerReconcile(),
  });

  return (
    <PageContainer>
      <PageHeader
        title="控制台"
        description="Kill Switch 由交易内核执行;页面只提交指令,不绕过内核。"
      />

      {/* Kill Switch */}
      <section className="rounded-lg border border-border bg-card p-5">
        <h2 className="mb-4 font-semibold">Kill Switch</h2>
        <div className="mb-4">
          <span className="text-sm text-muted-foreground">当前状态: </span>
          <span className={`font-bold ${ks?.level === "off" ? "text-success" : "text-destructive"}`}>
            {ks?.level ?? "—"}
          </span>
        </div>
        <div className="mb-3 w-full space-y-1.5">
          <Label htmlFor="ks-reason">触发原因</Label>
          <Input
            id="ks-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="记录本次操作原因(必填,写入审计)"
          />
        </div>
        <div className="flex flex-wrap gap-2" role="group" aria-label="Kill Switch 级别">
          {KS_LEVELS.map((lvl) => (
            <Button
              key={lvl.value}
              variant="outline"
              size="sm"
              onClick={() => ksMut.mutate({ level: lvl.value, reason })}
              disabled={ksMut.isPending}
              className={
                lvl.variant === "success"
                  ? "border-success text-success hover:bg-success/10"
                  : lvl.variant === "destructive"
                    ? "border-destructive text-destructive hover:bg-destructive/10"
                    : "border-warning text-warning hover:bg-warning/10"
              }
            >
              {lvl.label}
            </Button>
          ))}
        </div>
        {ksMut.isError && (
          <Alert variant="destructive" className="mt-3">
            <AlertDescription>{ksMut.error?.message}</AlertDescription>
          </Alert>
        )}
      </section>

      {/* Reconcile */}
      <section className="rounded-lg border border-border bg-card p-5">
        <h2 className="mb-4 font-semibold">核对</h2>
        <Button onClick={() => reconMut.mutate()} disabled={reconMut.isPending}>
          {reconMut.isPending ? "核对中..." : "触发核对"}
        </Button>
        {reconMut.data && (
          <p
            className={`mt-3 text-sm ${reconMut.data.ok ? "text-success" : "text-destructive"}`}
            role="status"
          >
            {reconMut.data.ok ? "✓ " : "✗ "}
            {reconMut.data.summary}
          </p>
        )}
      </section>

      {/* Audit Logs */}
      <section className="rounded-lg border border-border bg-card p-5">
        <h2 className="mb-4 font-semibold">审计日志</h2>
        {(logs?.items ?? []).length === 0 ? (
          <EmptyState title="无日志" />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>时间</TableHead>
                <TableHead>操作者</TableHead>
                <TableHead>动作</TableHead>
                <TableHead>目标</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(logs?.items ?? []).map((log) => (
                <TableRow key={log.id}>
                  <TableCell className="text-muted-foreground">
                    {new Date(log.created_at).toLocaleTimeString()}
                  </TableCell>
                  <TableCell>{log.actor}</TableCell>
                  <TableCell className="font-mono">{log.action}</TableCell>
                  <TableCell className="font-mono text-muted-foreground">{log.target ?? "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>
    </PageContainer>
  );
}
