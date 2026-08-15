import { useRef, useState } from "react";
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
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "../components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";
import { cn } from "../lib/utils";

interface KsLevel {
  value: string;
  label: string;
  variant: "success" | "warning" | "destructive";
  impact: string;
  notice: string;
}

const KS_LEVELS: KsLevel[] = [
  {
    value: "off",
    label: "恢复正常",
    variant: "success",
    impact: "恢复提交新订单的能力,解除当前中止状态。",
    notice: "解除限制后交易行为立即恢复,请确认当前已无风险。",
  },
  {
    value: "no_new_orders",
    label: "暂停新单",
    variant: "warning",
    impact: "禁止提交任何新订单;已提交订单不受影响,仍可撤单、查询与减仓。",
    notice: "可随时切换其他级别解除限制。",
  },
  {
    value: "reduce_only",
    label: "仅减仓",
    variant: "warning",
    impact: "禁止开新仓,仅允许减少现有持仓的卖出操作。",
    notice: "可随时切换其他级别解除限制。",
  },
  {
    value: "cancel_all",
    label: "撤全部",
    variant: "destructive",
    impact: "向券商撤销全部活动订单(不可撤销的订单除外),撤销动作由交易内核执行;随后进入仅减仓约束。",
    notice: "撤单动作不可自动恢复;解除限制需人工确认后逐级切换。",
  },
  {
    value: "halt",
    label: "全局停止",
    variant: "destructive",
    impact: "立即停止一切交易行为:禁止新单、撤单与减仓,策略暂停,由交易内核强制生效。",
    notice: "不可自动恢复;恢复交易需人工确认后逐级解除。",
  },
];

function ksColorClass(variant: KsLevel["variant"]) {
  if (variant === "success") {
    return "border-success text-success hover:bg-success/10";
  }
  if (variant === "destructive") {
    return "border-destructive text-destructive hover:bg-destructive/10";
  }
  return "border-warning text-warning hover:bg-warning/10";
}

function levelLabel(level: string | undefined) {
  return KS_LEVELS.find((l) => l.value === level)?.label ?? level ?? "";
}

export default function Control() {
  const qc = useQueryClient();
  const [reason, setReason] = useState("manual");
  const [confirmLevel, setConfirmLevel] = useState<string | null>(null);
  // Radix modal 弹窗关闭时只把焦点还给 DialogTrigger;本页用普通按钮+状态控制弹窗,
  // 需自己记录触发按钮并在 onCloseAutoFocus 中归还焦点,否则键盘用户会丢焦点。
  const triggerRef = useRef<HTMLButtonElement | null>(null);
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
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["kill-switch"] });
      setConfirmLevel(null);
    },
  });

  const reconMut = useMutation({
    mutationFn: () => api.triggerReconcile(),
  });

  const confirmTarget = KS_LEVELS.find((l) => l.value === confirmLevel) ?? null;
  const busy = ksMut.isPending;

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
        <div className="flex flex-wrap gap-2" role="group" aria-label="Kill Switch 级别">
          {KS_LEVELS.map((lvl) => (
            <Button
              key={lvl.value}
              variant="outline"
              size="sm"
              onClick={(e) => {
                ksMut.reset();
                triggerRef.current = e.currentTarget;
                setConfirmLevel(lvl.value);
              }}
              disabled={busy}
              className={ksColorClass(lvl.variant)}
            >
              {lvl.label}
            </Button>
          ))}
        </div>
        {/* 进行中/成功状态播报(aria-live),失败在弹窗内播报 */}
        <p
          role="status"
          className={cn(
            "mt-3 min-h-5 text-sm",
            ksMut.isSuccess ? "text-success" : "text-muted-foreground",
          )}
        >
          {busy && `正在切换 Kill Switch 至「${levelLabel(ksMut.variables?.level)}」...`}
          {ksMut.isSuccess && `✓ Kill Switch 已切换至「${levelLabel(ksMut.variables?.level)}」`}
        </p>
      </section>

      {/* Kill Switch 确认弹窗 */}
      <Dialog
        open={!!confirmTarget}
        onOpenChange={(v) => {
          if (!v && !busy) setConfirmLevel(null);
        }}
      >
        <DialogContent
          onEscapeKeyDown={(e) => busy && e.preventDefault()}
          onPointerDownOutside={(e) => busy && e.preventDefault()}
          onCloseAutoFocus={(e) => {
            e.preventDefault();
            triggerRef.current?.focus();
          }}
        >
          <DialogHeader>
            <DialogTitle>确认切换 Kill Switch</DialogTitle>
            <DialogDescription>
              此操作由交易内核执行,页面只提交指令,不绕过内核。请核对目标状态与影响范围后确认。
            </DialogDescription>
          </DialogHeader>
          {confirmTarget && (
            <div className="space-y-3 text-sm">
              <div className="space-y-2 rounded-lg bg-muted/50 p-3">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">当前状态</span>
                  <span className="font-semibold">{ks?.level ?? "—"}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">目标状态</span>
                  <span className={`font-bold ${ksColorClass(confirmTarget.variant)}`}>
                    {confirmTarget.label}
                  </span>
                </div>
              </div>
              <div className="space-y-1">
                <p className="font-medium">影响范围</p>
                <p className="text-muted-foreground">{confirmTarget.impact}</p>
              </div>
              <p
                className={
                  confirmTarget.variant === "destructive" ? "text-destructive" : "text-warning"
                }
              >
                {confirmTarget.notice}
              </p>
              <div className="space-y-1.5">
                <Label htmlFor="ks-confirm-reason">操作原因</Label>
                <Input
                  id="ks-confirm-reason"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="记录本次操作原因(写入审计)"
                />
              </div>
              {ksMut.isError && (
                <Alert variant="destructive">
                  <AlertDescription>
                    {ksMut.error?.message ?? "请求失败,请重试或取消。"}
                  </AlertDescription>
                </Alert>
              )}
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmLevel(null)} disabled={busy}>
              取消
            </Button>
            <Button
              variant="outline"
              onClick={() => confirmTarget && ksMut.mutate({ level: confirmTarget.value, reason })}
              disabled={busy}
              className={ksColorClass(confirmTarget?.variant ?? "warning")}
            >
              {busy ? "切换中..." : `确认${confirmTarget?.label ?? ""}`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

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
