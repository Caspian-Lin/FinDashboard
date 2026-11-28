import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { PageHeader } from "@/components/ui/page-header";
import { Button } from "@/components/ui/button";
import { Input, Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LoadingState, ErrorState, EmptyState } from "@/components/ui/states";
import { EvidenceFields } from "@/components/research/StrategyExplanation";
import { ResearchMemoryArticle } from "@/components/research/ResearchMemoryArticle";
import FormalExperimentDialog from "@/components/research/FormalExperimentDialog";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import ResearchRunSetup from "@/components/research/ResearchRunSetup";
import { useT, type Language, type LocalizedText } from "@/i18n";
import { researchDocsApi, type ResearchRunQueueIn } from "@/lib/research";
import { ResearchTimestamp } from "@/components/research/ResearchTimestamp";
import { TopicDataCard } from "@/components/data/TopicDataCard";
import {
  workspaceApi,
  topicRunApi,
  topicActionApi,
  evidenceUrl,
  conflictDetail,
  type ActivateInput,
  type EvidenceRef,
  type Topic,
  type TopicActionName,
  type TopicArtifactLink,
  type TopicConflictDetail,
  type TopicDraftInput,
  type TopicDraftUpdate,
  type TopicInput,
  type TopicRunLink,
  type RoundInput,
  type MemoryDetail,
} from "@/lib/research-workspace";
import { fetchJSON } from "@/lib/api";

const EMPTY_DRAFT: TopicDraftInput = { title: "", question: "" };
const EMPTY_GOAL = { version: "", criteria: "", source: { kind: "document", ref_id: "" } };
const STATES: Record<string, LocalizedText> = {
  low: { zh: "低", en: "Low" },
  medium: { zh: "中", en: "Medium" },
  high: { zh: "高", en: "High" },
  research_run: { zh: "研究回测", en: "Research backtest" },
  strategy: { zh: "策略版本", en: "Strategy version" },
  dataset: { zh: "数据发布", en: "Data release" },
  experiment: { zh: "验证实验", en: "Validation experiment" },
  memory: { zh: "研究记忆", en: "Research memory" },
  document: { zh: "正式文档", en: "Canonical document" },
  factor_series: { zh: "因子序列", en: "Factor series" },
  simulation: { zh: "模拟盘", en: "Simulation" },
  backtest: { zh: "探索回测", en: "Exploratory backtest" },
  factor_experiment: { zh: "因子实验", en: "Factor experiment" },
  code_artifact: { zh: "代码产物", en: "Code artifact" },
  code_run: { zh: "沙箱运行", en: "Sandbox run" },
  validation_experiment: { zh: "OOS 验证实验", en: "OOS experiment" },
  factor_snapshot: { zh: "因子快照", en: "Factor snapshot" },
  plan: { zh: "计划（未执行）", en: "Plan (not executed)" },
  round: { zh: "执行记录", en: "Execution record" },
  evidence: { zh: "证据整理", en: "Evidence notes" },
  review: { zh: "复核结论", en: "Reviewed conclusion" },
  draft: { zh: "草稿", en: "Draft" },
  active: { zh: "研究中", en: "Active" },
  paused: { zh: "已暂停", en: "Paused" },
  closed: { zh: "已收束", en: "Closed" },
  unknown: { zh: "结论未定", en: "Conclusion pending" },
  supported: { zh: "工作结论：获支持", en: "Working conclusion: supported" },
  not_supported: { zh: "工作结论：未获支持", en: "Working conclusion: not supported" },
  insufficient_evidence: { zh: "工作结论：证据不足", en: "Working conclusion: insufficient evidence" },
  completed: { zh: "已完成", en: "Completed" },
  failed: { zh: "失败", en: "Failed" },
  interrupted: { zh: "中断", en: "Interrupted" },
  rejected: { zh: "被拒绝", en: "Rejected" },
  forgotten: { zh: "已纠正/移除", en: "Corrected or removed" },
  archived: { zh: "归档", en: "Archived" },
  matched: { zh: "引用存在", en: "Reference verified" },
  missing: { zh: "断链", en: "Broken reference" },
  checksum_mismatch: { zh: "checksum不一致", en: "Checksum mismatch" },
  version_mismatch: { zh: "版本不一致", en: "Version mismatch" },
  version_required: { zh: "需补精确版本", en: "Exact version required" },
  invalid: { zh: "引用非法", en: "Invalid reference" },
};
const ACTION_LABELS: Record<string, LocalizedText> = {
  pause: { zh: "暂停课题", en: "Pause topic" },
  resume: { zh: "继续课题", en: "Resume topic" },
  close: { zh: "收束课题", en: "Close topic" },
  reopen: { zh: "重开课题", en: "Reopen topic" },
  discard: { zh: "放弃草稿", en: "Discard draft" },
};
const ACTION_HINTS: Record<string, LocalizedText> = {
  pause: { zh: "暂停保留目标、轮次与引用；可随时继续，不产生新研究。", en: "Pausing retains the goal, rounds, and references. You can resume at any time." },
  resume: { zh: "继续后课题恢复为研究中；不会自动发起任何运行。", en: "Resuming makes the topic active again. Runs still require a separate action." },
  close: { zh: "收束表示本轮研究告一段落，原因会写入生命周期记录。", en: "Closing ends this round of research and records your reason in the topic history." },
  reopen: { zh: "重开需要已归档目标版本；历史轮次与结论原样保留。", en: "Reopening requires an archived goal version. Previous rounds and conclusions stay in the history." },
  discard: { zh: "放弃草稿会收束课题并保留记录，不会删除任何证据。", en: "Discarding closes the draft and retains its record and evidence." },
};
const REASON_REQUIRED = new Set(["close", "reopen", "discard"]);
const NEXT_ACTION_LABELS: Record<string, LocalizedText> = {
  activate: { zh: "补齐目标并激活", en: "Complete goal and activate" },
  check_refs: { zh: "核验断链引用", en: "Verify broken references" },
  resume: { zh: "恢复课题", en: "Resume topic" },
  reopen: { zh: "重开课题", en: "Reopen topic" },
  write_round: { zh: "记录轮次", en: "Record a round" },
  review_open_questions: { zh: "复核开放问题", en: "Review open questions" },
  review: { zh: "复核证据", en: "Review evidence" },
};

/** #516: every evidence kind the API accepts, in display order. */
const EVIDENCE_KINDS = [
  "research_run",
  "strategy",
  "dataset",
  "experiment",
  "memory",
  "document",
  "factor_series",
  "simulation",
  "backtest",
  "factor_experiment",
  "code_artifact",
  "code_run",
  "validation_experiment",
  "factor_snapshot",
] as const;

const ENTRY_TYPE_LABELS: Record<string, LocalizedText> = {
  plan: { zh: "计划（只保存，不执行）", en: "Plan (save only)" },
  round: { zh: "执行记录", en: "Execution record" },
  evidence: { zh: "证据整理", en: "Evidence notes" },
  review: { zh: "复核结论", en: "Reviewed conclusion" },
  lifecycle: { zh: "生命周期", en: "Lifecycle" },
  goal: { zh: "目标快照", en: "Goal snapshot" },
  draft: { zh: "初始草稿", en: "Initial draft" },
};

/** #516: oos_outcome is the primary conclusion; status is only diagnostic. */
const OOS_OUTCOME_LABELS: Record<string, LocalizedText> = {
  supported: { zh: "样本外获支持", en: "Supported out of sample" },
  not_supported: { zh: "样本外未获支持", en: "Not supported out of sample" },
  inconclusive: { zh: "样本外无法判定", en: "Out-of-sample result inconclusive" },
  unknown: { zh: "样本外未知（证据受限）", en: "Out-of-sample outcome unknown (limited evidence)" },
};

const ARTIFACT_STATES: Record<string, LocalizedText> = {
  queued: { zh: "排队中", en: "Queued" },
  running: { zh: "运行中", en: "Running" },
  succeeded: { zh: "已完成", en: "Completed" },
  failed: { zh: "失败", en: "Failed" },
  cancelled: { zh: "已取消", en: "Cancelled" },
  interrupted: { zh: "已中断", en: "Interrupted" },
};

const EVIDENCE_GAP_LABELS: Record<string, LocalizedText> = {
  no_trials: { zh: "尚无 trial", en: "No trials yet" },
  final_test_not_unsealed: { zh: "尚未揭盲", en: "Final holdout still sealed" },
  oos_inconclusive: { zh: "OOS 无可判定证据", en: "Out-of-sample evidence is inconclusive" },
  trial_summary_oversized: { zh: "trial 摘要超限，不推断 OOS", en: "Trial summary exceeds the size limit. OOS outcome unavailable." },
  status_not_terminal: { zh: "实验未到终态", en: "Experiment is still in progress" },
  run_not_created: { zh: "沙箱运行尚未生成记录", en: "Sandbox run record is pending" },
  timed_out: { zh: "运行超时", en: "Run timed out" },
  oom_killed: { zh: "内存不足被终止", en: "Run terminated after running out of memory" },
  unseal_job_failed: { zh: "揭盲任务失败", en: "Final holdout job failed" },
  unseal_job_interrupted: { zh: "揭盲任务中断", en: "Final holdout job interrupted" },
  unseal_job_cancelled: { zh: "揭盲任务取消", en: "Final holdout job cancelled" },
};

function evidenceGapLabels(gaps: string[], lang: Language): string {
  return gaps.map((gap) => EVIDENCE_GAP_LABELS[gap]?.[lang] ?? gap).join(lang === "zh" ? "、" : ", ");
}

// These are the server's current continuation hints. Keep unknown or older
// diagnostics intact so clients can still read the exact reason from the API.
const NEXT_ACTION_REASONS_EN = new Map(Object.entries({
  "草稿缺少目标版本与来源,补齐后才能发起研究": "Add the goal version and source to start research.",
  "课题已暂停;续接前先确认是否恢复": "Confirm whether to resume this paused topic.",
  "课题已收束;需要新证据时先重开": "Reopen the topic to gather new evidence.",
  "尚无轮次记录,下一步动作未留痕": "Record the next action in the first research round.",
  "最近一轮失败或中断,需记录新尝试或原因": "Record a new attempt or explain why the last round stopped.",
  "仍有未解决的开放问题,先核对证据": "Check the evidence for the open questions.",
  "暂无自动发现的缺口;读轮次与正式文档后决定下一步": "Review the rounds and canonical documents before deciding what to do next.",
}));

function nextActionReason(why: string, lang: Language): string {
  if (lang === "zh") return why;
  const broken = /^(\d+)个引用断链、版本或checksum不符,需重新核验$/.exec(why);
  if (broken) return `Verify references with broken links, version mismatches, or checksum mismatches (${broken[1]}).`;
  return NEXT_ACTION_REASONS_EN.get(why) ?? why;
}

function lifecycleActions(status: string): string[] {
  if (status === "draft") return ["discard"];
  if (status === "active") return ["pause", "close"];
  if (status === "paused") return ["resume", "close"];
  if (status === "closed") return ["reopen"];
  return [];
}

function activationFrom(topic: Topic): ActivateInput {
  return {
    goal: topic.goal ?? { ...EMPTY_GOAL, source: { ...EMPTY_GOAL.source } },
    conclusion: topic.conclusion,
    summary: topic.summary,
    open_questions: topic.open_questions,
    next_step: topic.next_step,
  };
}

export function EvidenceLinks({ refs }: { refs: EvidenceRef[] }) {
  const { tl, lang } = useT();
  return (
    <ul className="space-y-2">
      {refs.map((r, i) => {
        const url = evidenceUrl(r);
        return (
          <li key={i} className="break-all text-sm">
            {url ? (
              <Link className="text-primary underline" to={url}>
                {STATES[r.kind]?.[lang] ?? r.kind}{tl({ zh: "：", en: ": " })}{r.ref_id}
                {r.version ? ` @${r.version}` : ""}
              </Link>
            ) : (
              <span className="text-warning">
                {tl({ zh: "无法验证的来源：", en: "Source could not be verified: " })}{r.kind || "unknown"} / {r.ref_id || tl({ zh: "空引用", en: "Empty reference" })}
              </span>
            )}
            {r.checksum && <p className="font-mono text-xs text-muted-foreground">{r.checksum}</p>}
            <SourceFact reference={r} />
          </li>
        );
      })}
    </ul>
  );
}
function MemoryEvidence({ refs }: { refs: EvidenceRef[] }) {
  const { tl } = useT();
  const [open, setOpen] = useState(false);
  return (
    <details className="min-w-0 text-sm" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="cursor-pointer text-muted-foreground">{tl({ zh: "关联证据（" + refs.length + "）", en: "Linked evidence (" + refs.length + ")" })}</summary>
      {open && (
        <div className="mt-3">
          <EvidenceLinks refs={refs} />
        </div>
      )}
    </details>
  );
}
function SourceFact({ reference }: { reference: EvidenceRef }) {
  const { tl } = useT();
  const q = useQuery({
    queryKey: ["source-fact", reference],
    queryFn: () =>
      fetchJSON<{ status: string; facts?: Record<string, unknown> }>(
        `/research/topics/source?${new URLSearchParams({ kind: reference.kind, ref_id: reference.ref_id, ...(reference.version ? { version: reference.version } : {}), ...(reference.checksum ? { checksum: reference.checksum } : {}) })}`,
      ),
    enabled: !!reference.ref_id && reference.kind !== "unknown",
  });
  if (q.isError) return <p className="text-xs text-warning">{tl({ zh: "来源核验暂不可用", en: "Source verification is temporarily unavailable" })}</p>;
  if (!q.data) return null;
  return (
    <div className="mt-1 text-xs text-muted-foreground">
      <p>
        {tl({ zh: "自动来源核验：", en: "Automatic source check: " })}
        {q.data.status === "matched"
          ? tl({ zh: "引用存在", en: "Reference verified" })
          : q.data.status === "missing"
            ? tl({ zh: "断链，产物不存在", en: "Broken reference: artifact missing" })
            : q.data.status === "checksum_mismatch"
              ? tl({ zh: "checksum不一致", en: "Checksum mismatch" })
              : q.data.status === "version_required"
                ? tl({ zh: "需补精确版本", en: "Exact version required" })
                : q.data.status}
      </p>
      {q.data.facts && (
        <details>
          <summary className="cursor-pointer">{tl({ zh: "查看产物事实（独立于轮次解释）", en: "View artifact facts (separate from the round's interpretation)" })}</summary>
          <EvidenceFields value={q.data.facts} />
        </details>
      )}
    </div>
  );
}
function TopicGoalSource({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const { tl } = useT();
  const [manual, setManual] = useState(false);
  const docs = useQuery({ queryKey: ["research-docs"], queryFn: researchDocsApi.list });
  const options = docs.data?.docs ?? [];
  const hasCatalog = options.length > 0;
  return (
    <div className="min-w-0 space-y-1">
      {hasCatalog && !manual ? (
        <label className="grid gap-1 text-sm">
          <span className="font-medium">{tl({ zh: "目标来源（正式文档）", en: "Goal source (canonical document)" })}</span>
          <select className="h-11 min-w-0 rounded-md border border-input bg-background px-3" required
            value={value} onChange={(event) => onChange(event.target.value)}>
            <option value="">{tl({ zh: "选择仓库文档", en: "Choose a repository document" })}</option>
            {options.map((doc) => <option value={doc.path} key={doc.path}>{doc.title}</option>)}
            {value && !options.some((doc) => doc.path === value) && <option value={value}>{value}</option>}
          </select>
        </label>
      ) : (
        <Field label={tl({ zh: "目标来源（研究文档相对路径）", en: "Goal source (repository document path)" })}
          required value={value} onChange={onChange} />
      )}
      {hasCatalog && (
        <Button type="button" variant="link" className="min-h-8 p-0" onClick={() => setManual(!manual)}>
          {manual ? tl({ zh: "从文档列表选择", en: "Choose from documents" })
            : tl({ zh: "使用其他仓库文档路径", en: "Use another repository document path" })}
        </Button>
      )}
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  multiline = false,
  required = false,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  multiline?: boolean;
  required?: boolean;
}) {
  const id = `topic-${label}`;
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      {multiline ? (
        <Textarea
          id={id}
          value={value}
          required={required}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <Input
          id={id}
          value={value}
          required={required}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </div>
  );
}
function Choice({
  label,
  value,
  values,
  onChange,
}: {
  label: string;
  value: string;
  values: string[];
  onChange: (v: string) => void;
}) {
  const { lang } = useT();
  return (
    <div className="space-y-1">
      <Label htmlFor={label}>{label}</Label>
      <select
        id={label}
        className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {values.map((v) => (
          <option key={v} value={v}>
            {STATES[v]?.[lang] ?? v}
          </option>
        ))}
      </select>
    </div>
  );
}

function ContextCard({ topicId }: { topicId: string }) {
  const { tl, lang } = useT();
  const q = useQuery({
    queryKey: ["topic-context", topicId],
    queryFn: () => workspaceApi.context(topicId),
  });
  if (q.isLoading) return <LoadingState />;
  if (q.isError) return <ErrorState message={String(q.error)} onRetry={() => q.refetch()} />;
  const context = q.data;
  if (!context) return null;
  return (
    <section
      aria-label={tl({ zh: "继续研究上下文", en: "Research continuation context" })}
      className="space-y-3 rounded-lg border border-border p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">{tl({ zh: "继续研究（跨客户端续接）", en: "Resume research across clients" })}</h3>
        <span className="text-xs text-muted-foreground">
          {tl({ zh: "生成于 ", en: "Generated " })}<ResearchTimestamp value={context.generated_at} /> · revision {context.revision}
          {context.excerpted ? tl({ zh: " · 已按边界截取", en: " · Excerpted to fit limits" }) : ""}
        </span>
      </div>
      {context.excerpted && (
        <p className="text-xs text-muted-foreground">
          {tl({
            zh: `上下文按服务端边界截取（最近轮次 ≤${context.bounds.max_rounds}、引用核验 ≤${context.bounds.max_refs}、摘录 ≤${context.bounds.excerpt_chars} 字、载荷 ≤64 KiB）；精确全文请用轮次 ID 或课题详情查看。`,
            en: `Context limits: recent rounds ≤${context.bounds.max_rounds}, reference checks ≤${context.bounds.max_refs}, excerpts ≤${context.bounds.excerpt_chars} characters, payload ≤64 KiB. Open the round by ID or the topic details for the full text.`,
          })}
          {context.truncated_sections?.length
            ? tl({
                zh: ` 已压缩段落：${context.truncated_sections.join("、")}`,
                en: ` Shortened sections: ${context.truncated_sections.join(", ")}`,
              })
            : ""}
        </p>
      )}
      {context.lifecycle.last && (
        <p className="text-sm text-muted-foreground">
          {tl({ zh: "最近生命周期：", en: "Latest lifecycle change: " })}
          {STATES[context.lifecycle.last.from ?? ""]?.[lang] ?? context.lifecycle.last.from} →{" "}
          {STATES[context.lifecycle.last.to ?? ""]?.[lang] ?? context.lifecycle.last.to}
          {context.lifecycle.last.reason ? tl({ zh: `（${context.lifecycle.last.reason}）`, en: ` (${context.lifecycle.last.reason})` }) : ""}
          {context.lifecycle.last.actor ? ` · ${context.lifecycle.last.actor}` : ""}
        </p>
      )}
      {context.goal ? (
        <p className="text-sm">
          {tl({
            zh: `当前目标 ${context.goal.version}：${context.goal.criteria}`,
            en: `Current goal ${context.goal.version}: ${context.goal.criteria}`,
          })}
        </p>
      ) : (
        <p className="text-sm text-warning">{tl({ zh: "尚未补齐目标版本与来源，激活后才能发起研究。", en: "Complete the goal version and source, then activate the topic to start research." })}</p>
      )}
      <div>
        <h4 className="text-sm font-medium">{tl({ zh: "建议下一步（服务端派生，不是执行）", en: "Suggested next steps" })}</h4>
        <ul className="list-disc space-y-1 pl-5 text-sm">
          {context.next_actions.map((action) => (
            <li key={action.action}>
              {NEXT_ACTION_LABELS[action.action]?.[lang] ?? action.action}{tl({ zh: "：", en: ": " })}{nextActionReason(action.why, lang)}
            </li>
          ))}
        </ul>
      </div>
      {context.dangling_refs.length > 0 && (
        <div role="alert" className="rounded-md border border-destructive/40 p-3 text-sm">
          <p className="font-medium text-destructive">
            {tl({
              zh: `断链引用 ${context.dangling_refs.length} 条，需重新核验（不是自动接受或自动修复）：`,
              en: `Broken references requiring manual verification (${context.dangling_refs.length})`,
            })}
          </p>
          <ul className="mt-1 space-y-1">
            {context.dangling_refs.map((item, index) => (
              <li key={`${item.ref.kind}-${item.ref.ref_id}-${index}`} className="break-all">
                {STATES[item.ref.kind]?.[lang] ?? item.ref.kind}{tl({ zh: "：", en: ": " })}{item.ref.ref_id} · {STATES[item.status]?.[lang] ?? item.status}
              </li>
            ))}
          </ul>
        </div>
      )}
      {context.active_links.length > 0 && (
        <div>
          <h4 className="text-sm font-medium">{tl({ zh: "进行中的关联", en: "In progress" })}</h4>
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {context.active_links.map((link) => (
              <li key={`${link.entity_type}-${link.entity_id}`} className="break-all">
                {STATES[link.entity_type]?.[lang] ?? link.entity_type}{tl({ zh: "：", en: ": " })}{link.entity_id}
                {link.job_id ? ` · ${link.job_id}` : ""}
                {link.goal_version ? `${tl({ zh: " · 目标 ", en: " · Goal " })}${link.goal_version}` : ""} · {ARTIFACT_STATES[link.state]?.[lang] ?? link.state}
              </li>
            ))}
          </ul>
        </div>
      )}
      {context.experiment_links.length > 0 && (
        <div>
          <h4 className="text-sm font-medium">{tl({ zh: "因子与OOS（以 oos_outcome 优先）", en: "Factors and out-of-sample evidence" })}</h4>
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {context.experiment_links.map((link) => (
              <li key={`${link.entity_type}-${link.entity_id}`} className="break-all">
                {STATES[link.entity_type]?.[lang] ?? link.entity_type}{tl({ zh: "：", en: ": " })}{link.entity_id} ·{" "}
                {ARTIFACT_STATES[link.state]?.[lang] ?? link.state}
                {link.entity_type === "validation_experiment"
                  ? ` · ${OOS_OUTCOME_LABELS[link.oos_outcome ?? "unknown"]?.[lang] ?? tl({ zh: "样本外未知", en: "Out-of-sample outcome unknown" })} · ${
                      link.final_test_unsealed ? tl({ zh: "已揭盲，不可重做", en: "Holdout used" }) : tl({ zh: "未揭盲", en: "Holdout sealed" })
                    }${tl({ zh: " · 预算 ", en: " · Budget " })}${link.trial_budget?.used ?? 0}/${link.trial_budget?.limit ?? "?"}`
                  : link.status
                    ? ` · ${link.status}`
                    : ""}
                {link.evidence_gaps.length > 0
                  ? `${tl({ zh: " · 缺口：", en: " · Gaps: " })}${evidenceGapLabels(link.evidence_gaps, lang)}`
                  : ""}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div>
        <h4 className="text-sm font-medium">{tl({ zh: `最近轮次（${context.recent_rounds.length}）`, en: `Recent rounds (${context.recent_rounds.length})` })}</h4>
        {Object.keys(context.entry_types ?? {}).length > 0 && (
          <p className="text-xs text-muted-foreground">
            {tl({ zh: "条目结构：", en: "Entry types: " })}
            {Object.entries(context.entry_types)
              .map(([type, count]) => `${ENTRY_TYPE_LABELS[type]?.[lang] ?? type} ${count}`)
              .join(" · ")}
          </p>
        )}
        {context.recent_rounds.length === 0 ? (
          <p className="text-sm text-muted-foreground">{tl({ zh: "尚无轮次记录；下一步动作还没有留痕。", en: "Record the next action in the first research round." })}</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {context.recent_rounds.map((round) => (
              <li key={round.entry_id} className="border-t border-border pt-2">
                <p className="text-xs text-muted-foreground">
                  <ResearchTimestamp value={round.created_at} /> · {round.branch} ·{" "}
                  {round.entry_type ? `${ENTRY_TYPE_LABELS[round.entry_type]?.[lang] ?? round.entry_type} · ` : ""}
                  {STATES[round.outcome ?? ""]?.[lang] ?? round.outcome ?? tl({ zh: "未记录结果", en: "No result recorded" })} ·{" "}
                  {round.evidence_level === "agent_interpretation"
                    ? tl({ zh: "agent 解释", en: "Agent interpretation" })
                    : round.evidence_level === "reviewed_conclusion"
                      ? tl({ zh: "经复核结论", en: "Reviewed conclusion" })
                      : round.evidence_level === "research_plan"
                        ? tl({ zh: "计划（未执行）", en: "Plan (not executed)" })
                        : tl({ zh: "人工记录", en: "Human record" })}{" "}
                  · {round.entry_id}
                </p>
                <p className="whitespace-pre-wrap">
                  {round.conclusion_excerpt || tl({ zh: "本轮尚无工作结论", en: "No working conclusion for this round" })}
                </p>
                {round.next_step && <p className="text-muted-foreground">{tl({ zh: "下一步：", en: "Next step: " })}{round.next_step}</p>}
              </li>
            ))}
          </ul>
        )}
      </div>
      {context.open_questions.length > 0 && (
        <div>
          <h4 className="text-sm font-medium">{tl({ zh: "开放问题", en: "Open questions" })}</h4>
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {context.open_questions.map((question) => (
              <li key={question}>{question}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function TopicArtifactsCard({ topic }: { topic: Topic }) {
  const { tl, lang } = useT();
  const qc = useQueryClient();
  const [notice, setNotice] = useState<LocalizedText | null>(null);
  const [unsealJobId, setUnsealJobId] = useState<string | null>(null);
  const [error, setError] = useState<string | LocalizedText | null>(null);
  const [formalOpen, setFormalOpen] = useState(false);
  const [confirmUnseal, setConfirmUnseal] = useState<{ link: TopicArtifactLink; operationId: string } | null>(null);
  const goalVersion = topic.goal?.version ?? "";
  const artifacts = useQuery({
    queryKey: ["topic-artifacts", topic.topic_id],
    queryFn: () => topicActionApi.artifacts(topic.topic_id),
  });
  const refresh = () => {
    void artifacts.refetch();
    void qc.invalidateQueries({ queryKey: ["topic-context", topic.topic_id] });
  };
  const unseal = useMutation({
    mutationFn: ({ link, operationId }: { link: TopicArtifactLink; operationId: string }) =>
      topicActionApi.act(topic.topic_id, {
        action: "validation_experiment.unseal" as TopicActionName,
        goal_version: goalVersion,
        payload: { experiment_id: link.entity_id },
        operation_id: operationId,
      }),
    onSuccess: (receipt) => {
      setConfirmUnseal(null);
      setUnsealJobId(typeof receipt.job_id === "string" ? receipt.job_id : null);
      setError(null);
      const id = String(receipt.job_id ?? receipt.entity_id);
      setNotice({
        zh: `已提交一次性揭盲执行（${id}）；揭盲后不可重做。`,
        en: `Final holdout job queued (${id}). The holdout can be run only once.`,
      });
      refresh();
    },
    onError: (err) => {
      const detail = conflictDetail(err);
      setNotice(null);
      setError(
        detail?.code === "already_unsealed"
          ? {
              zh: "该实验已揭盲（final_test_unsealed=true），不可重做。",
              en: "This experiment has already used its final holdout (final_test_unsealed=true).",
            }
          : String(err),
      );
    },
  });
  const items = artifacts.data?.items ?? [];
  const ordered = [...items].sort((a, b) =>
    a.entity_type === "validation_experiment" ? -1 : b.entity_type === "validation_experiment" ? 1 : 0,
  );
  return (
    <section
      aria-label={tl({ zh: "课题因子与OOS产物", en: "Topic factor and OOS artifacts" })}
      className="space-y-3 rounded-lg border border-border p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">{tl({ zh: "因子实验 / 沙箱运行 / OOS 实验", en: "Factor experiments, sandbox runs, and OOS experiments" })}</h3>
        <Button type="button" variant="outline" size="sm" disabled={topic.status !== "active" || !goalVersion} onClick={() => setFormalOpen(true)}>
          {tl({ zh: "新建正式规格验证", en: "New formal specification validation" })}
        </Button>
      </div>
      {artifacts.isLoading && <LoadingState />}
      {artifacts.isError && (
        <ErrorState message={String(artifacts.error)} onRetry={() => artifacts.refetch()} />
      )}
      {!artifacts.isLoading && !artifacts.isError && ordered.length === 0 && (
        <p className="text-sm text-muted-foreground">
          {tl({
            zh: "尚无因子实验 / 沙箱运行 / OOS 实验链接。先在课题内登记实验或执行已登记 artifact，服务端会在同一事务写入链接与研究审计。",
            en: "No factor experiments, sandbox runs, or OOS experiments are linked yet. Register an experiment here or run a registered artifact to link it and record the action in the research audit.",
          })}
        </p>
      )}
      {notice && <p className="text-sm text-primary" role="status">
        {tl(notice)}
        {unsealJobId && <Link className="ml-2 underline" to={`/jobs?job=${encodeURIComponent(unsealJobId)}`}>{tl({ zh: "查看揭盲任务", en: "View final holdout job" })}</Link>}
      </p>}
      {error && (
        <p role="alert" className="text-sm text-destructive">{typeof error === "string" ? error : tl(error)}</p>
      )}
      <ul className="space-y-3">
        {ordered.map((link) => (
          <li key={`${link.entity_type}-${link.entity_id}`} className="rounded-md border border-border p-3">
            <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span className="rounded bg-muted px-1.5 py-0.5 font-medium text-foreground">
                {STATES[link.entity_type]?.[lang] ?? link.entity_type}
              </span>
              <span>{link.entity_id}</span>
              <span>{ARTIFACT_STATES[link.state]?.[lang] ?? link.state}</span>
              {link.job_id ? <span>{tl({ zh: "任务 ", en: "Job " })}{link.job_id}</span> : null}
              <span>{tl({ zh: "目标 ", en: "Goal " })}{link.goal_version}</span>
            </p>
            {link.entity_type === "validation_experiment" ? (
              <div className="mt-2 space-y-1 text-sm">
                <p className="font-medium">
                  {tl({ zh: "OOS 结论：", en: "OOS outcome: " })}
                  <span
                    className={
                      link.oos_outcome === "supported"
                        ? "text-success"
                        : link.oos_outcome === "not_supported"
                          ? "text-destructive"
                          : ""
                    }
                  >
                    {OOS_OUTCOME_LABELS[link.oos_outcome ?? "unknown"]?.[lang] ?? tl({ zh: "样本外未知", en: "Out-of-sample outcome unknown" })}
                  </span>
                </p>
                <p className="text-muted-foreground">
                  {tl({ zh: "流程状态：", en: "Workflow status: " })}{link.diagnostic_status ?? link.status ?? tl({ zh: "未知", en: "Unknown" })}{tl({ zh: " · 试验预算", en: " · Trial budget" })}{" "}
                  {link.trial_budget?.used ?? 0}/{link.trial_budget?.limit ?? "?"} ·{" "}
                  {link.final_test_unsealed ? tl({ zh: "已揭盲，不可重做", en: "Holdout used" }) : tl({ zh: "尚未揭盲（一次性门）", en: "Final holdout sealed (single use)" })}
                </p>
                {link.checksum && (
                  <p className="break-all text-xs text-muted-foreground">
                    {tl({ zh: "version checksum：", en: "Version checksum: " })}{link.checksum}
                  </p>
                )}
                {link.evidence_gaps.length > 0 && (
                  <p className="text-xs text-warning">
                    {tl({ zh: "证据缺口：", en: "Evidence gaps: " })}{evidenceGapLabels(link.evidence_gaps, lang)}
                  </p>
                )}
                {link.unseal_job_id && (
                  <p className="text-xs text-muted-foreground">
                    {tl({ zh: "揭盲任务：", en: "Final holdout job: " })}{link.unseal_job_id} ·{" "}
                    {ARTIFACT_STATES[link.unseal_job_status ?? ""]?.[lang] ??
                      link.unseal_job_status ??
                      tl({ zh: "未知", en: "Unknown" })}
                    {link.unseal_job_error ? ` · ${link.unseal_job_error}` : ""}
                  </p>
                )}
                {link.final_test_unsealed ? (
                  <p className="text-xs text-muted-foreground">
                    {tl({
                      zh: "已揭盲的实验不能再次执行；如需新证据请新建实验并用 supersedes_id 关联。",
                      en: "Each final holdout can run once. To gather new evidence, create another experiment and link it using supersedes_id.",
                    })}
                  </p>
                ) : link.unseal_job_pending ? (
                  <p className="text-xs text-warning">
                    {tl({ zh: "排队的揭盲任务尚未完成（", en: "The final holdout job is already queued (" })}{link.unseal_job_status}{tl({ zh: "）；不要重复入队，等待终态后再读取。", en: "). Wait for it to finish before reading the results." })}
                  </p>
                ) : (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={unseal.isPending || !goalVersion || topic.status !== "active"}
                    onClick={() => { unseal.reset(); setConfirmUnseal({ link, operationId: newOperationId("topic-unseal") }); }}
                  >
                    {tl({ zh: "显式执行一次性揭盲", en: "Run one-time holdout" })}
                  </Button>
                )}
                <Link className="ml-2 text-sm text-primary underline" to={`/research/experiments?experiment=${encodeURIComponent(link.entity_id)}&topic=${encodeURIComponent(topic.topic_id)}`}>
                  {tl({ zh: "复核正式实验", en: "Review formal experiment" })}
                </Link>
              </div>
            ) : link.entity_type === "factor_experiment" ? (
              <div className="mt-2 space-y-1 text-sm">
                <p>
                  {tl({ zh: "因子实验状态：", en: "Factor experiment status: " })}{link.status ?? tl({ zh: "未知", en: "Unknown" })}{tl({ zh: " · 数据发布 checksum：", en: " · Release checksum: " })}{" "}
                  <span className="break-all">{link.checksum ?? tl({ zh: "未记录", en: "Not recorded" })}</span>
                </p>
                <p className="text-muted-foreground">
                  {tl({ zh: "特征快照：", en: "Feature snapshot: " })}{link.feature_snapshot_id ?? "—"}
                  {link.validation_experiment_id ? `${tl({ zh: " · 关联 OOS：", en: " · Linked OOS: " })}${link.validation_experiment_id}` : ""}
                </p>
                <p className="break-all text-xs text-muted-foreground">
                  {tl({ zh: "结果 checksum：", en: "Result checksum: " })}{link.result_checksum ?? tl({ zh: "未产出（或结果超 64 KiB 不取摘要）", en: "Unavailable (summary limit: 64 KiB)" })} ·{" "}
                  {link.result_bytes ?? 0}{tl({ zh: " 字节", en: " bytes" })}
                  {link.metrics
                    ? `${tl({ zh: " · 指标：", en: " · Metrics: " })}${JSON.stringify(link.metrics).slice(0, 240)}`
                    : ""}
                </p>
                {link.failure_reason && (
                  <p className="text-xs text-destructive">{tl({ zh: "失败原因：", en: "Failure reason: " })}{link.failure_reason}</p>
                )}
                {link.evidence_gaps.length > 0 && (
                  <p className="text-xs text-warning">
                    {tl({ zh: "证据缺口：", en: "Evidence gaps: " })}{evidenceGapLabels(link.evidence_gaps, lang)}
                  </p>
                )}
              </div>
            ) : (
              <div className="mt-2 space-y-1 text-sm">
                <p>
                  {tl({ zh: "沙箱运行：", en: "Sandbox run: " })}{link.kind ?? "factor"}
                  {link.mode ? ` / ${link.mode}` : ""} · commit{" "}
                  <span className="break-all">{link.commit ?? "—"}</span>
                </p>
                <p className="text-muted-foreground">
                  {tl({ zh: "输出 checksum：", en: "Output checksum: " })}<span className="break-all">{link.checksum ?? tl({ zh: "尚未产出", en: "Pending" })}</span>
                  {link.output_snapshot_id ? `${tl({ zh: " · 快照 ", en: " · Snapshot " })}${link.output_snapshot_id}` : ""}
                </p>
                {link.error_summary && (
                  <p className="text-xs text-destructive">{link.error_summary}</p>
                )}
                {link.evidence_gaps.length > 0 && (
                  <p className="text-xs text-warning">
                    {tl({ zh: "证据缺口：", en: "Evidence gaps: " })}{evidenceGapLabels(link.evidence_gaps, lang)}
                  </p>
                )}
              </div>
            )}
            {link.deep_link && (
              <Link className="mt-1 inline-block text-xs text-primary underline" to={link.deep_link}>
                {tl({ zh: "打开详情", en: "Open details" })}
              </Link>
            )}
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted-foreground">
        {tl({
          zh: "本卡片只读展示（不提供因子实验/沙箱创建表单；创建走 FactorLab 或 MCP），读取与执行严格分开：不会自动发起任何运行；只有显式提交动作（如一次性揭盲）才入队，服务端会要求课题 active 且目标版本匹配，并按 operation_id 返回原回执而不是重复执行。",
          en: "Create factor experiments and sandbox runs in Factor Lab or through MCP. Use the explicit run action to queue work. The topic must be active and its goal version must match. Resubmitting the same operation ID returns the original receipt.",
        })}
      </p>
      {formalOpen && (
        <FormalExperimentDialog topic={topic} onClose={() => setFormalOpen(false)} onCreated={(id) => {
          setFormalOpen(false);
          setNotice({
            zh: `已登记实验 ${id}。在原课题中核对后，再单独确认揭盲。`,
            en: `Experiment ${id} registered. Review it in the original topic, then confirm the final holdout.`,
          });
          refresh();
        }} />
      )}
      <Dialog open={Boolean(confirmUnseal)} onOpenChange={(open) => {
        if (!open && !unseal.isPending) setConfirmUnseal(null);
      }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{tl({ zh: "确认一次性揭盲", en: "Confirm one-time holdout" })}</DialogTitle>
            <DialogDescription>
              {tl({
                zh: "服务端会为这个冻结实验提交执行任务。开始最终测试后，该窗口将被标记为已使用。",
                en: "This queues the frozen experiment. Once the final test starts, its window is marked as used.",
              })}
            </DialogDescription>
          </DialogHeader>
          <p className="break-all text-sm">{confirmUnseal?.link.entity_id}</p>
          {unseal.error && <p role="alert" className="text-sm text-destructive">{unseal.error.message}</p>}
          <DialogFooter>
            <Button variant="outline" disabled={unseal.isPending} onClick={() => setConfirmUnseal(null)}>{tl({ zh: "返回", en: "Back" })}</Button>
            <Button disabled={unseal.isPending || !confirmUnseal} onClick={() => confirmUnseal && unseal.mutate(confirmUnseal)}>
              {unseal.isPending ? tl({ zh: "提交中", en: "Submitting" }) : tl({ zh: "确认执行并消耗最终窗口", en: "Confirm and use final window" })}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

const TERMINAL_TOPIC_RUN_STATES = new Set(["succeeded", "failed", "cancelled"]);

function newOperationId(prefix: string): string {
  const token =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${token}`.slice(0, 64);
}

function TopicRunsCard({ topic }: { topic: Topic }) {
  const { tl, lang } = useT();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [operationId, setOperationId] = useState("");
  const [notice, setNotice] = useState<LocalizedText | null>(null);
  const [receipt, setReceipt] = useState<{ run_id: string; job_id?: string | null } | null>(null);
  const runsQuery = useQuery({
    queryKey: ["topic-runs", topic.topic_id],
    queryFn: () => topicRunApi.list(topic.topic_id),
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? [];
      return items.some((item) => !TERMINAL_TOPIC_RUN_STATES.has(item.state)) ? 5_000 : false;
    },
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["topic-runs", topic.topic_id] });
    void qc.invalidateQueries({ queryKey: ["topic-context", topic.topic_id] });
  };
  const start = useMutation({
    mutationFn: (body: ResearchRunQueueIn) => {
      if (!topic.goal) throw new Error(tl({ zh: "课题缺少有效的目标版本", en: "The topic needs a valid goal version." }));
      return topicRunApi.start(topic.topic_id, {
        ...body,
        goal_version: topic.goal.version,
        operation_id: operationId,
      });
    },
    onSuccess: (receipt) => {
      setReceipt(receipt);
      setOpen(false);
      const { run_id, job_id } = receipt;
      setNotice(receipt.created
        ? {
            zh: `已登记 ${run_id}（${job_id ?? "无任务"}），等待 worker 执行。`,
            en: `Registered ${run_id} (${job_id ?? "no job"}). Waiting for a worker.`,
          }
        : {
            zh: `幂等命中：返回原运行 ${run_id}，未重复登记。`,
            en: `Reused the existing run ${run_id}.`,
          });
      refresh();
    },
  });
  const retry = useMutation({
    mutationFn: (run: TopicRunLink) =>
      topicRunApi.retry(topic.topic_id, run.run_id, {
        idempotency_key: newOperationId("topic-retry"),
        requested_by: "user:web",
        operation_id: newOperationId("OP-topic-retry"),
      }),
    onSuccess: (receipt) => {
      setNotice({
        zh: `已登记重放运行 ${receipt.run_id}，原运行链接保留。`,
        en: `Replay ${receipt.run_id} registered. The original run remains linked.`,
      });
      refresh();
    },
  });
  const cancel = useMutation({
    mutationFn: (run: TopicRunLink) => topicRunApi.cancel(topic.topic_id, run.run_id),
    onSuccess: () => {
      setNotice({
        zh: "已请求取消；worker 在下一个 checkpoint 收口。",
        en: "Cancellation requested. The worker will stop at the next checkpoint.",
      });
      refresh();
    },
  });

  const openForm = () => {
    start.reset();
    setOperationId(newOperationId("OP-topic-run"));
    setNotice(null);
    setReceipt(null);
    setOpen(true);
  };

  const mutationError = retry.error ?? cancel.error;
  const runItems = runsQuery.data?.items ?? [];
  return (
    <section aria-label={tl({ zh: "课题研究运行", en: "Topic research runs" })} className="space-y-3 rounded-lg border border-border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">{tl({ zh: "研究运行（运行事实，独立于轮次解释）", en: "Research runs" })}</h3>
        <Button onClick={openForm} disabled={topic.status !== "active" || !topic.goal}>
          {tl({ zh: "发起研究运行", en: "Start research run" })}
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">
        {tl({
          zh: "发起是独立于保存的显式动作：只冻结已发布策略、数据发布与快照并登记 queued 任务，页面不会在请求内执行回测。运行状态、进度与错误由服务端 join 派生，不会回写课题轮次。",
          en: "Starting a run freezes the published strategy, data release, and snapshot, then queues the job. The run list shows server-reported status, progress, and errors. Topic rounds are recorded separately.",
        })}
      </p>
      {topic.status !== "active" && (
        <p className="text-xs text-warning">{tl({ zh: "只有 active 课题可以发起或重试运行。", en: "Activate the topic to start or retry runs." })}</p>
      )}
      {notice && <p className="text-sm text-primary">{tl(notice)}</p>}
      {receipt && (
        <div className="flex flex-wrap gap-3 text-sm">
          <Link className="text-primary underline" to={`/research/runs?run=${encodeURIComponent(receipt.run_id)}`}>{tl({ zh: "查看研究运行", en: "View research run" })}</Link>
          {receipt.job_id && <Link className="text-primary underline" to={`/jobs?job=${encodeURIComponent(receipt.job_id)}`}>{tl({ zh: "跟进后台任务", en: "Follow background job" })}</Link>}
        </div>
      )}
      {mutationError && (
        <p role="alert" className="text-sm text-destructive">
          {String(mutationError)}
        </p>
      )}
      {open && topic.goal && (
        <ResearchRunSetup
          key={`${topic.topic_id}:${topic.goal.version}:${operationId}`}
          onStart={(payload) => start.mutate(payload)}
          pending={start.isPending}
          error={start.error}
          onRepreview={() => start.reset()}
          onCancel={() => setOpen(false)}
        />
      )}
      {runsQuery.isLoading ? (
        <LoadingState />
      ) : runsQuery.isError ? (
        <ErrorState message={String(runsQuery.error)} onRetry={() => runsQuery.refetch()} />
      ) : runItems.length === 0 ? (
        <p className="text-sm text-muted-foreground">{tl({ zh: "尚未发起运行；轮次记录不能替代运行事实。", en: "No research runs have started yet." })}</p>
      ) : (
        <ul className="space-y-3">
          {runItems.map((run) => (
            <li key={run.link_id} className="rounded-md border border-border p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <Link className="font-mono text-primary underline" to={run.deep_link}>
                  {run.run_id}
                </Link>
                <span className="rounded bg-muted px-2 py-0.5 text-xs">
                  {ARTIFACT_STATES[run.state]?.[lang] ?? run.state}
                </span>
                {run.job_id && <span className="font-mono text-xs text-muted-foreground">{run.job_id}</span>}
                <span className="text-xs text-muted-foreground">{tl({ zh: "目标 ", en: "Goal " })}{run.goal_version}</span>
              </div>
              {run.progress_total ? (
                <p className="mt-1 text-xs text-muted-foreground">
                  {tl({ zh: "进度 ", en: "Progress " })}{run.progress_done ?? 0}/{run.progress_total}
                  {run.phase ? ` · ${run.phase}` : ""}
                </p>
              ) : run.phase ? (
                <p className="mt-1 text-xs text-muted-foreground">{run.phase}</p>
              ) : null}
              {run.error_summary && (
                <p className="mt-1 break-words text-xs text-destructive">{run.error_summary}</p>
              )}
              {run.result_checksum && (
                <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
                  result {run.result_checksum}
                </p>
              )}
              <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                {run.operation_id && <span className="font-mono">op {run.operation_id}</span>}
                <ResearchTimestamp value={run.created_at} />
                {(run.state === "succeeded" || run.state === "interrupted") && (
                  <Button size="sm" variant="outline" onClick={() => retry.mutate(run)} disabled={retry.isPending}>
                    {tl({ zh: "重放运行", en: "Replay run" })}
                  </Button>
                )}
                {!TERMINAL_TOPIC_RUN_STATES.has(run.state) && (
                  <Button size="sm" variant="outline" onClick={() => cancel.mutate(run)} disabled={cancel.isPending}>
                    {tl({ zh: "取消", en: "Cancel" })}
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ConflictPanel({
  conflict,
  canKeepNote,
  onReload,
  onKeepNote,
  onClose,
}: {
  conflict: TopicConflictDetail;
  canKeepNote: boolean;
  onReload: () => void;
  onKeepNote: () => void;
  onClose: () => void;
}) {
  const { tl, lang } = useT();
  return (
    <section
      role="dialog"
      aria-label={tl({ zh: "课题冲突", en: "Topic conflict" })}
      className="space-y-3 rounded-lg border border-destructive/50 p-4"
    >
      <h2 className="font-semibold">{tl({ zh: "课题已被其他会话更新，本次修改未保存", en: "Another session updated this topic. Your change was not saved." })}</h2>
      {conflict.code === "illegal_transition" ? (
        <>
          <p className="text-sm">
            {tl({ zh: "当前状态 ", en: "Current status " })}{STATES[conflict.current ?? ""]?.[lang] ?? conflict.current}{tl({ zh: " 不允许该操作；允许的目标状态：", en: " blocks this action. Allowed target states: " })}
            {(conflict.allowed ?? []).map((s) => STATES[s]?.[lang] ?? s).join(" / ") || tl({ zh: "无", en: "None" })}
          </p>
          {conflict.actions?.length ? (
            <p className="text-sm text-muted-foreground">
              {tl({ zh: "可用动作：", en: "Available actions: " })}{conflict.actions.map((a) => ACTION_LABELS[a]?.[lang] ?? a).join(" / ")}{tl({ zh: "（走 POST /transitions，PUT 不能改变状态）", en: "(use POST /transitions to change status)" })}
            </p>
          ) : null}
        </>
      ) : conflict.code === "idempotency_conflict" ? (
        <p className="text-sm">{tl({ zh: "幂等键 ", en: "Idempotency key " })}{conflict.idempotency_key}{tl({ zh: " 已绑定内容不同的课题。", en: " already belongs to a topic with different content." })}</p>
      ) : (
        <>
          <p className="text-sm">
            {tl({ zh: "当前 revision ", en: "Current revision " })}{conflict.current_revision ?? "?"}{tl({ zh: "，你的 revision", en: ", your revision" })}{" "}
            {conflict.your_revision ?? "?"}{tl({ zh: "。已刷新到最新版本，你的改动没有覆盖对方内容。", en: ". The latest version is now loaded and the other changes are intact." })}
          </p>
          {conflict.changed_fields?.length ? (
            <p className="text-sm">{tl({ zh: "变化的字段：", en: "Changed fields: " })}{conflict.changed_fields.join(lang === "zh" ? "、" : ", ")}</p>
          ) : null}
          {conflict.current_summary && (
            <p className="text-sm text-muted-foreground">
              {tl({ zh: "当前：状态 ", en: "Current status " })}{STATES[conflict.current_summary.status ?? ""]?.[lang] ?? conflict.current_summary.status}
              {" · "}{tl({ zh: "目标 ", en: "Goal " })}{conflict.current_summary.goal_version ?? tl({ zh: "未补齐", en: "Incomplete" })}
              {" · "}r{conflict.current_summary.revision}
            </p>
          )}
        </>
      )}
      <div className="flex flex-wrap gap-2">
        <Button onClick={onReload}>{tl({ zh: "重新加载最新版本", en: "Reload latest version" })}</Button>
        {canKeepNote && (
          <Button variant="outline" onClick={onKeepNote}>
            {tl({ zh: "仅保留我的说明（追加轮次）", en: "Keep my notes in a new round" })}
          </Button>
        )}
        <Button variant="outline" onClick={onClose}>
          {tl({ zh: "关闭", en: "Close" })}
        </Button>
      </div>
    </section>
  );
}

export default function ResearchTopics() {
  const { tl, lang } = useT();
  const [params, setParams] = useSearchParams();
  const topicId = params.get("topic");
  const memoryId = params.get("memory");
  const [offset, setOffset] = useState(0);
  const [entryOffset, setEntryOffset] = useState(0);
  const [memoryOffset, setMemoryOffset] = useState(0);
  const [statusFilter, setStatusFilter] = useState("");
  const [sort, setSort] = useState("updated");
  const tab = params.get("tab") ?? (memoryId ? "memories" : "topics");
  const setTab = (value: string) =>
    setParams(
      value === "topics"
        ? topicId
          ? { topic: topicId }
          : {}
        : { tab: value, ...(memoryId ? { memory: memoryId } : {}) },
    );
  const [draftForm, setDraftForm] = useState<TopicDraftInput | null>(null);
  const [draftKey, setDraftKey] = useState("");
  const [draftEdit, setDraftEdit] = useState<TopicDraftUpdate | null>(null);
  const [activation, setActivation] = useState<ActivateInput | null>(null);
  const [lifecycle, setLifecycle] = useState<{ action: string; reason: string } | null>(null);
  const [conflict, setConflict] = useState<TopicConflictDetail | null>(null);
  const [form, setForm] = useState<TopicInput | null>(null);
  const [round, setRound] = useState<RoundInput | null>(null);
  const [key, setKey] = useState("");
  // Base revision is pinned when a form opens: a background refetch must not
  // silently upgrade it, otherwise the stale-write 409 never surfaces.
  const [pinnedRevision, setPinnedRevision] = useState<number | null>(null);
  const qc = useQueryClient();
  const topics = useQuery({
    queryKey: ["topics", offset, statusFilter, sort],
    queryFn: () => workspaceApi.topics(offset, statusFilter || undefined, sort),
  });
  const topic = useQuery({
    queryKey: ["topic", topicId],
    queryFn: () => workspaceApi.topic(topicId!),
    enabled: !!topicId,
  });
  const entries = useQuery({
    queryKey: ["topic-entries", topicId, entryOffset],
    queryFn: () => workspaceApi.entries(topicId!, entryOffset),
    enabled: !!topicId,
  });
  const memories = useQuery({
    queryKey: ["memory-page", memoryOffset],
    queryFn: () => workspaceApi.memories(memoryOffset),
    enabled: tab === "memories" && !memoryId,
  });
  const memory = useQuery({
    queryKey: ["memory-selected", memoryId],
    queryFn: () => fetchJSON<MemoryDetail>(`/research/memories/${encodeURIComponent(memoryId!)}`),
    enabled: tab === "memories" && !!memoryId,
  });
  const onConflict = (error: unknown) => {
    const detail = conflictDetail(error);
    if (detail) setConflict(detail);
  };
  const refreshTopic = () => {
    void qc.invalidateQueries({ queryKey: ["topics"] });
    void qc.invalidateQueries({ queryKey: ["topic"] });
    void qc.invalidateQueries({ queryKey: ["topic-entries"] });
    void qc.invalidateQueries({ queryKey: ["topic-context"] });
  };
  const saveDraft = useMutation({
    mutationFn: () => workspaceApi.createDraft(draftForm!, draftKey),
    onSuccess: (t) => {
      setDraftForm(null);
      setParams({ topic: t.topic_id });
      refreshTopic();
    },
    onError: onConflict,
  });
  const activateTopic = useMutation({
    mutationFn: () =>
      workspaceApi.activate(
        topicId!,
        activation!,
        pinnedRevision ?? topic.data?.revision ?? 1,
      ),
    onSuccess: () => {
      setActivation(null);
      setPinnedRevision(null);
      refreshTopic();
    },
    onError: onConflict,
  });
  const changeLifecycle = useMutation({
    mutationFn: () =>
      workspaceApi.transition(
        topicId!,
        lifecycle!.action,
        lifecycle!.reason,
        pinnedRevision ?? topic.data?.revision ?? 1,
      ),
    onSuccess: () => {
      setLifecycle(null);
      setPinnedRevision(null);
      refreshTopic();
    },
    onError: onConflict,
  });
  const saveDraftEdit = useMutation({
    mutationFn: () =>
      workspaceApi.updateDraft(
        topicId!,
        draftEdit!,
        pinnedRevision ?? topic.data?.revision ?? 1,
      ),
    onSuccess: () => {
      setDraftEdit(null);
      setPinnedRevision(null);
      refreshTopic();
    },
    onError: onConflict,
  });
  const save = useMutation({
    mutationFn: () =>
      topicId && topic.data
        ? workspaceApi.update(topicId, form!, pinnedRevision ?? topic.data.revision)
        : workspaceApi.create(form!),
    onSuccess: (t) => {
      setForm(null);
      setPinnedRevision(null);
      setParams({ topic: t.topic_id });
      refreshTopic();
    },
    onError: onConflict,
  });
  const append = useMutation({
    mutationFn: () => workspaceApi.append(topicId!, round!, key),
    onSuccess: () => {
      setRound(null);
      refreshTopic();
    },
    onError: onConflict,
  });
  const setValue = <K extends keyof TopicInput>(name: K, value: TopicInput[K]) =>
    setForm((f) => (f ? { ...f, [name]: value } : f));
  const setRoundValue = <K extends keyof RoundInput>(name: K, value: RoundInput[K]) =>
    setRound((r) => (r ? { ...r, [name]: value } : r));
  const startRound = (conclusion = "", entryType: RoundInput["entry_type"] = "round") => {
    setKey(crypto.randomUUID());
    setRound({
      entry_type: entryType,
      goal_version: topic.data?.goal?.version ?? "",
      objective: "",
      action: "",
      rationale: "",
      outcome: "completed",
      conclusion,
      confidence: "unknown",
      next_step: "",
      source_refs: [],
      supersedes_id: null,
      branch: "main",
      document_ref:
        entryType === "review" ? { kind: "document", ref_id: "", checksum: "" } : null,
    });
  };
  const openDraftForm = () => {
    setParams({});
    setDraftForm({ ...EMPTY_DRAFT });
    setDraftKey(crypto.randomUUID());
    setForm(null);
    setActivation(null);
    setDraftEdit(null);
    setRound(null);
    setLifecycle(null);
    setConflict(null);
    setPinnedRevision(null);
    saveDraft.reset();
  };
  const reloadLatest = () => {
    setConflict(null);
    setForm(null);
    setActivation(null);
    setDraftEdit(null);
    setLifecycle(null);
    setPinnedRevision(null);
    refreshTopic();
  };
  return (
    <div className="space-y-6">
      <PageHeader
        title={tl({ zh: "研究课题", en: "Research topics" })}
        description={tl({
          zh: "先保存问题，再补齐目标与来源后激活；围绕问题组织每轮尝试、证据和下一步，随时暂停与续接。",
          en: "Save the question first, then add a goal and source to activate the topic. Use it to track attempts, evidence, and next steps across sessions.",
        })}
        actions={<Button onClick={openDraftForm}>{tl({ zh: "新建课题", en: "New topic" })}</Button>}
      />
      <div className="flex flex-wrap gap-2">
        <Button variant={tab === "topics" ? "default" : "outline"} onClick={() => setTab("topics")}>
          {tl({ zh: "课题与轮次", en: "Topics and rounds" })}
        </Button>
        <Button
          variant={tab === "memories" ? "default" : "outline"}
          onClick={() => setTab("memories")}
        >
          {tl({ zh: "研究记忆", en: "Research memory" })}
        </Button>
        <Button variant="outline" asChild>
          <Link to="/research/docs">{tl({ zh: "正式研究资料", en: "Canonical research documents" })}</Link>
        </Button>
        <Button variant="outline" asChild>
          <Link to="/research/tools">{tl({ zh: "高级研究工具", en: "Advanced research tools" })}</Link>
        </Button>
        <Button variant="outline" asChild>
          <Link
            to={`/research/agent-connect${topicId ? `?topic=${encodeURIComponent(topicId)}` : ""}`}
          >
            {tl({ zh: "继续与 agent 研究", en: "Continue research with an agent" })}
          </Link>
        </Button>
      </div>
      {params.get("source_kind") && params.get("source_id") && (
        <section className="rounded-md border border-border p-4">
          <h2 className="font-semibold">{tl({ zh: "产物来源：", en: "Artifact source: " })}{params.get("source_id")}</h2>
          <SourceFact
            reference={{ kind: params.get("source_kind")!, ref_id: params.get("source_id")! }}
          />
          <Link
            className="text-sm text-primary underline"
            to={
              params.get("source_kind") === "simulation"
                ? "/research/simulation"
                : params.get("source_kind") === "backtest"
                  ? "/backtest"
                  : "/research/factors"
            }
          >
            {tl({ zh: "打开对应研究工具", en: "Open research tool" })}
          </Link>
        </section>
      )}
      <p className="max-w-prose text-sm text-muted-foreground">
        {tl({
          zh: "自动产物事实、agent 解释与工作结论各有来源。正式接受的结论以仓库研究文档为准，冲突时保留双方记录；旧记忆中的故障和假设需重新核验。保存课题与执行研究严格分开，保存不会启动任何运行。",
          en: "Artifact facts, agent interpretations, and working conclusions have separate sources. The repository's research documents hold the canonical conclusions. Preserve conflicting records and verify old failures and hypotheses against current evidence. To run research, use the separate run action.",
        })}
      </p>
      {conflict && (
        <ConflictPanel
          conflict={conflict}
          canKeepNote={Boolean(topic.data?.goal) && topic.data?.status !== "draft"}
          onReload={reloadLatest}
          onKeepNote={() => {
            const note = form?.summary ?? activation?.summary ?? draftEdit?.summary ?? "";
            setConflict(null);
            setForm(null);
            setActivation(null);
            setDraftEdit(null);
            startRound(note);
          }}
          onClose={() => setConflict(null)}
        />
      )}
      {draftForm && (
        <form
          className="space-y-4 rounded-lg border border-border p-4"
          onSubmit={(e) => {
            e.preventDefault();
            saveDraft.mutate();
          }}
        >
          <h2 className="text-lg font-semibold">{tl({ zh: "第一步：保存研究问题草稿", en: "Step 1: Save the research question as a draft" })}</h2>
          <p className="text-sm text-muted-foreground">
            {tl({
              zh: "草稿只保存问题本身并生成稳定 RT-ID；目标版本、门槛与来源在第二步补齐后才会激活。保存与执行分开。",
              en: "Saving the question creates a stable RT ID. In step 2, add the goal version, acceptance criteria, and source to activate the topic.",
            })}
          </p>
          <Field
            label={tl({ zh: "课题标题", en: "Topic title" })}
            required
            value={draftForm.title}
            onChange={(v) => setDraftForm({ ...draftForm, title: v })}
          />
          <Field
            label={tl({ zh: "研究问题", en: "Research question" })}
            required
            multiline
            value={draftForm.question}
            onChange={(v) => setDraftForm({ ...draftForm, question: v })}
          />
          {saveDraft.isError && (
            <p role="alert" className="text-destructive">
              {String(saveDraft.error)}
            </p>
          )}
          <div className="flex gap-2">
            <Button type="submit" disabled={saveDraft.isPending}>
              {tl({ zh: "保存草稿", en: "Save draft" })}
            </Button>
            <Button type="button" variant="outline" onClick={() => setDraftForm(null)}>
              {tl({ zh: "取消", en: "Cancel" })}
            </Button>
          </div>
        </form>
      )}
      {form && (
        <form
          className="space-y-4 rounded-lg border border-border p-4"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <h2 className="text-lg font-semibold">
            {topicId ? tl({ zh: "更新课题与目标版本", en: "Update topic and goal version" }) : tl({ zh: "建立研究课题", en: "Create research topic" })}
          </h2>
          <Field
            label={tl({ zh: "课题标题", en: "Topic title" })}
            required
            value={form.title}
            onChange={(v) => setValue("title", v)}
          />
          <Field
            label={tl({ zh: "研究问题", en: "Research question" })}
            required
            multiline
            value={form.question}
            onChange={(v) => setValue("question", v)}
          />
          <div className="grid gap-4 md:grid-cols-2">
            <Field
              label={tl({ zh: "目标版本", en: "Goal version" })}
              required
              value={form.goal.version}
              onChange={(v) => setValue("goal", { ...form.goal, version: v })}
            />
            <TopicGoalSource
              value={form.goal.source.ref_id}
              onChange={(v) =>
                setValue("goal", { ...form.goal, source: { kind: "document", ref_id: v } })
              }
            />
          </div>
          <Field
            label={tl({ zh: "目标和验收门槛", en: "Goal and acceptance criteria" })}
            required
            multiline
            value={form.goal.criteria}
            onChange={(v) => setValue("goal", { ...form.goal, criteria: v })}
          />
          <div className="grid gap-4 md:grid-cols-2">
            <Choice
              label={tl({ zh: "课题状态", en: "Topic status" })}
              value={form.status}
              values={["active", "paused", "closed"]}
              onChange={(v) => setValue("status", v)}
            />
            <Choice
              label={tl({ zh: "工作结论（不替代正式结论）", en: "Working conclusion" })}
              value={form.conclusion}
              values={["unknown", "supported", "not_supported", "insufficient_evidence"]}
              onChange={(v) => setValue("conclusion", v)}
            />
          </div>
          <Field
            label={tl({ zh: "当前摘要", en: "Current summary" })}
            multiline
            value={form.summary}
            onChange={(v) => setValue("summary", v)}
          />
          <Field
            label={tl({ zh: "开放问题（每行一个）", en: "Open questions (one per line)" })}
            multiline
            value={form.open_questions.join("\n")}
            onChange={(v) => setValue("open_questions", v.split("\n").filter(Boolean))}
          />
          <Field label={tl({ zh: "下一步", en: "Next step" })} value={form.next_step} onChange={(v) => setValue("next_step", v)} />
          {save.isError && (
            <p role="alert" className="text-destructive">
              {String(save.error)}
            </p>
          )}
          <div className="flex gap-2">
            <Button type="submit" disabled={save.isPending}>
              {tl({ zh: "保存研究记录", en: "Save research record" })}
            </Button>
            <Button type="button" variant="outline" onClick={() => setForm(null)}>
              {tl({ zh: "取消", en: "Cancel" })}
            </Button>
          </div>
        </form>
      )}
      {tab === "topics" && !form && !draftForm && (
        <div className="grid gap-6 lg:grid-cols-[18rem_minmax(0,1fr)]">
          <aside className="space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Label htmlFor="topic-status-filter">{tl({ zh: "状态", en: "Status" })}</Label>
              <select
                id="topic-status-filter"
                aria-label={tl({ zh: "按状态筛选", en: "Filter by status" })}
                className="h-8 rounded-md border border-input bg-background px-2 text-sm"
                value={statusFilter}
                onChange={(e) => {
                  setStatusFilter(e.target.value);
                  setOffset(0);
                }}
              >
                <option value="">{tl({ zh: "全部", en: "All" })}</option>
                <option value="draft">{tl({ zh: "草稿", en: "Draft" })}</option>
                <option value="active">{tl({ zh: "研究中", en: "Active" })}</option>
                <option value="paused">{tl({ zh: "已暂停", en: "Paused" })}</option>
                <option value="closed">{tl({ zh: "已收束", en: "Closed" })}</option>
              </select>
              <Label htmlFor="topic-sort">{tl({ zh: "排序", en: "Sort" })}</Label>
              <select
                id="topic-sort"
                aria-label={tl({ zh: "排序方式", en: "Sort order" })}
                className="h-8 rounded-md border border-input bg-background px-2 text-sm"
                value={sort}
                onChange={(e) => {
                  setSort(e.target.value);
                  setOffset(0);
                }}
              >
                <option value="updated">{tl({ zh: "最近更新", en: "Recently updated" })}</option>
                <option value="created">{tl({ zh: "创建时间", en: "Date created" })}</option>
              </select>
            </div>
            {topics.isLoading ? (
              <LoadingState />
            ) : topics.isError ? (
              <ErrorState message={String(topics.error)} onRetry={() => topics.refetch()} />
            ) : topics.data?.items.length === 0 ? (
              <EmptyState
                title={tl({ zh: "还没有研究课题", en: "No research topics yet" })}
                description={tl({
                  zh: "先保存一个问题作为草稿，补齐目标与来源后再激活。",
                  en: "Save a question as a draft, then add a goal and source to activate it.",
                })}
              />
            ) : (
              topics.data?.items.map((t) => (
                <button
                  className={`w-full rounded-md border border-border p-3 text-left hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring ${t.topic_id === topicId ? "bg-accent" : ""}`}
                  key={t.topic_id}
                  onClick={() => {
                    setParams({ topic: t.topic_id });
                    setEntryOffset(0);
                    setRound(null);
                    setActivation(null);
                    setDraftEdit(null);
                    setLifecycle(null);
                    setConflict(null);
                    setPinnedRevision(null);
                  }}
                >
                  <p className="font-medium">{t.title}</p>
                  <p className="mt-2 text-xs text-muted-foreground">
                    {STATES[t.status]?.[lang] ?? t.status} · {STATES[t.conclusion]?.[lang] ?? t.conclusion}
                    {t.updated_at ? ` · ${t.updated_at.slice(0, 10)}` : ""}
                  </p>
                  <p className="mt-1 line-clamp-2 text-sm">{t.next_step || t.question}</p>
                </button>
              ))
            )}
            <Pager offset={offset} more={topics.data?.has_more} onChange={setOffset} />
          </aside>
          <main className="min-w-0 space-y-6">
            {!topicId && (
              <EmptyState
                title={tl({ zh: "选择一个研究问题", en: "Choose a research question" })}
                description={tl({
                  zh: "查看它的目标版本、证据缺口、失败尝试和下一步，无需按页面顺序操作。",
                  en: "Open a topic to see its goal version, evidence gaps, failed attempts, and next step.",
                })}
              />
            )}
            {topic.isLoading && <LoadingState />}
            {topic.isError && (
              <ErrorState message={String(topic.error)} onRetry={() => topic.refetch()} />
            )}
            {topic.data && (
              <>
                <section className="space-y-3 border-b border-border pb-5">
                  <h2 className="text-xl font-semibold">{topic.data.title}</h2>
                  <p className="max-w-prose text-sm">{topic.data.question}</p>
                  <p className="text-sm text-muted-foreground">
                    {STATES[topic.data.status]?.[lang] ?? topic.data.status} ·{" "}
                    {STATES[topic.data.conclusion]?.[lang] ?? topic.data.conclusion} · revision{" "}
                    {topic.data.revision}
                    {topic.data.updated_at ? (
                      <>
                        {" · "}{tl({ zh: "最近更新 ", en: "Recently updated " })}<ResearchTimestamp value={topic.data.updated_at} />
                      </>
                    ) : null}
                  </p>
                  {topic.data.goal ? (
                    <>
                      <p className="text-sm">
                        {tl({
                          zh: `当前目标 ${topic.data.goal.version}：${topic.data.goal.criteria}`,
                          en: `Current goal ${topic.data.goal.version}: ${topic.data.goal.criteria}`,
                        })}
                      </p>
                      <EvidenceLinks refs={[topic.data.goal.source]} />
                    </>
                  ) : (
                    <p className="text-sm text-warning">
                      {tl({
                        zh: "尚未补齐目标版本与验收门槛；补齐并激活前不能发起研究。",
                        en: "Add a goal version and acceptance criteria, then activate the topic to start research.",
                      })}
                    </p>
                  )}
                  <p className="max-w-prose whitespace-pre-wrap text-sm">
                    {topic.data.summary || tl({ zh: "尚无工作摘要", en: "No working summary yet" })}
                  </p>
                  <h3 className="font-medium">{tl({ zh: "证据缺口", en: "Evidence gaps" })}</h3>
                  <ul className="list-disc space-y-1 pl-5 text-sm">
                    {topic.data.open_questions.map((x) => (
                      <li key={x}>{x}</li>
                    ))}
                  </ul>
                  <p className="text-sm">{tl({ zh: "下一步：", en: "Next step: " })}{topic.data.next_step || tl({ zh: "尚未记录", en: "Not recorded" })}</p>
                  <div className="flex flex-wrap gap-2">
                    {topic.data.status !== "draft" && (
                      <>
                        <Button onClick={() => startRound()}>{tl({ zh: "记录新轮次（执行后）", en: "Record a completed round" })}</Button>
                        <Button variant="outline" onClick={() => startRound("", "plan")}>
                          {tl({ zh: "只保存计划（不执行）", en: "Save plan only" })}
                        </Button>
                        <Button variant="outline" onClick={() => startRound("", "review")}>
                          {tl({ zh: "记录复核结论", en: "Record reviewed conclusion" })}
                        </Button>
                      </>
                    )}
                    {topic.data.status === "draft" ? (
                      <>
                        <Button
                          variant="outline"
                          onClick={() => {
                            setPinnedRevision(topic.data!.revision);
                            setActivation(null);
                            setDraftEdit({
                              title: topic.data!.title,
                              question: topic.data!.question,
                              conclusion: topic.data!.conclusion,
                              summary: topic.data!.summary,
                              open_questions: topic.data!.open_questions,
                              next_step: topic.data!.next_step,
                            });
                            saveDraftEdit.reset();
                          }}
                        >
                          {tl({ zh: "编辑草稿", en: "Edit draft" })}
                        </Button>
                        <Button
                          variant="outline"
                          onClick={() => {
                            setPinnedRevision(topic.data!.revision);
                            setDraftEdit(null);
                            setActivation(activationFrom(topic.data!));
                            saveDraft.reset();
                          }}
                        >
                          {tl({ zh: "补齐目标并激活", en: "Complete goal and activate" })}
                        </Button>
                      </>
                    ) : (
                      <Button
                        variant="outline"
                        onClick={() => {
                          setPinnedRevision(topic.data!.revision);
                          setForm({
                            title: topic.data!.title,
                            question: topic.data!.question,
                            goal: topic.data!.goal ?? EMPTY_GOAL,
                            status: topic.data!.status,
                            conclusion: topic.data!.conclusion,
                            summary: topic.data!.summary,
                            open_questions: topic.data!.open_questions,
                            next_step: topic.data!.next_step,
                          });
                          save.reset();
                        }}
                      >
                        {tl({ zh: "更新记录", en: "Update record" })}
                      </Button>
                    )}
                    {lifecycleActions(topic.data.status).map((action) => (
                      <Button
                        key={action}
                        variant="outline"
                        onClick={() => {
                          setPinnedRevision(topic.data!.revision);
                          setLifecycle({ action, reason: "" });
                          changeLifecycle.reset();
                        }}
                      >
                        {ACTION_LABELS[action]?.[lang]}
                      </Button>
                    ))}
                  </div>
                  <div className="flex flex-wrap gap-3 text-sm">
                    <Link className="text-primary underline" to="/research/strategy">
                      {tl({ zh: "策略", en: "Strategies" })}
                    </Link>
                    <Link className="text-primary underline" to="/research/runs">
                      {tl({ zh: "研究回测", en: "Research backtest" })}
                    </Link>
                    <Link className="text-primary underline" to="/research/experiments">
                      {tl({ zh: "OOS 验证", en: "OOS validation" })}
                    </Link>
                    <Link
                      className="text-primary underline"
                      to={`/data?tab=overview&topic=${encodeURIComponent(topic.data.topic_id)}`}
                    >
                      {tl({ zh: "数据工具", en: "Data tools" })}
                    </Link>
                    <Link className="text-primary underline" to="/research/factors">
                      {tl({ zh: "因子工具", en: "Factor tools" })}
                    </Link>
                  </div>
                  {/* 课题数据视图(#512):只读消费 /api/data/catalog/topics/{id}/coverage */}
                  <TopicDataCard topicId={topic.data.topic_id} bare />
                </section>
                {draftEdit && topic.data.status === "draft" && (
                  <form
                    className="space-y-4 rounded-lg border border-border p-4"
                    onSubmit={(e) => {
                      e.preventDefault();
                      saveDraftEdit.mutate();
                    }}
                  >
                    <h3 className="font-semibold">{tl({ zh: "编辑草稿（不改变生命周期状态）", en: "Edit draft" })}</h3>
                    <p className="text-sm text-muted-foreground">
                      {tl({
                        zh: "草稿只保存问题与工作笔记；目标版本、门槛与来源须在“补齐目标并激活”时提供，PUT 不能改变状态。",
                        en: "Save the question and working notes here. Use Complete goal and activate to add the goal version, criteria, and source.",
                      })}
                    </p>
                    <Field
                      label={tl({ zh: "课题标题", en: "Topic title" })}
                      required
                      value={draftEdit.title}
                      onChange={(v) => setDraftEdit({ ...draftEdit, title: v })}
                    />
                    <Field
                      label={tl({ zh: "研究问题", en: "Research question" })}
                      required
                      multiline
                      value={draftEdit.question}
                      onChange={(v) => setDraftEdit({ ...draftEdit, question: v })}
                    />
                    <Field
                      label={tl({ zh: "当前摘要", en: "Current summary" })}
                      multiline
                      value={draftEdit.summary ?? ""}
                      onChange={(v) => setDraftEdit({ ...draftEdit, summary: v })}
                    />
                    <Field
                      label={tl({ zh: "开放问题（每行一个）", en: "Open questions (one per line)" })}
                      multiline
                      value={(draftEdit.open_questions ?? []).join("\n")}
                      onChange={(v) =>
                        setDraftEdit({ ...draftEdit, open_questions: v.split("\n").filter(Boolean) })
                      }
                    />
                    <Field
                      label={tl({ zh: "下一步", en: "Next step" })}
                      value={draftEdit.next_step ?? ""}
                      onChange={(v) => setDraftEdit({ ...draftEdit, next_step: v })}
                    />
                    {saveDraftEdit.isError && (
                      <p role="alert" className="text-destructive">
                        {String(saveDraftEdit.error)}
                      </p>
                    )}
                    <div className="flex gap-2">
                      <Button type="submit" disabled={saveDraftEdit.isPending}>
                        {tl({ zh: "保存草稿修改", en: "Save draft changes" })}
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => {
                          setDraftEdit(null);
                          setPinnedRevision(null);
                        }}
                      >
                        {tl({ zh: "取消", en: "Cancel" })}
                      </Button>
                    </div>
                  </form>
                )}
                {activation && topic.data.status === "draft" && (
                  <form
                    className="space-y-4 rounded-lg border border-border p-4"
                    onSubmit={(e) => {
                      e.preventDefault();
                      activateTopic.mutate();
                    }}
                  >
                    <h3 className="font-semibold">{tl({ zh: "第二步：补齐目标、门槛与来源后激活", en: "Step 2: Add the goal, criteria, and source" })}</h3>
                    <p className="text-sm text-muted-foreground">
                      {tl({ zh: "激活只表示课题具备可执行目标，不会自动发起研究运行。", en: "Activating the topic enables you to start research separately." })}
                    </p>
                    <div className="grid gap-4 md:grid-cols-2">
                      <Field
                        label={tl({ zh: "目标版本", en: "Goal version" })}
                        required
                        value={activation.goal.version}
                        onChange={(v) =>
                          setActivation({ ...activation, goal: { ...activation.goal, version: v } })
                        }
                      />
                      <TopicGoalSource
                        value={activation.goal.source.ref_id}
                        onChange={(v) =>
                          setActivation({
                            ...activation,
                            goal: {
                              ...activation.goal,
                              source: { kind: "document", ref_id: v },
                            },
                          })
                        }
                      />
                    </div>
                    <Field
                      label={tl({ zh: "目标和验收门槛", en: "Goal and acceptance criteria" })}
                      required
                      multiline
                      value={activation.goal.criteria}
                      onChange={(v) =>
                        setActivation({ ...activation, goal: { ...activation.goal, criteria: v } })
                      }
                    />
                    <Field
                      label={tl({ zh: "当前摘要", en: "Current summary" })}
                      multiline
                      value={activation.summary ?? ""}
                      onChange={(v) => setActivation({ ...activation, summary: v })}
                    />
                    <Field
                      label={tl({ zh: "开放问题（每行一个）", en: "Open questions (one per line)" })}
                      multiline
                      value={(activation.open_questions ?? []).join("\n")}
                      onChange={(v) =>
                        setActivation({
                          ...activation,
                          open_questions: v.split("\n").filter(Boolean),
                        })
                      }
                    />
                    <Field
                      label={tl({ zh: "下一步", en: "Next step" })}
                      value={activation.next_step ?? ""}
                      onChange={(v) => setActivation({ ...activation, next_step: v })}
                    />
                    {activateTopic.isError && (
                      <p role="alert" className="text-destructive">
                        {String(activateTopic.error)}
                      </p>
                    )}
                    <div className="flex gap-2">
                      <Button type="submit" disabled={activateTopic.isPending}>
                        {tl({ zh: "激活课题", en: "Activate topic" })}
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => setActivation(null)}
                      >
                        {tl({ zh: "稍后再说", en: "Later" })}
                      </Button>
                    </div>
                  </form>
                )}
                {lifecycle && (
                  <form
                    role="dialog"
                    aria-label={tl({ zh: "确认生命周期操作", en: "Confirm lifecycle action" })}
                    className="space-y-3 rounded-lg border border-border p-4"
                    onSubmit={(e) => {
                      e.preventDefault();
                      changeLifecycle.mutate();
                    }}
                  >
                    <h3 className="font-semibold">{ACTION_LABELS[lifecycle.action]?.[lang]}</h3>
                    <p className="text-sm text-muted-foreground">
                      {ACTION_HINTS[lifecycle.action]?.[lang]}{tl({ zh: " 当前 revision ", en: " Current revision " })}{topic.data.revision}{tl({ zh: "；确认后写入不可改写的生命周期记录。", en: ". Confirmation writes a permanent lifecycle record." })}
                    </p>
                    <Field
                      label={
                        REASON_REQUIRED.has(lifecycle.action) ? tl({ zh: "原因（必填）", en: "Reason (required)" }) : tl({ zh: "原因（可选）", en: "Reason (optional)" })
                      }
                      required={REASON_REQUIRED.has(lifecycle.action)}
                      multiline
                      value={lifecycle.reason}
                      onChange={(v) => setLifecycle({ ...lifecycle, reason: v })}
                    />
                    {changeLifecycle.isError && (
                      <p role="alert" className="text-destructive">
                        {String(changeLifecycle.error)}
                      </p>
                    )}
                    <div className="flex gap-2">
                      <Button type="submit" disabled={changeLifecycle.isPending}>
                        {tl({
                          zh: "确认" + ACTION_LABELS[lifecycle.action]?.zh,
                          en: "Confirm: " + ACTION_LABELS[lifecycle.action]?.en,
                        })}
                      </Button>
                      <Button type="button" variant="outline" onClick={() => setLifecycle(null)}>
                        {tl({ zh: "取消", en: "Cancel" })}
                      </Button>
                    </div>
                  </form>
                )}
                <ContextCard topicId={topic.data.topic_id} />
                <TopicRunsCard topic={topic.data} />
                <TopicArtifactsCard topic={topic.data} />
                {round && (
                  <form
                    className="space-y-4 rounded-lg border border-border p-4"
                    onSubmit={(e) => {
                      e.preventDefault();
                      append.mutate();
                    }}
                  >
                    <h3 className="font-semibold">
                      {round.entry_type === "plan"
                        ? tl({ zh: "只保存计划（设计意图，不执行）", en: "Save plan only" })
                        : round.entry_type === "review"
                          ? tl({ zh: "记录复核结论（需正式文档与 checksum）", en: "Record reviewed conclusion (document and checksum required)" })
                          : tl({ zh: "追加轮次，保留本次结果", en: "Add a round to keep this result" })}
                    </h3>
                    <div className="grid gap-4 md:grid-cols-2">
                      <Choice
                        label={tl({ zh: "条目类型", en: "Entry type" })}
                        value={round.entry_type}
                        values={["round", "evidence", "plan", "review"]}
                        onChange={(v) => {
                          setRoundValue("entry_type", v as RoundInput["entry_type"]);
                          setRoundValue(
                            "document_ref",
                            v === "review"
                              ? { kind: "document", ref_id: "", checksum: "" }
                              : null,
                          );
                        }}
                      />
                      {round.entry_type !== "plan" && (
                        <Choice
                          label={tl({ zh: "尝试结果", en: "Attempt outcome" })}
                          value={round.outcome}
                          values={["completed", "failed", "interrupted", "rejected", "paused"]}
                          onChange={(v) => setRoundValue("outcome", v)}
                        />
                      )}
                    </div>
                    {round.entry_type === "plan" && (
                      <p className="text-xs text-muted-foreground">
                        {tl({
                          zh: "计划只记录设计（因子公式、窗口、方向、预注册假设、试验预算），永远不会启动运行； 真正执行请使用上方“发起研究运行”或 OOS 卡片中的显式按钮。",
                          en: "A plan records the formula, window, direction, preregistered hypothesis, and trial budget. To execute it, use Start research run or the explicit action in the OOS section.",
                        })}
                      </p>
                    )}
                    {round.entry_type === "review" && (
                      <div className="space-y-2 rounded-md border border-border p-3">
                        <p className="text-xs text-muted-foreground">
                          {tl({ zh: "复核结论必须指向 ", en: "Link the reviewed conclusion to " })}<code>docs/research</code>{tl({ zh: " 下的相对 .md 文件与 checksum （如 ", en: " using a relative .md path and checksum (for example, " })}<code>ROADMAP.md</code>、<code>rounds/2026-11-09-x.md</code>{tl({ zh: "），证据等级记为 reviewed_conclusion，区别于人工记录与 agent 解释。", en: "). The evidence grade is reviewed_conclusion." })}
                        </p>
                        <Field
                          label={tl({
                            zh: "正式文档路径（相对 docs/research 的 .md）",
                            en: "Canonical document path (.md relative to docs/research)",
                          })}
                          required
                          value={round.document_ref?.ref_id ?? ""}
                          onChange={(v) =>
                            setRoundValue("document_ref", {
                              kind: "document",
                              ref_id: v,
                              checksum: round.document_ref?.checksum ?? "",
                            })
                          }
                        />
                        <Field
                          label={tl({ zh: "文档 checksum（sha256）", en: "Document checksum (SHA-256)" })}
                          required
                          value={round.document_ref?.checksum ?? ""}
                          onChange={(v) =>
                            setRoundValue("document_ref", {
                              kind: "document",
                              ref_id: round.document_ref?.ref_id ?? "",
                              checksum: v,
                            })
                          }
                        />
                      </div>
                    )}
                    <Field
                      label={tl({ zh: "本轮目标版本", en: "Round goal version" })}
                      required
                      value={round.goal_version}
                      onChange={(v) => setRoundValue("goal_version", v)}
                    />
                    <Field
                      label={tl({ zh: "本轮目标", en: "Round objective" })}
                      required
                      value={round.objective}
                      onChange={(v) => setRoundValue("objective", v)}
                    />
                    <Field
                      label={tl({ zh: "采取的动作", en: "Action taken" })}
                      required
                      multiline
                      value={round.action}
                      onChange={(v) => setRoundValue("action", v)}
                    />
                    <Field
                      label={tl({ zh: "为何这样做", en: "Rationale" })}
                      required
                      multiline
                      value={round.rationale}
                      onChange={(v) => setRoundValue("rationale", v)}
                    />
                    <div className="grid gap-4 md:grid-cols-2">
                      <Choice
                        label={tl({ zh: "解释置信度", en: "Confidence in the interpretation" })}
                        value={round.confidence}
                        values={["unknown", "low", "medium", "high"]}
                        onChange={(v) => setRoundValue("confidence", v)}
                      />
                    </div>
                    <Field
                      label={tl({ zh: "结论和替代解释", en: "Conclusion and alternative explanations" })}
                      multiline
                      value={round.conclusion}
                      onChange={(v) => setRoundValue("conclusion", v)}
                    />
                    <Field
                      label={tl({ zh: "分支尝试名称", en: "Branch attempt name" })}
                      value={round.branch}
                      onChange={(v) => setRoundValue("branch", v)}
                    />
                    <Field
                      label={tl({ zh: "纠正旧轮次ID（可选）", en: "Corrected round ID (optional)" })}
                      value={round.supersedes_id ?? ""}
                      onChange={(v) => setRoundValue("supersedes_id", v || null)}
                    />
                    <Field
                      label={tl({ zh: "本轮下一步", en: "Next step for this round" })}
                      value={round.next_step}
                      onChange={(v) => setRoundValue("next_step", v)}
                    />
                    <h4 className="font-medium">{tl({ zh: "关联精确产物", en: "Link exact artifacts" })}</h4>
                    {round.source_refs.map((r, i) => (
                      <div key={i} className="grid gap-2 sm:grid-cols-4">
                        <Choice
                          label={`${tl({ zh: "产物类型", en: "Artifact type " })}${i + 1}`}
                          value={r.kind}
                          values={[...EVIDENCE_KINDS]}
                          onChange={(v) =>
                            setRoundValue(
                              "source_refs",
                              round.source_refs.map((x, j) => (j === i ? { ...x, kind: v } : x)),
                            )
                          }
                        />
                        <Field
                          label={`${tl({ zh: "产物ID", en: "Artifact ID " })}${i + 1}`}
                          required
                          value={r.ref_id}
                          onChange={(v) =>
                            setRoundValue(
                              "source_refs",
                              round.source_refs.map((x, j) => (j === i ? { ...x, ref_id: v } : x)),
                            )
                          }
                        />
                        <Field
                          label={`${tl({ zh: "版本", en: "Version " })}${i + 1}`}
                          value={r.version ?? ""}
                          onChange={(v) =>
                            setRoundValue(
                              "source_refs",
                              round.source_refs.map((x, j) =>
                                j === i ? { ...x, version: v || undefined } : x,
                              ),
                            )
                          }
                        />
                        <Button
                          type="button"
                          variant="outline"
                          onClick={() =>
                            setRoundValue(
                              "source_refs",
                              round.source_refs.filter((_, j) => j !== i),
                            )
                          }
                        >
                          {tl({ zh: "移除引用", en: "Remove reference" })}
                        </Button>
                      </div>
                    ))}
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() =>
                        setRoundValue("source_refs", [
                          ...round.source_refs,
                          { kind: "research_run", ref_id: "" },
                        ])
                      }
                    >
                      {tl({ zh: "添加产物引用", en: "Add artifact reference" })}
                    </Button>
                    {append.isError && (
                      <p role="alert" className="text-destructive">
                        {String(append.error)}
                      </p>
                    )}
                    <div className="flex gap-2">
                      <Button type="submit" disabled={append.isPending}>
                        {tl({ zh: "保存轮次", en: "Save round" })}
                      </Button>
                      <Button type="button" variant="outline" onClick={() => setRound(null)}>
                        {tl({ zh: "取消", en: "Cancel" })}
                      </Button>
                    </div>
                  </form>
                )}
                <section>
                  <div className="mb-3 flex flex-wrap items-baseline gap-x-4 gap-y-1">
                    <h3 className="font-semibold">{tl({ zh: "证据与轮次时间线", en: "Evidence and round timeline" })}</h3>
                    {topicId && (
                      <>
                        <Link className="text-sm text-primary underline" to={`/jobs?topic=${encodeURIComponent(topicId)}`}>{tl({ zh: "查看课题任务", en: "View topic jobs" })}</Link>
                        <Link className="text-sm text-primary underline" to={`/research/audit?topic=${encodeURIComponent(topicId)}`}>{tl({ zh: "查看课题审计", en: "View topic audit" })}</Link>
                      </>
                    )}
                  </div>
                  {entries.isLoading && <LoadingState />}
                  {entries.isError && (
                    <ErrorState message={String(entries.error)} onRetry={() => entries.refetch()} />
                  )}
                  {entries.data?.items.map((e) => (
                    <article key={e.entry_id} className="space-y-3 border-t border-border py-4">
                      <p className="text-xs text-muted-foreground">
                        <ResearchTimestamp value={e.created_at} /> · {e.created_by} · {e.entry_id}
                        {topicId && (
                          <Link className="ms-2 text-primary underline" to={`/research/audit?topic=${encodeURIComponent(topicId)}&entry=${encodeURIComponent(e.entry_id)}`}>
                            {tl({ zh: "操作回执", en: "Operation receipt" })}
                          </Link>
                        )}
                      </p>
                      {e.entry_type === "lifecycle" ? (
                        <>
                          <h4 className="font-medium">
                            {tl({ zh: "生命周期：", en: "Lifecycle: " })}{STATES[e.from ?? ""]?.[lang] ?? e.from} →{" "}
                            {STATES[e.to ?? ""]?.[lang] ?? e.to}
                          </h4>
                          <p className="text-sm">{tl({ zh: "原因：", en: "Reason: " })}{e.reason || tl({ zh: "未填写", en: "Not provided" })}</p>
                          <p className="text-xs text-muted-foreground">
                            {tl({ zh: "操作者 ", en: "Actor " })}{e.actor}{tl({ zh: " · 动作 ", en: " · Action " })}{e.action}
                          </p>
                        </>
                      ) : e.topic ? (
                        <>
                          <h4 className="font-medium">
                            {e.topic.goal
                              ? `${tl({ zh: "目标记录 r", en: "Goal record r" })}${e.topic_revision} · ${e.topic.goal.version}`
                              : `${tl({ zh: "草稿记录 r", en: "Draft record r" })}${e.topic_revision}`}
                          </h4>
                          {e.topic.goal ? (
                            <>
                              <p className="text-sm">{e.topic.goal.criteria}</p>
                              <EvidenceLinks refs={[e.topic.goal.source]} />
                            </>
                          ) : (
                            <p className="text-sm text-muted-foreground">
                              {tl({
                                zh: "草稿只保存问题，目标版本与来源在激活时补齐。",
                                en: "A draft stores the question. Add its goal version and source during activation.",
                              })}
                            </p>
                          )}
                          <p className="text-xs text-warning">
                            {tl({
                              zh: "与当前目标不同的版本分别保留，不自动替换历史验收门。",
                              en: "This historical version retains its own acceptance criteria.",
                            })}
                          </p>
                        </>
                      ) : (
                        <>
                          <h4 className="font-medium">
                            {e.objective} · {STATES[e.outcome ?? ""]?.[lang] ?? e.outcome}
                          </h4>
                          <p className="text-xs text-muted-foreground">
                            {e.evidence_level === "agent_interpretation"
                              ? tl({ zh: "agent 解释", en: "Agent interpretation" })
                              : tl({ zh: "人工记录", en: "Human record" })}{" "}
                            {tl({ zh: "· 置信度 ", en: "· Confidence " })}{STATES[e.confidence ?? ""]?.[lang] ?? e.confidence}{tl({ zh: " · 目标", en: " · Goal" })}{" "}
                            {e.goal_version}{tl({ zh: " · 分支 ", en: " · Branch " })}{e.branch}
                          </p>
                          <p className="whitespace-pre-wrap text-sm">{tl({ zh: "动作：", en: "Action: " })}{e.action}</p>
                          <p className="whitespace-pre-wrap text-sm">{tl({ zh: "理由：", en: "Rationale: " })}{e.rationale}</p>
                          <EvidenceLinks refs={e.source_refs ?? []} />
                          <p className="whitespace-pre-wrap text-sm">
                            {tl({ zh: "工作结论：", en: "Working conclusion: " })}{e.conclusion || tl({ zh: "尚未形成", en: "Pending" })}
                          </p>
                          {e.supersedes_id && (
                            <p className="text-sm text-warning">
                              {tl({ zh: "纠正链：本轮纠正 ", en: "This round corrects " })}{e.supersedes_id}{tl({ zh: "，旧记录仍保留", en: ". The previous record remains available." })}
                            </p>
                          )}
                          <p className="text-sm">{tl({ zh: "下一步：", en: "Next step: " })}{e.next_step}</p>
                        </>
                      )}
                    </article>
                  ))}
                  <Pager
                    offset={entryOffset}
                    more={entries.data?.has_more}
                    onChange={setEntryOffset}
                  />
                </section>
              </>
            )}
          </main>
        </div>
      )}
      {tab === "memories" && (
        <section aria-label={tl({ zh: "研究记忆阅读区", en: "Research memory reader" })} className="w-full min-w-0 max-w-5xl space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3 text-sm text-muted-foreground">
            <p>{tl({ zh: "这里是跨课题的记忆库。课题通过引用组织相关记忆，原记录保留。", en: "Browse memories across topics. Topics link to the original memory records." })}</p>
            <Link className="shrink-0 text-primary underline underline-offset-4" to="/research/docs?doc=topic-organization.md">{tl({ zh: "课题组织说明", en: "Topic organization guide" })}</Link>
          </div>
          {memoryId && (
            <section aria-label={tl({ zh: "选定记忆详情", en: "Selected memory" })} className="min-w-0">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <h2 className="text-base font-semibold">{tl({ zh: "完整记忆", en: "Full memory" })}</h2>
                <Button variant="outline" onClick={() => setParams({ tab: "memories" })}>
                  {tl({ zh: "返回记忆列表", en: "Back to memory list" })}
                </Button>
              </div>
              {memory.isLoading && <LoadingState />}
              {memory.isError && (
                <ErrorState message={String(memory.error)} onRetry={() => memory.refetch()} />
              )}
              {memory.data && (
                <ResearchMemoryArticle
                  memory={memory.data}
                  content={memory.data.content}
                  detail
                  evidence={
                    <>
                      <EvidenceLinks refs={memory.data.source_refs} />
                      {(!memory.data.source_refs.length ||
                        memory.data.source_refs.some(
                          (ref) => !ref.ref_id || ref.kind === "unknown",
                        )) && (
                        <p className="text-sm text-warning">
                          {tl({
                            zh: "旧引用为空或含unknown，不能验证此处解释。",
                            en: "The reference is empty or marked unknown, so this interpretation cannot be verified.",
                          })}
                        </p>
                      )}
                    </>
                  }
                  actions={
                    memory.data.supersedes_id && (
                      <Link
                        className="break-all text-sm text-primary underline"
                        to={`/research/topics?memory=${encodeURIComponent(memory.data.supersedes_id)}`}
                      >
                        {tl({ zh: "查看被纠正的记忆：", en: "View corrected memory: " })}{memory.data.supersedes_id}
                      </Link>
                    )
                  }
                />
              )}
            </section>
          )}
          {!memoryId && memories.isLoading && <LoadingState />}
          {!memoryId && memories.isError && (
            <ErrorState message={String(memories.error)} onRetry={() => memories.refetch()} />
          )}
          {!memoryId && memories.data?.items.length === 0 && (
            <EmptyState
              title={tl({ zh: "暂无研究记忆", en: "No research memories yet" })}
              description={tl({
                zh: "agent 通过研究记忆工具保存跨会话笔记，在此核查来源。",
                en: "Agents save notes across sessions using research memory tools. Open a memory here to verify its source.",
              })}
            />
          )}
          {!memoryId &&
            memories.data?.items.map((m) => (
              <ResearchMemoryArticle
                key={m.memory_id}
                memory={m}
                content={m.excerpt}
                excerpted={m.content_length > 1200}
                evidence={
                  <>
                    <MemoryEvidence refs={m.source_refs} />
                    {m.unverifiable_refs && (
                      <p className="text-sm text-warning">
                        {tl({
                          zh: "旧引用为空、含unknown或超过摘要上限，不能验证此处解释。",
                          en: "The reference is empty, marked unknown, or exceeds the summary limit, so this interpretation cannot be verified.",
                        })}
                      </p>
                    )}
                    {m.supersedes_id && (
                      <Link
                        className="break-all text-sm text-primary underline"
                        to={`/research/topics?memory=${encodeURIComponent(m.supersedes_id)}`}
                      >
                        {tl({ zh: "查看被纠正的记忆：", en: "View corrected memory: " })}{m.supersedes_id}
                      </Link>
                    )}
                  </>
                }
                actions={
                  <Button variant="outline" onClick={() => setParams({ memory: m.memory_id })}>
                    {tl({ zh: "查看完整记忆与来源", en: "View full memory and sources" })}
                  </Button>
                }
              />
            ))}
          {!memoryId && (
            <Pager
              offset={memoryOffset}
              more={memories.data?.has_more}
              onChange={setMemoryOffset}
            />
          )}
        </section>
      )}
    </div>
  );
}
function Pager({
  offset,
  more,
  onChange,
}: {
  offset: number;
  more?: boolean;
  onChange: (v: number) => void;
}) {
  const { tl } = useT();
  return (
    <div className="flex items-center gap-3">
      <Button
        variant="outline"
        disabled={offset === 0}
        onClick={() => onChange(Math.max(0, offset - 20))}
      >
        {tl({ zh: "上一页", en: "Previous page" })}
      </Button>
      <span className="text-xs text-muted-foreground">{tl({ zh: `第 ${offset / 20 + 1} 页`, en: `Page ${offset / 20 + 1}` })}</span>
      <Button variant="outline" disabled={!more} onClick={() => onChange(offset + 20)}>
        {tl({ zh: "下一页", en: "Next page" })}
      </Button>
    </div>
  );
}

