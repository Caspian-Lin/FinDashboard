import { fetchJSON } from "./api";
import type { ResearchRunQueueIn } from "./research";

export interface EvidenceRef {
  kind: string;
  ref_id: string;
  version?: string;
  checksum?: string;
}
export interface Goal {
  version: string;
  criteria: string;
  source: EvidenceRef;
}
export type TopicStatus = "draft" | "active" | "paused" | "closed";
export interface TopicDraftInput {
  title: string;
  question: string;
}
/** Draft edits carry no goal; activation is the only way to add one. */
export interface TopicDraftUpdate {
  title: string;
  question: string;
  conclusion?: string;
  summary?: string;
  open_questions?: string[];
  next_step?: string;
}
export interface TopicInput {
  title: string;
  question: string;
  goal: Goal;
  status: string;
  conclusion: string;
  summary: string;
  open_questions: string[];
  next_step: string;
}
export interface Topic extends Omit<TopicInput, "goal"> {
  topic_id: string;
  revision: number;
  created_by: string;
  created_at: string;
  updated_at?: string;
  /** Drafts have no goal until activation. */
  goal: Goal | null;
  /** Server-side creation receipt: false means an idempotent retry hit. */
  created?: boolean;
}
export interface RoundInput {
  entry_type: "round" | "evidence";
  goal_version: string;
  objective: string;
  action: string;
  rationale: string;
  outcome: string;
  conclusion: string;
  confidence: string;
  next_step: string;
  source_refs: EvidenceRef[];
  supersedes_id: string | null;
  branch: string;
}
export interface EntryTopic extends Omit<TopicInput, "goal"> {
  goal: Goal | null;
}
export interface Entry extends Omit<Partial<RoundInput>, "entry_type"> {
  entry_id: string;
  topic_id: string;
  created_by: string;
  created_at: string;
  checksum?: string;
  topic?: EntryTopic;
  topic_revision?: number;
  evidence_level: string;
  /** round/evidence, plus goal snapshots, initial drafts and lifecycle audits. */
  entry_type?: "round" | "evidence" | "goal" | "draft" | "lifecycle";
  // Lifecycle entries (entry_type=lifecycle) carry the transition audit.
  from?: string;
  to?: string;
  action?: string;
  actor?: string;
  reason?: string;
  at?: string;
}
export interface LifecycleTransition {
  from?: string | null;
  to?: string | null;
  reason?: string | null;
  actor?: string | null;
  action?: string | null;
  at?: string;
  entry_id?: string;
}
export interface ContextRound {
  entry_id: string;
  branch: string;
  outcome?: string | null;
  objective?: string;
  conclusion_excerpt: string;
  next_step?: string;
  created_at: string;
  created_by: string;
  evidence_level: string;
}
export interface DanglingRef {
  ref: EvidenceRef;
  status: string;
  entry_ids?: string[];
}
export interface NextAction {
  action: string;
  why: string;
}
export interface ActiveLink {
  entity_type: string;
  entity_id: string;
  job_id?: string | null;
  state: string;
  goal_version?: string;
}
export interface TopicContext {
  topic: {
    topic_id: string;
    revision: number;
    title?: string;
    question?: string;
    status: string;
    conclusion: string;
    summary: string;
    updated_at?: string | null;
  };
  goal: Goal | null;
  lifecycle: { since: string; last: LifecycleTransition | null };
  open_questions: string[];
  next_step: string;
  recent_rounds: ContextRound[];
  active_links: ActiveLink[];
  dangling_refs: DanglingRef[];
  evidence_levels: Record<string, string>;
  next_actions: NextAction[];
  generated_at: string;
  revision: number;
  excerpted: boolean;
  truncated_sections?: string[];
  bounds: Record<string, number>;
}
export interface BranchSummary {
  branch: string;
  count: number;
  last_at: string | null;
  last_outcome: string | null;
}
export interface RefCheckItem {
  ref: EvidenceRef;
  status: string;
  facts?: Record<string, unknown>;
  entry_ids: string[];
  error?: string;
}
export interface RefCheckPage {
  items: RefCheckItem[];
  total: number;
  has_more: boolean;
  offset: number;
  limit: number;
  rounds: number;
}
/** Structured 409 payload (`detail`) shared by revision and state conflicts. */
export interface TopicConflictDetail {
  code: string;
  current_revision?: number;
  your_revision?: number;
  changed_fields?: string[];
  current_summary?: {
    status?: string;
    revision?: number;
    goal_version?: string | null;
    title?: string | null;
  };
  allowed?: string[];
  /** Action names (pause/close/...) when the server offers them. */
  actions?: string[];
  current?: string;
  action?: string;
  idempotency_key?: string;
}
export interface Memory {
  memory_id: string;
  memory_type: string;
  excerpt: string;
  content_length: number;
  status: string;
  source_refs: EvidenceRef[];
  created_by: string;
  confirmed_by: string | null;
  supersedes_id: string | null;
  created_at: string;
  unverifiable_refs: boolean;
  refs_truncated?: boolean;
}
export interface MemoryDetail
  extends Omit<Memory, "excerpt" | "content_length" | "unverifiable_refs"> {
  content: string;
  tags: string[];
  updated_at: string;
  confirmed_at: string | null;
  conversation_id: string | null;
}
export interface Page<T> {
  items: T[];
  has_more: boolean;
  offset: number;
  limit: number;
}
export interface FactorExplanation {
  name: string;
  node_id: string;
  sources: Record<string, unknown>[];
  parameters: Record<string, unknown> | null;
  formula: string | null;
  window?: number;
  unit: string;
  raw_direction: string;
  effective_direction: Record<string, string>;
  evidence: string;
  implementation_version?: string;
  dependencies?: string[];
}
export interface Explanation {
  provenance: Record<string, unknown>;
  spec: Record<string, unknown>;
  factors: FactorExplanation[];
  effective_policies: Record<string, unknown>;
  overrides: Record<string, unknown>;
  schedule: Record<string, unknown>;
  gaps: string[];
  warnings: string[];
  mechanism_hypotheses: {
    claim: string;
    level: string;
    source: string;
    counterparty: string;
    failure: string;
  }[];
}
export interface DecisionItem {
  decision_id?: string;
  business_date?: string;
  decision_at?: string;
  stage?: string;
  checksum?: string;
  trace_id?: string;
  item?: Record<string, unknown>;
}
export interface ActivateInput {
  goal: Goal;
  conclusion?: string;
  summary?: string;
  open_questions?: string[];
  next_step?: string;
  reason?: string;
}
/** A link from a topic/goal version to a research artifact (#515). */
export interface TopicRunLink {
  link_id: number;
  topic_id: string;
  goal_version: string;
  entry_id: string | null;
  entity_type: string;
  entity_id: string;
  run_id: string;
  job_id: string | null;
  operation_id: string | null;
  created_by: string;
  created_at: string | null;
  /** Join-derived: queued/running/succeeded/failed/cancelled/interrupted. */
  state: string;
  run_status: string | null;
  job_status: string | null;
  progress_done: number | null;
  progress_total: number | null;
  phase: string | null;
  job_attempt: number | null;
  job_max_attempts: number | null;
  error_code: string | null;
  error_summary: string | null;
  result_checksum: string | null;
  manifest_checksum: string | null;
  run_created_at: string | null;
  run_completed_at: string | null;
  deep_link: string;
}
export interface TopicRunPage {
  items: TopicRunLink[];
  has_more: boolean;
  next_cursor: string | null;
  limit: number;
}
export interface TopicRunStartInput extends ResearchRunQueueIn {
  goal_version: string;
  operation_id?: string;
  /** Content-addressed factor series (FS-), optional. */
  factor_series_ids?: string[];
}
export interface TopicRunStartReceipt {
  topic_id: string;
  goal_version: string;
  run_id: string;
  job_id: string | null;
  operation_id: string | null;
  created: boolean;
  status: string | null;
  manifest_checksum: string | null;
  link?: TopicRunLink;
}
export interface RunTopicLink {
  link_id: number;
  topic_id: string;
  title: string | null;
  topic_status: string;
  topic_revision: number;
  goal_version: string;
  entry_id: string | null;
  operation_id: string | null;
  created_by: string;
  created_at: string | null;
}
export const workspaceApi = {
  topics: (offset = 0, status?: string, sort = "updated") =>
    fetchJSON<Page<Topic>>(
      `/research/topics?limit=20&offset=${offset}&sort=${sort}${status ? `&status=${status}` : ""}`,
    ),
  topic: (id: string) => fetchJSON<Topic>(`/research/topics/${encodeURIComponent(id)}`),
  entries: (id: string, offset = 0) =>
    fetchJSON<Page<Entry>>(
      `/research/topics/${encodeURIComponent(id)}/entries?limit=20&offset=${offset}`,
    ),
  context: (id: string, rounds = 10, refs = 10) =>
    fetchJSON<TopicContext>(
      `/research/topics/${encodeURIComponent(id)}/context?rounds=${rounds}&refs=${refs}`,
    ),
  branches: (id: string) =>
    fetchJSON<Page<BranchSummary>>(`/research/topics/${encodeURIComponent(id)}/branches`),
  refsCheck: (id: string, rounds = 10) =>
    fetchJSON<RefCheckPage>(
      `/research/topics/${encodeURIComponent(id)}/refs/check?rounds=${rounds}`,
    ),
  memories: (offset = 0) =>
    fetchJSON<Page<Memory>>(`/research/topics/memories?limit=20&offset=${offset}`),
  create: (body: TopicInput, idempotencyKey?: string) =>
    fetchJSON<Topic>("/research/topics", {
      method: "POST",
      body: JSON.stringify({
        ...body,
        ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
      }),
    }),
  createDraft: (body: TopicDraftInput, idempotencyKey?: string) =>
    fetchJSON<Topic>("/research/topics", {
      method: "POST",
      body: JSON.stringify({
        ...body,
        ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
      }),
    }),
  update: (id: string, body: TopicInput, revision: number) =>
    fetchJSON<Topic>(`/research/topics/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({ ...body, expected_revision: revision }),
    }),
  updateDraft: (id: string, body: TopicDraftUpdate, revision: number) =>
    fetchJSON<Topic>(`/research/topics/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({ ...body, expected_revision: revision }),
    }),
  activate: (id: string, body: ActivateInput, revision: number) =>
    fetchJSON<Topic>(`/research/topics/${encodeURIComponent(id)}/activate`, {
      method: "POST",
      body: JSON.stringify({ ...body, expected_revision: revision }),
    }),
  transition: (id: string, action: string, reason: string, revision: number) =>
    fetchJSON<Topic>(`/research/topics/${encodeURIComponent(id)}/transitions`, {
      method: "POST",
      body: JSON.stringify({ action, reason, expected_revision: revision }),
    }),
  append: (id: string, body: RoundInput, key: string) =>
    fetchJSON<Entry>(`/research/topics/${encodeURIComponent(id)}/entries`, {
      method: "POST",
      body: JSON.stringify({ round: body, idempotency_key: key }),
    }),
  explain: (runId?: string, strategyId?: string, version?: number) =>
    fetchJSON<Explanation>(
      `/research/explanation?${new URLSearchParams(runId ? { run_id: runId } : { strategy_id: strategyId ?? "", version: String(version) })}`,
    ),
  decisions: (id: string, params: Record<string, string> = {}) =>
    fetchJSON<Page<DecisionItem>>(
      `/research/explanation/${encodeURIComponent(id)}/decisions?${new URLSearchParams(params)}`,
    ),
};

export const topicRunApi = {
  start: (topicId: string, body: TopicRunStartInput) =>
    fetchJSON<TopicRunStartReceipt>(`/research/topics/${encodeURIComponent(topicId)}/runs`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  list: (topicId: string, limit = 20, cursor?: string) =>
    fetchJSON<TopicRunPage>(
      `/research/topics/${encodeURIComponent(topicId)}/runs?limit=${limit}${
        cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""
      }`,
    ),
  get: (topicId: string, runId: string) =>
    fetchJSON<TopicRunLink>(
      `/research/topics/${encodeURIComponent(topicId)}/runs/${encodeURIComponent(runId)}`,
    ),
  retry: (
    topicId: string,
    runId: string,
    body: { idempotency_key: string; requested_by: string; operation_id: string },
  ) =>
    fetchJSON<TopicRunStartReceipt>(
      `/research/topics/${encodeURIComponent(topicId)}/runs/${encodeURIComponent(runId)}/retry`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  cancel: (topicId: string, runId: string) =>
    fetchJSON<TopicRunLink>(
      `/research/topics/${encodeURIComponent(topicId)}/runs/${encodeURIComponent(runId)}/cancel`,
      { method: "POST" },
    ),
  linkRun: (
    runId: string,
    body: { topic_id: string; goal_version: string; operation_id?: string },
  ) =>
    fetchJSON<TopicRunLink & { created: boolean }>(
      `/research/runs/${encodeURIComponent(runId)}/link`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  runTopics: (runId: string) =>
    fetchJSON<{ run_id: string; items: RunTopicLink[]; count: number }>(
      `/research/runs/${encodeURIComponent(runId)}/topics`,
    ),
};

/** Extract the structured 409 body without importing the ApiError class. */
export function conflictDetail(error: unknown): TopicConflictDetail | null {
  const detail = (error as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === "object" && "code" in detail) {
    const record = detail as { code?: unknown };
    if (typeof record.code === "string") return detail as TopicConflictDetail;
  }
  return null;
}

export function evidenceUrl(ref: EvidenceRef): string | null {
  if (!ref.ref_id || ref.kind === "unknown") return null;
  const id = encodeURIComponent(ref.ref_id);
  return (
    (
      {
        research_run: `/research/runs?run=${id}`,
        strategy: ref.version
          ? `/research/strategy-explanation?strategy=${id}&version=${encodeURIComponent(ref.version)}`
          : `/research/strategy`,
        dataset: `/data?release=${id}`,
        experiment: `/research/experiments?experiment=${id}`,
        document: `/research/docs?doc=${id}`,
        memory: `/research/topics?memory=${id}`,
        factor_series: `/research/topics?source_kind=factor_series&source_id=${id}`,
        simulation: `/research/topics?source_kind=simulation&source_id=${id}`,
        backtest: `/research/topics?source_kind=backtest&source_id=${id}`,
      } as Record<string, string>
    )[ref.kind] ?? null
  );
}
