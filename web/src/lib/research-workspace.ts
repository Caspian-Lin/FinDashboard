import { fetchJSON } from "./api";

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
export interface Topic extends TopicInput {
  topic_id: string;
  revision: number;
  created_by: string;
  created_at: string;
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
export interface Entry extends Partial<RoundInput> {
  entry_id: string;
  topic_id: string;
  created_by: string;
  created_at: string;
  topic?: TopicInput;
  topic_revision?: number;
  evidence_level: string;
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
export const workspaceApi = {
  topics: (offset = 0) => fetchJSON<Page<Topic>>(`/research/topics?limit=20&offset=${offset}`),
  topic: (id: string) => fetchJSON<Topic>(`/research/topics/${encodeURIComponent(id)}`),
  entries: (id: string, offset = 0) =>
    fetchJSON<Page<Entry>>(
      `/research/topics/${encodeURIComponent(id)}/entries?limit=20&offset=${offset}`,
    ),
  memories: (offset = 0) =>
    fetchJSON<Page<Memory>>(`/research/topics/memories?limit=20&offset=${offset}`),
  create: (body: TopicInput) =>
    fetchJSON<Topic>("/research/topics", { method: "POST", body: JSON.stringify(body) }),
  update: (id: string, body: TopicInput, revision: number) =>
    fetchJSON<Topic>(`/research/topics/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({ ...body, expected_revision: revision }),
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
        dataset: `/research/data?release=${id}`,
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
