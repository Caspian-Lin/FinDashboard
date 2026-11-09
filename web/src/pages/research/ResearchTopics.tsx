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
import { ResearchTimestamp } from "@/components/research/ResearchTimestamp";
import { TopicDataCard } from "@/components/data/TopicDataCard";
import {
  workspaceApi,
  topicRunApi,
  evidenceUrl,
  conflictDetail,
  type ActivateInput,
  type EvidenceRef,
  type Topic,
  type TopicConflictDetail,
  type TopicDraftInput,
  type TopicDraftUpdate,
  type TopicInput,
  type TopicRunLink,
  type TopicRunStartInput,
  type RoundInput,
  type MemoryDetail,
} from "@/lib/research-workspace";
import {
  factorLabApi,
  featureSnapshotNames,
  featureSnapshotSymbolCount,
  strategySpecApi,
  type ResearchStrategySpec,
} from "@/lib/research";
import { fetchJSON } from "@/lib/api";

const EMPTY_DRAFT: TopicDraftInput = { title: "", question: "" };
const EMPTY_GOAL = { version: "", criteria: "", source: { kind: "document", ref_id: "" } };
const STATES: Record<string, string> = {
  low: "低",
  medium: "中",
  high: "高",
  research_run: "研究回测",
  strategy: "策略版本",
  dataset: "数据发布",
  experiment: "验证实验",
  memory: "研究记忆",
  document: "正式文档",
  factor_series: "因子序列",
  simulation: "模拟盘",
  backtest: "探索回测",
  draft: "草稿",
  active: "研究中",
  paused: "已暂停",
  closed: "已收束",
  unknown: "结论未定",
  supported: "工作结论：获支持",
  not_supported: "工作结论：未获支持",
  insufficient_evidence: "工作结论：证据不足",
  completed: "已完成",
  failed: "失败",
  interrupted: "中断",
  rejected: "被拒绝",
  forgotten: "已纠正/移除",
  archived: "归档",
  matched: "引用存在",
  missing: "断链",
  checksum_mismatch: "checksum不一致",
  version_mismatch: "版本不一致",
  version_required: "需补精确版本",
  invalid: "引用非法",
};
const ACTION_LABELS: Record<string, string> = {
  pause: "暂停课题",
  resume: "继续课题",
  close: "收束课题",
  reopen: "重开课题",
  discard: "放弃草稿",
};
const ACTION_HINTS: Record<string, string> = {
  pause: "暂停保留目标、轮次与引用；可随时继续，不产生新研究。",
  resume: "继续后课题恢复为研究中；不会自动发起任何运行。",
  close: "收束表示本轮研究告一段落，原因会写入生命周期记录。",
  reopen: "重开需要已归档目标版本；历史轮次与结论原样保留。",
  discard: "放弃草稿会收束课题并保留记录，不会删除任何证据。",
};
const REASON_REQUIRED = new Set(["close", "reopen", "discard"]);
const NEXT_ACTION_LABELS: Record<string, string> = {
  activate: "补齐目标并激活",
  check_refs: "核验断链引用",
  resume: "恢复课题",
  reopen: "重开课题",
  write_round: "记录轮次",
  review_open_questions: "复核开放问题",
  review: "复核证据",
};

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
  return (
    <ul className="space-y-2">
      {refs.map((r, i) => {
        const url = evidenceUrl(r);
        return (
          <li key={i} className="break-all text-sm">
            {url ? (
              <Link className="text-primary underline" to={url}>
                {r.kind}：{r.ref_id}
                {r.version ? ` @${r.version}` : ""}
              </Link>
            ) : (
              <span className="text-warning">
                无法验证的来源：{r.kind || "unknown"} / {r.ref_id || "空引用"}
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
  const [open, setOpen] = useState(false);
  return (
    <details className="min-w-0 text-sm" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="cursor-pointer text-muted-foreground">关联证据（{refs.length}）</summary>
      {open && (
        <div className="mt-3">
          <EvidenceLinks refs={refs} />
        </div>
      )}
    </details>
  );
}
function SourceFact({ reference }: { reference: EvidenceRef }) {
  const q = useQuery({
    queryKey: ["source-fact", reference],
    queryFn: () =>
      fetchJSON<{ status: string; facts?: Record<string, unknown> }>(
        `/research/topics/source?${new URLSearchParams({ kind: reference.kind, ref_id: reference.ref_id, ...(reference.version ? { version: reference.version } : {}), ...(reference.checksum ? { checksum: reference.checksum } : {}) })}`,
      ),
    enabled: !!reference.ref_id && reference.kind !== "unknown",
  });
  if (q.isError) return <p className="text-xs text-warning">来源核验暂不可用</p>;
  if (!q.data) return null;
  return (
    <div className="mt-1 text-xs text-muted-foreground">
      <p>
        自动来源核验：
        {q.data.status === "matched"
          ? "引用存在"
          : q.data.status === "missing"
            ? "断链，产物不存在"
            : q.data.status === "checksum_mismatch"
              ? "checksum不一致"
              : q.data.status === "version_required"
                ? "需补精确版本"
                : q.data.status}
      </p>
      {q.data.facts && (
        <details>
          <summary className="cursor-pointer">查看产物事实（独立于轮次解释）</summary>
          <EvidenceFields value={q.data.facts} />
        </details>
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
            {STATES[v] ?? v}
          </option>
        ))}
      </select>
    </div>
  );
}

function ContextCard({ topicId }: { topicId: string }) {
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
      aria-label="继续研究上下文"
      className="space-y-3 rounded-lg border border-border p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">继续研究（跨客户端续接）</h3>
        <span className="text-xs text-muted-foreground">
          生成于 <ResearchTimestamp value={context.generated_at} /> · revision {context.revision}
          {context.excerpted ? " · 已按边界截取" : ""}
        </span>
      </div>
      {context.excerpted && (
        <p className="text-xs text-muted-foreground">
          上下文按服务端边界截取（最近轮次 ≤{context.bounds.max_rounds}、引用核验 ≤
          {context.bounds.max_refs}、摘录 ≤{context.bounds.excerpt_chars} 字、载荷 ≤64 KiB）；
          精确全文请用轮次 ID 或课题详情查看。
          {context.truncated_sections?.length
            ? `已压缩段落：${context.truncated_sections.join("、")}`
            : ""}
        </p>
      )}
      {context.lifecycle.last && (
        <p className="text-sm text-muted-foreground">
          最近生命周期：
          {STATES[context.lifecycle.last.from ?? ""] ?? context.lifecycle.last.from} →{" "}
          {STATES[context.lifecycle.last.to ?? ""] ?? context.lifecycle.last.to}
          {context.lifecycle.last.reason ? `（${context.lifecycle.last.reason}）` : ""}
          {context.lifecycle.last.actor ? ` · ${context.lifecycle.last.actor}` : ""}
        </p>
      )}
      {context.goal ? (
        <p className="text-sm">
          当前目标 {context.goal.version}：{context.goal.criteria}
        </p>
      ) : (
        <p className="text-sm text-warning">尚未补齐目标版本与来源，激活后才能发起研究。</p>
      )}
      <div>
        <h4 className="text-sm font-medium">建议下一步（服务端派生，不是执行）</h4>
        <ul className="list-disc space-y-1 pl-5 text-sm">
          {context.next_actions.map((action) => (
            <li key={action.action}>
              {NEXT_ACTION_LABELS[action.action] ?? action.action}：{action.why}
            </li>
          ))}
        </ul>
      </div>
      {context.dangling_refs.length > 0 && (
        <div role="alert" className="rounded-md border border-destructive/40 p-3 text-sm">
          <p className="font-medium text-destructive">
            断链引用 {context.dangling_refs.length} 条，需重新核验（不是自动接受或自动修复）：
          </p>
          <ul className="mt-1 space-y-1">
            {context.dangling_refs.map((item, index) => (
              <li key={`${item.ref.kind}-${item.ref.ref_id}-${index}`} className="break-all">
                {item.ref.kind}：{item.ref.ref_id} · {STATES[item.status] ?? item.status}
              </li>
            ))}
          </ul>
        </div>
      )}
      {context.active_links.length > 0 && (
        <div>
          <h4 className="text-sm font-medium">进行中的关联</h4>
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {context.active_links.map((link) => (
              <li key={`${link.entity_type}-${link.entity_id}`} className="break-all">
                {link.entity_type}：{link.entity_id}
                {link.job_id ? ` · ${link.job_id}` : ""}
                {link.goal_version ? ` · 目标 ${link.goal_version}` : ""} · {link.state}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div>
        <h4 className="text-sm font-medium">最近轮次（{context.recent_rounds.length}）</h4>
        {context.recent_rounds.length === 0 ? (
          <p className="text-sm text-muted-foreground">尚无轮次记录；下一步动作还没有留痕。</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {context.recent_rounds.map((round) => (
              <li key={round.entry_id} className="border-t border-border pt-2">
                <p className="text-xs text-muted-foreground">
                  <ResearchTimestamp value={round.created_at} /> · {round.branch} ·{" "}
                  {STATES[round.outcome ?? ""] ?? round.outcome ?? "未记录结果"} ·{" "}
                  {round.evidence_level === "agent_interpretation" ? "agent 解释" : "人工记录"} ·{" "}
                  {round.entry_id}
                </p>
                <p className="whitespace-pre-wrap">
                  {round.conclusion_excerpt || "本轮尚无工作结论"}
                </p>
                {round.next_step && <p className="text-muted-foreground">下一步：{round.next_step}</p>}
              </li>
            ))}
          </ul>
        )}
      </div>
      {context.open_questions.length > 0 && (
        <div>
          <h4 className="text-sm font-medium">开放问题</h4>
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

const TOPIC_RUN_STATES: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};
const TERMINAL_TOPIC_RUN_STATES = new Set(["succeeded", "failed", "cancelled"]);

function manifestRecord(
  manifest: Record<string, unknown> | undefined,
  key: string,
): Record<string, unknown> | undefined {
  const value = manifest?.[key];
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function specReleaseIds(strategy: ResearchStrategySpec | undefined): string[] {
  const plan = manifestRecord(strategy?.spec, "validation_plan");
  const ids = plan?.dataset_release_ids;
  return Array.isArray(ids) ? ids.filter((item): item is string => typeof item === "string") : [];
}

function specNeedsFactorSnapshot(strategy: ResearchStrategySpec | undefined): boolean {
  const graph = manifestRecord(strategy?.spec, "feature_graph");
  const nodes = graph?.nodes;
  if (!Array.isArray(nodes)) return false;
  return nodes.some((node) => {
    if (!node || typeof node !== "object") return false;
    const item = node as Record<string, unknown>;
    return item.kind === "factor" || item.kind === "risk_factor";
  });
}

function newOperationId(prefix: string): string {
  const token =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${token}`.slice(0, 64);
}

function TopicRunsCard({ topic }: { topic: Topic }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [strategyKey, setStrategyKey] = useState("");
  const [snapshotIds, setSnapshotIds] = useState<string[]>([]);
  const [seriesIds, setSeriesIds] = useState("");
  const [initialCapital, setInitialCapital] = useState("100000");
  const [codeVersion, setCodeVersion] = useState("web-ui-v1");
  const [requestedBy, setRequestedBy] = useState("user:api");
  const [operationId, setOperationId] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [notice, setNotice] = useState("");
  const runsQuery = useQuery({
    queryKey: ["topic-runs", topic.topic_id],
    queryFn: () => topicRunApi.list(topic.topic_id),
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? [];
      return items.some((item) => !TERMINAL_TOPIC_RUN_STATES.has(item.state)) ? 5_000 : false;
    },
  });
  const strategiesQuery = useQuery({
    queryKey: ["spec-list", "topic-run"],
    queryFn: () => strategySpecApi.list(100),
    enabled: open,
  });
  const selectedStrategy = strategiesQuery.data?.find(
    (item) => `${item.strategy_id}@${item.version}` === strategyKey,
  );
  const releaseIds = specReleaseIds(selectedStrategy);
  const needsSnapshot = specNeedsFactorSnapshot(selectedStrategy);
  const snapshotsQuery = useQuery({
    queryKey: ["feature-snapshots", "topic-run", releaseIds.join(",")],
    queryFn: async () => {
      const perRelease = await Promise.all(
        releaseIds.map((id) => factorLabApi.features(id, 100)),
      );
      const seen = new Set<string>();
      return perRelease.flat().filter((snapshot) => {
        if (seen.has(snapshot.snapshot_id)) return false;
        seen.add(snapshot.snapshot_id);
        return true;
      });
    },
    enabled: open && needsSnapshot && releaseIds.length > 0,
  });

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["topic-runs", topic.topic_id] });
    void qc.invalidateQueries({ queryKey: ["topic-context", topic.topic_id] });
  };
  const start = useMutation({
    mutationFn: (body: TopicRunStartInput) => topicRunApi.start(topic.topic_id, body),
    onSuccess: (receipt) => {
      setOpen(false);
      setNotice(
        receipt.created
          ? `已登记 ${receipt.run_id}（${receipt.job_id ?? "无任务"}），等待 worker 执行。`
          : `幂等命中：返回原运行 ${receipt.run_id}，未重复登记。`,
      );
      refresh();
    },
  });
  const retry = useMutation({
    mutationFn: (run: TopicRunLink) =>
      topicRunApi.retry(topic.topic_id, run.run_id, {
        idempotency_key: newOperationId("topic-retry"),
        requested_by: requestedBy.trim() || "user:api",
        operation_id: newOperationId("OP-topic-retry"),
      }),
    onSuccess: (receipt) => {
      setNotice(`已登记重放运行 ${receipt.run_id}，原运行链接保留。`);
      refresh();
    },
  });
  const cancel = useMutation({
    mutationFn: (run: TopicRunLink) => topicRunApi.cancel(topic.topic_id, run.run_id),
    onSuccess: () => {
      setNotice("已请求取消；worker 在下一个 checkpoint 收口。");
      refresh();
    },
  });

  const openForm = () => {
    setOpen(true);
    setStrategyKey("");
    setSnapshotIds([]);
    setSeriesIds("");
    setInitialCapital("100000");
    setCodeVersion("web-ui-v1");
    setRequestedBy("user:api");
    setOperationId(newOperationId("OP-topic-run"));
    setIdempotencyKey(newOperationId("topic-run"));
    setNotice("");
  };
  const capital = Number(initialCapital);
  const validForm =
    !!selectedStrategy &&
    !selectedStrategy.status?.startsWith("draft") &&
    releaseIds.length > 0 &&
    (!needsSnapshot || snapshotIds.length > 0) &&
    Number.isFinite(capital) &&
    capital >= 100000 &&
    capital <= 500000 &&
    requestedBy.trim() !== "" &&
    codeVersion.trim().length >= 7 &&
    operationId.trim() !== "" &&
    idempotencyKey.trim().length >= 8;

  const submit = () => {
    if (!selectedStrategy || !validForm || !topic.goal) return;
    start.mutate({
      idempotency_key: idempotencyKey.trim(),
      strategy_id: selectedStrategy.strategy_id,
      strategy_version: selectedStrategy.version,
      dataset_release_ids: releaseIds,
      factor_snapshot_ids: snapshotIds,
      factor_series_ids: seriesIds
        .split(",")
        .map((id) => id.trim())
        .filter(Boolean),
      parameters: {},
      validation_config: {},
      portfolio_config: {},
      risk_config: {},
      execution_config: {},
      fee_config: {},
      benchmark_config: {},
      code_version: codeVersion.trim(),
      initial_capital: capital,
      requested_by: requestedBy.trim(),
      goal_version: topic.goal.version,
      operation_id: operationId.trim(),
    });
  };

  const mutationError = start.error ?? retry.error ?? cancel.error;
  const runItems = runsQuery.data?.items ?? [];
  return (
    <section aria-label="课题研究运行" className="space-y-3 rounded-lg border border-border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">研究运行（运行事实，独立于轮次解释）</h3>
        <Button onClick={openForm} disabled={topic.status !== "active" || !topic.goal}>
          发起研究运行
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">
        发起是独立于保存的显式动作：只冻结已发布策略、数据发布与快照并登记 queued 任务，
        页面不会在请求内执行回测。运行状态、进度与错误由服务端 join 派生，不会回写课题轮次。
      </p>
      {topic.status !== "active" && (
        <p className="text-xs text-warning">只有 active 课题可以发起或重试运行。</p>
      )}
      {notice && <p className="text-sm text-primary">{notice}</p>}
      {mutationError && (
        <p role="alert" className="text-sm text-destructive">
          {String(mutationError)}
        </p>
      )}
      {open && topic.goal && (
        <form
          className="space-y-4 rounded-lg border border-border p-4"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <h4 className="font-medium">冻结输入并发起运行（目标版本 {topic.goal.version}）</h4>
          <div className="space-y-1">
            <Label htmlFor="topic-run-strategy">已发布策略规格</Label>
            <select
              id="topic-run-strategy"
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              value={strategyKey}
              onChange={(event) => {
                setStrategyKey(event.target.value);
                setSnapshotIds([]);
              }}
            >
              <option value="">选择已发布策略</option>
              {(strategiesQuery.data ?? [])
                .filter((item) => !item.status?.startsWith("draft"))
                .map((item) => (
                  <option key={`${item.strategy_id}@${item.version}`} value={`${item.strategy_id}@${item.version}`}>
                    {item.strategy_id}@v{item.version}
                  </option>
                ))}
            </select>
          </div>
          <div className="rounded-md border border-border bg-muted/20 p-3 text-xs">
            <p className="font-medium">冻结数据发布（来自策略验证计划）</p>
            {releaseIds.length > 0 ? (
              <p className="mt-1 break-all font-mono">{releaseIds.join("、")}</p>
            ) : (
              <p className="mt-1 text-muted-foreground">选择策略后显示。</p>
            )}
          </div>
          {needsSnapshot && (
            <div className="space-y-2 rounded-md border border-warning/30 bg-warning/5 p-3">
              <Label>冻结因子快照（至少 1 个）</Label>
              {snapshotsQuery.isLoading ? (
                <p className="text-xs text-muted-foreground">正在加载与发布匹配的快照…</p>
              ) : snapshotsQuery.data && snapshotsQuery.data.length > 0 ? (
                snapshotsQuery.data.map((snapshot) => {
                  const checked = snapshotIds.includes(snapshot.snapshot_id);
                  return (
                    <label key={snapshot.snapshot_id} className="flex items-center gap-2 text-xs">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() =>
                          setSnapshotIds((current) =>
                            checked
                              ? current.filter((id) => id !== snapshot.snapshot_id)
                              : [...current, snapshot.snapshot_id],
                          )
                        }
                      />
                      <span className="font-mono">{snapshot.snapshot_id}</span>
                      <span className="text-muted-foreground">
                        {featureSnapshotSymbolCount(snapshot)} 标的 ·{" "}
                        {featureSnapshotNames(snapshot).length} 因子
                      </span>
                    </label>
                  );
                })
              ) : (
                <p className="text-xs text-warning">暂无匹配快照；请先在因子实验室生成。</p>
              )}
            </div>
          )}
          <Field
            label="因子序列 ID（可选，逗号分隔 FS-）"
            value={seriesIds}
            onChange={setSeriesIds}
          />
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="初始资金" value={initialCapital} onChange={setInitialCapital} required />
            <Field label="代码版本" value={codeVersion} onChange={setCodeVersion} required />
            <Field label="发起人" value={requestedBy} onChange={setRequestedBy} required />
            <Field label="operation_id（幂等锚）" value={operationId} onChange={setOperationId} required />
          </div>
          <Field
            label="运行幂等键"
            value={idempotencyKey}
            onChange={setIdempotencyKey}
            required
          />
          {start.isError && (
            <p role="alert" className="text-sm text-destructive">
              {String(start.error)}
            </p>
          )}
          <div className="flex gap-2">
            <Button type="submit" disabled={!validForm || start.isPending}>
              登记 queued 运行
            </Button>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>
              取消
            </Button>
          </div>
        </form>
      )}
      {runsQuery.isLoading ? (
        <LoadingState />
      ) : runsQuery.isError ? (
        <ErrorState message={String(runsQuery.error)} onRetry={() => runsQuery.refetch()} />
      ) : runItems.length === 0 ? (
        <p className="text-sm text-muted-foreground">尚未发起运行；轮次记录不能替代运行事实。</p>
      ) : (
        <ul className="space-y-3">
          {runItems.map((run) => (
            <li key={run.link_id} className="rounded-md border border-border p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <Link className="font-mono text-primary underline" to={run.deep_link}>
                  {run.run_id}
                </Link>
                <span className="rounded bg-muted px-2 py-0.5 text-xs">
                  {TOPIC_RUN_STATES[run.state] ?? run.state}
                </span>
                {run.job_id && <span className="font-mono text-xs text-muted-foreground">{run.job_id}</span>}
                <span className="text-xs text-muted-foreground">目标 {run.goal_version}</span>
              </div>
              {run.progress_total ? (
                <p className="mt-1 text-xs text-muted-foreground">
                  进度 {run.progress_done ?? 0}/{run.progress_total}
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
                    重放运行
                  </Button>
                )}
                {!TERMINAL_TOPIC_RUN_STATES.has(run.state) && (
                  <Button size="sm" variant="outline" onClick={() => cancel.mutate(run)} disabled={cancel.isPending}>
                    取消
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
  return (
    <section
      role="dialog"
      aria-label="课题冲突"
      className="space-y-3 rounded-lg border border-destructive/50 p-4"
    >
      <h2 className="font-semibold">课题已被其他会话更新，本次修改未保存</h2>
      {conflict.code === "illegal_transition" ? (
        <>
          <p className="text-sm">
            当前状态 {STATES[conflict.current ?? ""] ?? conflict.current} 不允许该操作；允许的目标状态：
            {(conflict.allowed ?? []).map((s) => STATES[s] ?? s).join(" / ") || "无"}
          </p>
          {conflict.actions?.length ? (
            <p className="text-sm text-muted-foreground">
              可用动作：{conflict.actions.map((a) => ACTION_LABELS[a] ?? a).join(" / ")}（走
              POST /transitions，PUT 不能改变状态）
            </p>
          ) : null}
        </>
      ) : conflict.code === "idempotency_conflict" ? (
        <p className="text-sm">幂等键 {conflict.idempotency_key} 已绑定内容不同的课题。</p>
      ) : (
        <>
          <p className="text-sm">
            当前 revision {conflict.current_revision ?? "?"}，你的 revision{" "}
            {conflict.your_revision ?? "?"}。已刷新到最新版本，你的改动没有覆盖对方内容。
          </p>
          {conflict.changed_fields?.length ? (
            <p className="text-sm">变化的字段：{conflict.changed_fields.join("、")}</p>
          ) : null}
          {conflict.current_summary && (
            <p className="text-sm text-muted-foreground">
              当前：状态 {STATES[conflict.current_summary.status ?? ""] ?? conflict.current_summary.status}
              {" · "}目标 {conflict.current_summary.goal_version ?? "未补齐"}
              {" · "}r{conflict.current_summary.revision}
            </p>
          )}
        </>
      )}
      <div className="flex flex-wrap gap-2">
        <Button onClick={onReload}>重新加载最新版本</Button>
        {canKeepNote && (
          <Button variant="outline" onClick={onKeepNote}>
            仅保留我的说明（追加轮次）
          </Button>
        )}
        <Button variant="outline" onClick={onClose}>
          关闭
        </Button>
      </div>
    </section>
  );
}

export default function ResearchTopics() {
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
  const startRound = (conclusion = "") => {
    setKey(crypto.randomUUID());
    setRound({
      entry_type: "round",
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
        title="研究课题"
        description="先保存问题，再补齐目标与来源后激活；围绕问题组织每轮尝试、证据和下一步，随时暂停与续接。"
        actions={<Button onClick={openDraftForm}>新建课题</Button>}
      />
      <div className="flex flex-wrap gap-2">
        <Button variant={tab === "topics" ? "default" : "outline"} onClick={() => setTab("topics")}>
          课题与轮次
        </Button>
        <Button
          variant={tab === "memories" ? "default" : "outline"}
          onClick={() => setTab("memories")}
        >
          研究记忆
        </Button>
        <Button variant="outline" asChild>
          <Link to="/research/docs">正式研究资料</Link>
        </Button>
        <Button variant="outline" asChild>
          <Link to="/research/workbench">继续与 agent 研究</Link>
        </Button>
      </div>
      {params.get("source_kind") && params.get("source_id") && (
        <section className="rounded-md border border-border p-4">
          <h2 className="font-semibold">产物来源：{params.get("source_id")}</h2>
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
            打开对应研究工具
          </Link>
        </section>
      )}
      <p className="max-w-prose text-sm text-muted-foreground">
        自动产物事实、agent
        解释与工作结论各有来源。正式接受的结论以仓库研究文档为准，冲突时保留双方记录；旧记忆中的故障和假设需重新核验。保存课题与执行研究严格分开，保存不会启动任何运行。
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
          <h2 className="text-lg font-semibold">第一步：保存研究问题草稿</h2>
          <p className="text-sm text-muted-foreground">
            草稿只保存问题本身并生成稳定 RT-ID；目标版本、门槛与来源在第二步补齐后才会激活。保存与执行分开。
          </p>
          <Field
            label="课题标题"
            required
            value={draftForm.title}
            onChange={(v) => setDraftForm({ ...draftForm, title: v })}
          />
          <Field
            label="研究问题"
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
              保存草稿
            </Button>
            <Button type="button" variant="outline" onClick={() => setDraftForm(null)}>
              取消
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
            {topicId ? "更新课题与目标版本" : "建立研究课题"}
          </h2>
          <Field
            label="课题标题"
            required
            value={form.title}
            onChange={(v) => setValue("title", v)}
          />
          <Field
            label="研究问题"
            required
            multiline
            value={form.question}
            onChange={(v) => setValue("question", v)}
          />
          <div className="grid gap-4 md:grid-cols-2">
            <Field
              label="目标版本"
              required
              value={form.goal.version}
              onChange={(v) => setValue("goal", { ...form.goal, version: v })}
            />
            <Field
              label="目标来源（研究文档相对路径）"
              required
              value={form.goal.source.ref_id}
              onChange={(v) =>
                setValue("goal", { ...form.goal, source: { kind: "document", ref_id: v } })
              }
            />
          </div>
          <Field
            label="目标和验收门槛"
            required
            multiline
            value={form.goal.criteria}
            onChange={(v) => setValue("goal", { ...form.goal, criteria: v })}
          />
          <div className="grid gap-4 md:grid-cols-2">
            <Choice
              label="课题状态"
              value={form.status}
              values={["active", "paused", "closed"]}
              onChange={(v) => setValue("status", v)}
            />
            <Choice
              label="工作结论（不替代正式结论）"
              value={form.conclusion}
              values={["unknown", "supported", "not_supported", "insufficient_evidence"]}
              onChange={(v) => setValue("conclusion", v)}
            />
          </div>
          <Field
            label="当前摘要"
            multiline
            value={form.summary}
            onChange={(v) => setValue("summary", v)}
          />
          <Field
            label="开放问题（每行一个）"
            multiline
            value={form.open_questions.join("\n")}
            onChange={(v) => setValue("open_questions", v.split("\n").filter(Boolean))}
          />
          <Field label="下一步" value={form.next_step} onChange={(v) => setValue("next_step", v)} />
          {save.isError && (
            <p role="alert" className="text-destructive">
              {String(save.error)}
            </p>
          )}
          <div className="flex gap-2">
            <Button type="submit" disabled={save.isPending}>
              保存研究记录
            </Button>
            <Button type="button" variant="outline" onClick={() => setForm(null)}>
              取消
            </Button>
          </div>
        </form>
      )}
      {tab === "topics" && !form && !draftForm && (
        <div className="grid gap-6 lg:grid-cols-[18rem_minmax(0,1fr)]">
          <aside className="space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Label htmlFor="topic-status-filter">状态</Label>
              <select
                id="topic-status-filter"
                aria-label="按状态筛选"
                className="h-8 rounded-md border border-input bg-background px-2 text-sm"
                value={statusFilter}
                onChange={(e) => {
                  setStatusFilter(e.target.value);
                  setOffset(0);
                }}
              >
                <option value="">全部</option>
                <option value="draft">草稿</option>
                <option value="active">研究中</option>
                <option value="paused">已暂停</option>
                <option value="closed">已收束</option>
              </select>
              <Label htmlFor="topic-sort">排序</Label>
              <select
                id="topic-sort"
                aria-label="排序方式"
                className="h-8 rounded-md border border-input bg-background px-2 text-sm"
                value={sort}
                onChange={(e) => {
                  setSort(e.target.value);
                  setOffset(0);
                }}
              >
                <option value="updated">最近更新</option>
                <option value="created">创建时间</option>
              </select>
            </div>
            {topics.isLoading ? (
              <LoadingState />
            ) : topics.isError ? (
              <ErrorState message={String(topics.error)} onRetry={() => topics.refetch()} />
            ) : topics.data?.items.length === 0 ? (
              <EmptyState
                title="还没有研究课题"
                description="先保存一个问题作为草稿，补齐目标与来源后再激活。"
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
                    {STATES[t.status] ?? t.status} · {STATES[t.conclusion] ?? t.conclusion}
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
                title="选择一个研究问题"
                description="查看它的目标版本、证据缺口、失败尝试和下一步，无需按页面顺序操作。"
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
                    {STATES[topic.data.status] ?? topic.data.status} ·{" "}
                    {STATES[topic.data.conclusion] ?? topic.data.conclusion} · revision{" "}
                    {topic.data.revision}
                    {topic.data.updated_at ? (
                      <>
                        {" · "}最近更新 <ResearchTimestamp value={topic.data.updated_at} />
                      </>
                    ) : null}
                  </p>
                  {topic.data.goal ? (
                    <>
                      <p className="text-sm">
                        当前目标 {topic.data.goal.version}：{topic.data.goal.criteria}
                      </p>
                      <EvidenceLinks refs={[topic.data.goal.source]} />
                    </>
                  ) : (
                    <p className="text-sm text-warning">
                      尚未补齐目标版本与验收门槛；补齐并激活前不能发起研究。
                    </p>
                  )}
                  <p className="max-w-prose whitespace-pre-wrap text-sm">
                    {topic.data.summary || "尚无工作摘要"}
                  </p>
                  <h3 className="font-medium">证据缺口</h3>
                  <ul className="list-disc space-y-1 pl-5 text-sm">
                    {topic.data.open_questions.map((x) => (
                      <li key={x}>{x}</li>
                    ))}
                  </ul>
                  <p className="text-sm">下一步：{topic.data.next_step || "尚未记录"}</p>
                  <div className="flex flex-wrap gap-2">
                    {topic.data.status !== "draft" && (
                      <Button onClick={() => startRound()}>记录新轮次</Button>
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
                          编辑草稿
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
                          补齐目标并激活
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
                        更新记录
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
                        {ACTION_LABELS[action]}
                      </Button>
                    ))}
                  </div>
                  <div className="flex flex-wrap gap-3 text-sm">
                    <Link className="text-primary underline" to="/research/strategy">
                      策略
                    </Link>
                    <Link className="text-primary underline" to="/research/runs">
                      研究回测
                    </Link>
                    <Link className="text-primary underline" to="/research/experiments">
                      OOS 验证
                    </Link>
                    <Link
                      className="text-primary underline"
                      to={`/data?tab=overview&topic=${encodeURIComponent(topic.data.topic_id)}`}
                    >
                      数据工具
                    </Link>
                    <Link className="text-primary underline" to="/research/factors">
                      因子工具
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
                    <h3 className="font-semibold">编辑草稿（不改变生命周期状态）</h3>
                    <p className="text-sm text-muted-foreground">
                      草稿只保存问题与工作笔记；目标版本、门槛与来源须在“补齐目标并激活”时提供，PUT 不能改变状态。
                    </p>
                    <Field
                      label="课题标题"
                      required
                      value={draftEdit.title}
                      onChange={(v) => setDraftEdit({ ...draftEdit, title: v })}
                    />
                    <Field
                      label="研究问题"
                      required
                      multiline
                      value={draftEdit.question}
                      onChange={(v) => setDraftEdit({ ...draftEdit, question: v })}
                    />
                    <Field
                      label="当前摘要"
                      multiline
                      value={draftEdit.summary ?? ""}
                      onChange={(v) => setDraftEdit({ ...draftEdit, summary: v })}
                    />
                    <Field
                      label="开放问题（每行一个）"
                      multiline
                      value={(draftEdit.open_questions ?? []).join("\n")}
                      onChange={(v) =>
                        setDraftEdit({ ...draftEdit, open_questions: v.split("\n").filter(Boolean) })
                      }
                    />
                    <Field
                      label="下一步"
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
                        保存草稿修改
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => {
                          setDraftEdit(null);
                          setPinnedRevision(null);
                        }}
                      >
                        取消
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
                    <h3 className="font-semibold">第二步：补齐目标、门槛与来源后激活</h3>
                    <p className="text-sm text-muted-foreground">
                      激活只表示课题具备可执行目标，不会自动发起研究运行。
                    </p>
                    <div className="grid gap-4 md:grid-cols-2">
                      <Field
                        label="目标版本"
                        required
                        value={activation.goal.version}
                        onChange={(v) =>
                          setActivation({ ...activation, goal: { ...activation.goal, version: v } })
                        }
                      />
                      <Field
                        label="目标来源（研究文档相对路径）"
                        required
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
                      label="目标和验收门槛"
                      required
                      multiline
                      value={activation.goal.criteria}
                      onChange={(v) =>
                        setActivation({ ...activation, goal: { ...activation.goal, criteria: v } })
                      }
                    />
                    <Field
                      label="当前摘要"
                      multiline
                      value={activation.summary ?? ""}
                      onChange={(v) => setActivation({ ...activation, summary: v })}
                    />
                    <Field
                      label="开放问题（每行一个）"
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
                      label="下一步"
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
                        激活课题
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => setActivation(null)}
                      >
                        稍后再说
                      </Button>
                    </div>
                  </form>
                )}
                {lifecycle && (
                  <form
                    role="dialog"
                    aria-label="确认生命周期操作"
                    className="space-y-3 rounded-lg border border-border p-4"
                    onSubmit={(e) => {
                      e.preventDefault();
                      changeLifecycle.mutate();
                    }}
                  >
                    <h3 className="font-semibold">{ACTION_LABELS[lifecycle.action]}</h3>
                    <p className="text-sm text-muted-foreground">
                      {ACTION_HINTS[lifecycle.action]} 当前 revision {topic.data.revision}；确认后写入不可改写的生命周期记录。
                    </p>
                    <Field
                      label={
                        REASON_REQUIRED.has(lifecycle.action) ? "原因（必填）" : "原因（可选）"
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
                        确认{ACTION_LABELS[lifecycle.action]}
                      </Button>
                      <Button type="button" variant="outline" onClick={() => setLifecycle(null)}>
                        取消
                      </Button>
                    </div>
                  </form>
                )}
                <ContextCard topicId={topic.data.topic_id} />
                <TopicRunsCard topic={topic.data} />
                {round && (
                  <form
                    className="space-y-4 rounded-lg border border-border p-4"
                    onSubmit={(e) => {
                      e.preventDefault();
                      append.mutate();
                    }}
                  >
                    <h3 className="font-semibold">追加轮次，保留本次结果</h3>
                    <Field
                      label="本轮目标版本"
                      required
                      value={round.goal_version}
                      onChange={(v) => setRoundValue("goal_version", v)}
                    />
                    <Field
                      label="本轮目标"
                      required
                      value={round.objective}
                      onChange={(v) => setRoundValue("objective", v)}
                    />
                    <Field
                      label="采取的动作"
                      required
                      multiline
                      value={round.action}
                      onChange={(v) => setRoundValue("action", v)}
                    />
                    <Field
                      label="为何这样做"
                      required
                      multiline
                      value={round.rationale}
                      onChange={(v) => setRoundValue("rationale", v)}
                    />
                    <div className="grid gap-4 md:grid-cols-2">
                      <Choice
                        label="尝试结果"
                        value={round.outcome}
                        values={["completed", "failed", "interrupted", "rejected", "paused"]}
                        onChange={(v) => setRoundValue("outcome", v)}
                      />
                      <Choice
                        label="解释置信度"
                        value={round.confidence}
                        values={["unknown", "low", "medium", "high"]}
                        onChange={(v) => setRoundValue("confidence", v)}
                      />
                    </div>
                    <Field
                      label="结论和替代解释"
                      multiline
                      value={round.conclusion}
                      onChange={(v) => setRoundValue("conclusion", v)}
                    />
                    <Field
                      label="分支尝试名称"
                      value={round.branch}
                      onChange={(v) => setRoundValue("branch", v)}
                    />
                    <Field
                      label="纠正旧轮次ID（可选）"
                      value={round.supersedes_id ?? ""}
                      onChange={(v) => setRoundValue("supersedes_id", v || null)}
                    />
                    <Field
                      label="本轮下一步"
                      value={round.next_step}
                      onChange={(v) => setRoundValue("next_step", v)}
                    />
                    <h4 className="font-medium">关联精确产物</h4>
                    {round.source_refs.map((r, i) => (
                      <div key={i} className="grid gap-2 sm:grid-cols-4">
                        <Choice
                          label={`产物类型${i + 1}`}
                          value={r.kind}
                          values={[
                            "research_run",
                            "strategy",
                            "dataset",
                            "experiment",
                            "memory",
                            "document",
                            "factor_series",
                            "simulation",
                            "backtest",
                          ]}
                          onChange={(v) =>
                            setRoundValue(
                              "source_refs",
                              round.source_refs.map((x, j) => (j === i ? { ...x, kind: v } : x)),
                            )
                          }
                        />
                        <Field
                          label={`产物ID${i + 1}`}
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
                          label={`版本${i + 1}`}
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
                          移除引用
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
                      添加产物引用
                    </Button>
                    {append.isError && (
                      <p role="alert" className="text-destructive">
                        {String(append.error)}
                      </p>
                    )}
                    <div className="flex gap-2">
                      <Button type="submit" disabled={append.isPending}>
                        保存轮次
                      </Button>
                      <Button type="button" variant="outline" onClick={() => setRound(null)}>
                        取消
                      </Button>
                    </div>
                  </form>
                )}
                <section>
                  <h3 className="mb-3 font-semibold">证据与轮次时间线</h3>
                  {entries.isLoading && <LoadingState />}
                  {entries.isError && (
                    <ErrorState message={String(entries.error)} onRetry={() => entries.refetch()} />
                  )}
                  {entries.data?.items.map((e) => (
                    <article key={e.entry_id} className="space-y-3 border-t border-border py-4">
                      <p className="text-xs text-muted-foreground">
                        <ResearchTimestamp value={e.created_at} /> · {e.created_by} · {e.entry_id}
                      </p>
                      {e.entry_type === "lifecycle" ? (
                        <>
                          <h4 className="font-medium">
                            生命周期：{STATES[e.from ?? ""] ?? e.from} →{" "}
                            {STATES[e.to ?? ""] ?? e.to}
                          </h4>
                          <p className="text-sm">原因：{e.reason || "未填写"}</p>
                          <p className="text-xs text-muted-foreground">
                            操作者 {e.actor} · 动作 {e.action}
                          </p>
                        </>
                      ) : e.topic ? (
                        <>
                          <h4 className="font-medium">
                            {e.topic.goal
                              ? `目标记录 r${e.topic_revision} · ${e.topic.goal.version}`
                              : `草稿记录 r${e.topic_revision}`}
                          </h4>
                          {e.topic.goal ? (
                            <>
                              <p className="text-sm">{e.topic.goal.criteria}</p>
                              <EvidenceLinks refs={[e.topic.goal.source]} />
                            </>
                          ) : (
                            <p className="text-sm text-muted-foreground">
                              草稿只保存问题，目标版本与来源在激活时补齐。
                            </p>
                          )}
                          <p className="text-xs text-warning">
                            与当前目标不同的版本分别保留，不自动替换历史验收门。
                          </p>
                        </>
                      ) : (
                        <>
                          <h4 className="font-medium">
                            {e.objective} · {STATES[e.outcome ?? ""] ?? e.outcome}
                          </h4>
                          <p className="text-xs text-muted-foreground">
                            {e.evidence_level === "agent_interpretation"
                              ? "agent 解释"
                              : "人工记录"}{" "}
                            · 置信度 {STATES[e.confidence ?? ""] ?? e.confidence} · 目标{" "}
                            {e.goal_version} · 分支 {e.branch}
                          </p>
                          <p className="whitespace-pre-wrap text-sm">动作：{e.action}</p>
                          <p className="whitespace-pre-wrap text-sm">理由：{e.rationale}</p>
                          <EvidenceLinks refs={e.source_refs ?? []} />
                          <p className="whitespace-pre-wrap text-sm">
                            工作结论：{e.conclusion || "尚未形成"}
                          </p>
                          {e.supersedes_id && (
                            <p className="text-sm text-warning">
                              纠正链：本轮纠正 {e.supersedes_id}，旧记录仍保留
                            </p>
                          )}
                          <p className="text-sm">下一步：{e.next_step}</p>
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
        <section aria-label="研究记忆阅读区" className="w-full min-w-0 max-w-5xl space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3 text-sm text-muted-foreground">
            <p>这里是跨课题的记忆库。课题通过引用组织相关记忆，原记录保留。</p>
            <Link className="shrink-0 text-primary underline underline-offset-4" to="/research/docs?doc=topic-organization.md">课题组织说明</Link>
          </div>
          {memoryId && (
            <section aria-label="选定记忆详情" className="min-w-0">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <h2 className="text-base font-semibold">完整记忆</h2>
                <Button variant="outline" onClick={() => setParams({ tab: "memories" })}>
                  返回记忆列表
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
                          旧引用为空或含unknown，不能验证此处解释。
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
                        查看被纠正的记忆：{memory.data.supersedes_id}
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
              title="暂无研究记忆"
              description="agent 通过研究记忆工具保存跨会话笔记，在此核查来源。"
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
                        旧引用为空、含unknown或超过摘要上限，不能验证此处解释。
                      </p>
                    )}
                    {m.supersedes_id && (
                      <Link
                        className="break-all text-sm text-primary underline"
                        to={`/research/topics?memory=${encodeURIComponent(m.supersedes_id)}`}
                      >
                        查看被纠正的记忆：{m.supersedes_id}
                      </Link>
                    )}
                  </>
                }
                actions={
                  <Button variant="outline" onClick={() => setParams({ memory: m.memory_id })}>
                    查看完整记忆与来源
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
  return (
    <div className="flex items-center gap-3">
      <Button
        variant="outline"
        disabled={offset === 0}
        onClick={() => onChange(Math.max(0, offset - 20))}
      >
        上一页
      </Button>
      <span className="text-xs text-muted-foreground">第 {offset / 20 + 1} 页</span>
      <Button variant="outline" disabled={!more} onClick={() => onChange(offset + 20)}>
        下一页
      </Button>
    </div>
  );
}
