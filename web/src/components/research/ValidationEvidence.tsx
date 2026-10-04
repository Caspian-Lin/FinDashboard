import { Link } from "react-router-dom";
import { useT } from "@/i18n";
import type { ValidationTrial } from "@/lib/research";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

export function ValidationEvidence({ trials }: { trials: ValidationTrial[] }) {
  const { tl } = useT();
  return <div className="space-y-2">
    <p className="text-xs text-muted-foreground">{tl({ zh: "压力档配置是计划。只有已执行证据可用于判断；历史记录缺执行状态时保留未知。", en: "Stress configuration is a plan. Evaluate executed evidence; legacy records without execution status remain unverified." })}</p>
    {trials.map(trial => <details key={trial.trial_id} className="rounded-lg border border-border p-3">
      <summary className="cursor-pointer text-sm">{tl({ zh: "执行证据", en: "Execution evidence" })} · {trial.trial_id} ({trial.robustness_probes?.length ?? 0})</summary>
      {!trial.robustness_probes?.length ? <p className="mt-2 text-xs text-muted-foreground">{tl({ zh: "暂无执行证据", en: "No execution evidence" })}</p> :
        <div className="mt-2 overflow-x-auto"><Table>
          <TableHeader><TableRow><TableHead>{tl({ zh: "检查", en: "Probe" })}</TableHead><TableHead>{tl({ zh: "执行与结论", en: "Execution and outcome" })}</TableHead><TableHead>{tl({ zh: "净收益 / 回撤", en: "Net return / drawdown" })}</TableHead><TableHead>{tl({ zh: "运行 / 原因", en: "Run / reason" })}</TableHead></TableRow></TableHeader>
          <TableBody>{trial.robustness_probes.map((probe, index) => {
            const status = probe.detail.status;
            const executed = status === "completed";
            const state = executed ? probe.probe_kind === "evidence" ? { zh: "已归档", en: "Archived" } : probe.passed ? { zh: "已执行 · 门槛通过", en: "Executed · threshold passed" } : { zh: "已执行 · 门槛未通过", en: "Executed · threshold failed" } : status === "unsupported" ? { zh: "能力不支持", en: "Unsupported" } : status === "failed" ? { zh: "执行失败", en: "Execution failed" } : status === "planned" ? { zh: "计划 · 尚未执行", en: "Planned · not executed" } : { zh: "历史记录 · 执行未核验", en: "Legacy · execution unverified" };
            const evidence = probe.detail.evidence as Record<string, unknown> | undefined;
            const runId = probe.detail.run_id ?? evidence?.run_id;
            return <TableRow key={`${probe.label}-${index}`}>
              <TableCell className="text-xs">{probe.label}</TableCell>
              <TableCell className="text-xs">{tl(state)}</TableCell>
              <TableCell className="text-xs tabular-nums">{executed && probe.probe_kind !== "evidence" ? `${(probe.total_return * 100).toFixed(2)}% / ${(Math.abs(probe.max_drawdown) * 100).toFixed(2)}%` : "—"}</TableCell>
              <TableCell className="max-w-xs break-words text-xs">{typeof runId === "string" ? <Link className="text-primary underline" to={`/research/runs?run=${encodeURIComponent(runId)}`}>{runId}</Link> : String(probe.detail.reason ?? probe.detail.error ?? "—")}</TableCell>
            </TableRow>;
          })}</TableBody>
        </Table></div>}
    </details>)}
  </div>;
}
