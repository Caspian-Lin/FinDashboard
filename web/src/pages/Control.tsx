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
import { useT, type LocalizedText } from "@/i18n";

interface KsLevel {
  value: string;
  label: LocalizedText;
  variant: "success" | "warning" | "destructive";
  impact: LocalizedText;
  notice: LocalizedText;
}

const KS_LEVELS: KsLevel[] = [
  {
    value: "off",
    label: { zh: "恢复正常", en: "Resume Normal" },
    variant: "success",
    impact: { zh: "恢复提交新订单的能力,解除当前中止状态。", en: "Restores the ability to submit new orders and lifts the current halt." },
    notice: { zh: "解除限制后交易行为立即恢复,请确认当前已无风险。", en: "Trading resumes immediately once the restriction is lifted; please confirm there is no remaining risk." },
  },
  {
    value: "no_new_orders",
    label: { zh: "暂停新单", en: "Pause New Orders" },
    variant: "warning",
    impact: { zh: "禁止提交任何新订单;已提交订单不受影响,仍可撤单、查询与减仓。", en: "Blocks submitting any new orders; already-submitted orders are unaffected and can still be cancelled, queried, or reduced." },
    notice: { zh: "可随时切换其他级别解除限制。", en: "You can switch to another level at any time to lift the restriction." },
  },
  {
    value: "reduce_only",
    label: { zh: "仅减仓", en: "Reduce Only" },
    variant: "warning",
    impact: { zh: "禁止开新仓,仅允许减少现有持仓的卖出操作。", en: "Blocks opening new positions; only sell operations that reduce existing positions are allowed." },
    notice: { zh: "可随时切换其他级别解除限制。", en: "You can switch to another level at any time to lift the restriction." },
  },
  {
    value: "cancel_all",
    label: { zh: "撤全部", en: "Cancel All" },
    variant: "destructive",
    impact: { zh: "向券商撤销全部活动订单(不可撤销的订单除外),撤销动作由交易内核执行;随后进入仅减仓约束。", en: "Cancels all active orders at the broker (except non-cancellable ones); the cancellation is executed by the trading kernel, followed by a reduce-only constraint." },
    notice: { zh: "撤单动作不可自动恢复;解除限制需人工确认后逐级切换。", en: "Cancellations cannot be undone automatically; lifting the restriction requires step-by-step manual confirmation." },
  },
  {
    value: "halt",
    label: { zh: "全局停止", en: "Global Halt" },
    variant: "destructive",
    impact: { zh: "立即停止一切交易行为:禁止新单、撤单与减仓,策略暂停,由交易内核强制生效。", en: "Immediately stops all trading: new orders, cancellations and position reduction are blocked and strategies pause, enforced by the trading kernel." },
    notice: { zh: "不可自动恢复;恢复交易需人工确认后逐级解除。", en: "Cannot be resumed automatically; restoring trading requires step-by-step manual confirmation." },
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

function levelValue(level: string | undefined) {
  return KS_LEVELS.find((l) => l.value === level)?.label ?? undefined;
}

/** 审计条目跨天混排,时间列带日期(MM-DD HH:MM:SS),完整时间戳放 title。 */
function formatAuditTime(iso: string) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export default function Control() {
  const { t, tl } = useT();
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
        title={t("control.title")}
        description={t("control.description")}
      />

      {/* Kill Switch */}
      <section className="rounded-lg border border-border bg-card p-5">
        <h2 className="mb-4 font-semibold">Kill Switch</h2>
        <div className="mb-4">
          <span className="text-sm text-muted-foreground">{t("control.currentLevel")}</span>
          <span className={`font-bold ${ks?.level === "off" ? "text-success" : "text-destructive"}`}>
            {ks?.level ?? "—"}
          </span>
        </div>
        <div className="flex flex-wrap gap-2" role="group" aria-label={t("control.levelGroup")}>
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
              {tl(lvl.label)}
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
          {busy && t("control.switching", { level: tl(levelValue(ksMut.variables?.level) ?? { zh: "", en: "" }) })}
          {ksMut.isSuccess && t("control.switched", { level: tl(levelValue(ksMut.variables?.level) ?? { zh: "", en: "" }) })}
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
            <DialogTitle>{t("control.confirmTitle")}</DialogTitle>
            <DialogDescription>
              {t("control.confirmDescription")}
            </DialogDescription>
          </DialogHeader>
          {confirmTarget && (
            <div className="space-y-3 text-sm">
              <div className="space-y-2 rounded-lg bg-muted/50 p-3">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">{t("control.currentLevelLabel")}</span>
                  <span className="font-semibold">{ks?.level ?? "—"}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">{t("control.targetLevel")}</span>
                  <span className={`font-bold ${ksColorClass(confirmTarget.variant)}`}>
                    {tl(confirmTarget.label)}
                  </span>
                </div>
              </div>
              <div className="space-y-1">
                <p className="font-medium">{t("control.impact")}</p>
                <p className="text-muted-foreground">{tl(confirmTarget.impact)}</p>
              </div>
              <p
                className={
                  confirmTarget.variant === "destructive" ? "text-destructive" : "text-warning"
                }
              >
                {tl(confirmTarget.notice)}
              </p>
              <div className="space-y-1.5">
                <Label htmlFor="ks-confirm-reason">{t("control.reason")}</Label>
                <Input
                  id="ks-confirm-reason"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder={t("control.reasonPlaceholder")}
                />
              </div>
              {ksMut.isError && (
                <Alert variant="destructive">
                  <AlertDescription>
                    {ksMut.error?.message ?? t("control.requestFailed")}
                  </AlertDescription>
                </Alert>
              )}
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmLevel(null)} disabled={busy}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="outline"
              onClick={() => confirmTarget && ksMut.mutate({ level: confirmTarget.value, reason })}
              disabled={busy}
              className={ksColorClass(confirmTarget?.variant ?? "warning")}
            >
              {busy ? t("control.switchingShort") : t("control.confirmLevel", { level: confirmTarget ? tl(confirmTarget.label) : "" })}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Reconcile */}
      <section className="rounded-lg border border-border bg-card p-5">
        <h2 className="mb-4 font-semibold">{t("control.reconcile")}</h2>
        <Button onClick={() => reconMut.mutate()} disabled={reconMut.isPending}>
          {reconMut.isPending ? t("control.reconciling") : t("control.triggerReconcile")}
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
        <h2 className="mb-4 font-semibold">{t("control.auditLog")}</h2>
        {(logs?.items ?? []).length === 0 ? (
          <EmptyState title={t("control.noLogs")} />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("common.time")}</TableHead>
                <TableHead>{t("control.actor")}</TableHead>
                <TableHead>{t("control.action")}</TableHead>
                <TableHead>{t("control.target")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(logs?.items ?? []).map((log) => (
                <TableRow key={log.id}>
                  <TableCell
                    className="whitespace-nowrap tabular-nums text-muted-foreground"
                    title={new Date(log.created_at).toLocaleString()}
                  >
                    {formatAuditTime(log.created_at)}
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
