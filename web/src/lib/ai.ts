import { fetchJSON } from "./api";

/* ============================================================ */
/* AI Research Assistant                                        */
/* ============================================================ */

export interface Provenance {
  provider: string;
  model_version: string;
  prompt_version: string;
  latency_ms: number;
  request_checksum: string;
}

export interface Citation {
  source: string;
  reference: string;
  url?: string;
}

export type DraftKind = "hypothesis" | "strategy_component" | "strategy_diff";
export type DraftStatus = "proposed" | "approved" | "consumed" | "rejected";

export interface DraftOut {
  draft_id: string;
  kind: DraftKind;
  provenance: Provenance;
  status: DraftStatus;
  payload: Record<string, unknown>;
  uncertainty: string;
  references: string[];
  approved_by?: string;
  consumed_ref?: string;
  created_at: string;
}

export interface AnswerOut {
  question: string;
  answer: string;
  technical_detail?: string;
  citations: Citation[];
  uncertainty: string;
  data_sufficient: boolean;
  disclaimer?: string;
  provenance: Provenance;
  draft_id: string;
}

export type HypothesisStatus = "proposed" | "approved" | "rejected" | "superseded";

export interface FactorHypothesis {
  hypothesis_id: string;
  name: string;
  economic_mechanism: string;
  input_fields: string[];
  decision_timing: string;
  formula: string;
  direction: string;
  applicable_assets: string[];
  expected_failure_scenarios: string[];
  parameters: { name: string; description: string; default_value: unknown }[];
  references: string[];
  status: HypothesisStatus;
  actor: string;
  created_at: string;
}

export interface HypothesisExperiment {
  experiment_id: string;
  hypothesis_id: string;
  model_version: string;
  prompt_version: string;
  dataset_version: string;
  code_version: string;
  registered_by: string;
  validation_experiment_id?: string;
  status: string;
  result?: Record<string, unknown>;
  created_at: string;
}

export interface AIAuditEvent {
  event_id: string;
  hypothesis_id?: string;
  event_type: string;
  actor: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export const aiResearchApi = {
  /* Hypotheses */
  createHypothesis: (body: Record<string, unknown>) =>
    fetchJSON<FactorHypothesis>(`/research/ai/hypotheses`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listHypotheses: (status?: string) => {
    const q = status ? `?status=${status}` : "";
    return fetchJSON<FactorHypothesis[]>(`/research/ai/hypotheses${q}`);
  },
  hypothesisDetail: (id: string) =>
    fetchJSON<FactorHypothesis>(`/research/ai/hypotheses/${id}`),
  approveHypothesis: (id: string, approver: string) =>
    fetchJSON<FactorHypothesis>(`/research/ai/hypotheses/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ approver }),
    }),
  rejectHypothesis: (id: string, approver: string, reason: string) =>
    fetchJSON<FactorHypothesis>(`/research/ai/hypotheses/${id}/reject`, {
      method: "POST",
      body: JSON.stringify({ approver, reason }),
    }),

  /* Experiments */
  registerExperiment: (hypothesisId: string, body: Record<string, unknown>) =>
    fetchJSON<HypothesisExperiment>(`/research/ai/hypotheses/${hypothesisId}/experiments`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  completeExperiment: (experimentId: string, validationExperimentId: string, completedBy: string) =>
    fetchJSON<HypothesisExperiment>(`/research/ai/experiments/${experimentId}/complete`, {
      method: "POST",
      body: JSON.stringify({
        validation_experiment_id: validationExperimentId,
        completed_by: completedBy,
      }),
    }),
  interruptExperiment: (experimentId: string, reason: string, actor: string) =>
    fetchJSON<HypothesisExperiment>(`/research/ai/experiments/${experimentId}/interrupt`, {
      method: "POST",
      body: JSON.stringify({ reason, actor }),
    }),
  listExperiments: (hypothesisId?: string) => {
    const q = hypothesisId ? `?hypothesis_id=${hypothesisId}` : "";
    return fetchJSON<HypothesisExperiment[]>(`/research/ai/experiments${q}`);
  },

  /* AI Drafts */
  generateHypothesisDraft: (question: string) =>
    fetchJSON<DraftOut>(`/research/ai/drafts/hypothesis`, {
      method: "POST",
      body: JSON.stringify({ question }),
    }),
  generateStrategyComponentDraft: (question: string) =>
    fetchJSON<DraftOut>(`/research/ai/drafts/strategy-component`, {
      method: "POST",
      body: JSON.stringify({ question }),
    }),
  generateStrategyDiffDraft: (question: string) =>
    fetchJSON<DraftOut>(`/research/ai/drafts/strategy-diff`, {
      method: "POST",
      body: JSON.stringify({ question }),
    }),
  ask: (question: string) =>
    fetchJSON<AnswerOut>(`/research/ai/ask`, {
      method: "POST",
      body: JSON.stringify({ question }),
    }),

  /* Draft approval flow */
  listDrafts: (params?: { kind?: DraftKind; status?: DraftStatus }) => {
    const q = new URLSearchParams();
    if (params?.kind) q.set("kind", params.kind);
    if (params?.status) q.set("status", params.status);
    return fetchJSON<DraftOut[]>(`/research/ai/drafts${q.toString() ? "?" + q : ""}`);
  },
  draftDetail: (id: string) =>
    fetchJSON<DraftOut>(`/research/ai/drafts/${id}`),
  approveDraft: (id: string, approver: string) =>
    fetchJSON<DraftOut>(`/research/ai/drafts/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ approver }),
    }),
  rejectDraft: (id: string, approver: string, reason: string) =>
    fetchJSON<DraftOut>(`/research/ai/drafts/${id}/reject`, {
      method: "POST",
      body: JSON.stringify({ approver, reason }),
    }),
  consumeDraft: (id: string, approver: string, consumedRef: string) =>
    fetchJSON<DraftOut>(`/research/ai/drafts/${id}/consume`, {
      method: "POST",
      body: JSON.stringify({ approver, consumed_ref: consumedRef }),
    }),

  /* Audit */
  audit: (hypothesisId?: string) => {
    const q = hypothesisId ? `?hypothesis_id=${hypothesisId}` : "";
    return fetchJSON<AIAuditEvent[]>(`/research/ai/audit${q}`);
  },
};
