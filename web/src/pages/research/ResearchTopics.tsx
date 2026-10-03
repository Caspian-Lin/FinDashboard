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
import {
  workspaceApi,
  evidenceUrl,
  type EvidenceRef,
  type TopicInput,
  type RoundInput,
  type MemoryDetail,
} from "@/lib/research-workspace";
import { fetchJSON } from "@/lib/api";

const EMPTY: TopicInput = {
  title: "",
  question: "",
  goal: { version: "", criteria: "", source: { kind: "document", ref_id: "" } },
  status: "active",
  conclusion: "unknown",
  summary: "",
  open_questions: [],
  next_step: "",
};
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
};
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
export default function ResearchTopics() {
  const [params, setParams] = useSearchParams();
  const topicId = params.get("topic");
  const memoryId = params.get("memory");
  const [offset, setOffset] = useState(0);
  const [entryOffset, setEntryOffset] = useState(0);
  const [memoryOffset, setMemoryOffset] = useState(0);
  const tab = params.get("tab") ?? (memoryId ? "memories" : "topics");
  const setTab = (value: string) =>
    setParams(
      value === "topics"
        ? topicId
          ? { topic: topicId }
          : {}
        : { tab: value, ...(memoryId ? { memory: memoryId } : {}) },
    );
  const [form, setForm] = useState<TopicInput | null>(null);
  const [round, setRound] = useState<RoundInput | null>(null);
  const [key, setKey] = useState("");
  const qc = useQueryClient();
  const topics = useQuery({
    queryKey: ["topics", offset],
    queryFn: () => workspaceApi.topics(offset),
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
  const save = useMutation({
    mutationFn: () =>
      topicId && topic.data
        ? workspaceApi.update(topicId, form!, topic.data.revision)
        : workspaceApi.create(form!),
    onSuccess: (t) => {
      setForm(null);
      setParams({ topic: t.topic_id });
      void qc.invalidateQueries({ queryKey: ["topics"] });
      void qc.invalidateQueries({ queryKey: ["topic"] });
      void qc.invalidateQueries({ queryKey: ["topic-entries"] });
    },
  });
  const append = useMutation({
    mutationFn: () => workspaceApi.append(topicId!, round!, key),
    onSuccess: () => {
      setRound(null);
      void qc.invalidateQueries({ queryKey: ["topic-entries"] });
    },
  });
  const setValue = <K extends keyof TopicInput>(name: K, value: TopicInput[K]) =>
    setForm((f) => (f ? { ...f, [name]: value } : f));
  const setRoundValue = <K extends keyof RoundInput>(name: K, value: RoundInput[K]) =>
    setRound((r) => (r ? { ...r, [name]: value } : r));
  const startRound = () => {
    setKey(crypto.randomUUID());
    setRound({
      entry_type: "round",
      goal_version: topic.data?.goal.version ?? "",
      objective: "",
      action: "",
      rationale: "",
      outcome: "completed",
      conclusion: "",
      confidence: "unknown",
      next_step: "",
      source_refs: [],
      supersedes_id: null,
      branch: "main",
    });
  };
  return (
    <div className="space-y-6">
      <PageHeader
        title="研究课题"
        description="围绕问题组织每轮尝试、证据和下一步，随时暂停与续接。"
        actions={
          <Button
            onClick={() => {
              setParams({});
              setForm(structuredClone(EMPTY));
              setRound(null);
              save.reset();
            }}
          >
            新建课题
          </Button>
        }
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
        解释与工作结论各有来源。正式接受的结论以仓库研究文档为准，冲突时保留双方记录；旧记忆中的故障和假设需重新核验。
      </p>
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
      {tab === "topics" && !form && (
        <div className="grid gap-6 lg:grid-cols-[18rem_minmax(0,1fr)]">
          <aside className="space-y-3">
            {topics.isLoading ? (
              <LoadingState />
            ) : topics.isError ? (
              <ErrorState message={String(topics.error)} onRetry={() => topics.refetch()} />
            ) : topics.data?.items.length === 0 ? (
              <EmptyState
                title="还没有研究课题"
                description="为一个待验证的问题建立课题，已有运行和记忆可在轮次中关联。"
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
                  }}
                >
                  <p className="font-medium">{t.title}</p>
                  <p className="mt-2 text-xs text-muted-foreground">
                    {STATES[t.status]} · {STATES[t.conclusion]}
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
                    {STATES[topic.data.status]} · {STATES[topic.data.conclusion]} · revision{" "}
                    {topic.data.revision}
                  </p>
                  <p className="text-sm">
                    当前目标 {topic.data.goal.version}：{topic.data.goal.criteria}
                  </p>
                  <EvidenceLinks refs={[topic.data.goal.source]} />
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
                    <Button onClick={startRound}>记录新轮次</Button>
                    <Button
                      variant="outline"
                      onClick={() => {
                        setForm({
                          title: topic.data!.title,
                          question: topic.data!.question,
                          goal: topic.data!.goal,
                          status: topic.data!.status,
                          conclusion: topic.data!.conclusion,
                          summary: topic.data!.summary,
                          open_questions: topic.data!.open_questions,
                          next_step: topic.data!.next_step,
                        });
                        save.reset();
                      }}
                    >
                      更新 / 暂停 / 收束
                    </Button>
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
                    <Link className="text-primary underline" to="/research/data">
                      数据工具
                    </Link>
                    <Link className="text-primary underline" to="/research/factors">
                      因子工具
                    </Link>
                  </div>
                </section>
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
                      {e.topic ? (
                        <>
                          <h4 className="font-medium">
                            目标记录 r{e.topic_revision} · {e.topic.goal.version}
                          </h4>
                          <p className="text-sm">{e.topic.goal.criteria}</p>
                          <EvidenceLinks refs={[e.topic.goal.source]} />
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
