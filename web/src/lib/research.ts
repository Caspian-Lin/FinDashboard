import { fetchJSON, type ApiError } from "./api";

/* ============================================================ */
/* Research Runs                                                */
/* ============================================================ */

export type ResearchRunStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface ResearchRunSummary {
  run_id: string;
  strategy_id: string;
  strategy_version: number;
  status: ResearchRunStatus;
  strategy_kind: string;
  requested_by: string;
  created_at: string;
  started_at?: string;
  completed_at?: string;
  initial_capital: number;
}

export interface ResearchArtifact {
  artifact_id: string;
  decision_id: string;
  sequence: number;
  stage: string;
  trace_id: string;
  parent_trace_ids: string[];
  payload: Record<string, unknown>;
  checksum: string;
}

export interface ResearchLineageArtifact {
  trace_id: string;
  stage: string;
  parent_trace_ids: string[];
  payload: Record<string, unknown>;
}

export interface ResearchLineage {
  run_id: string;
  leaf_trace_id: string;
  artifacts: ResearchLineageArtifact[];
}

export interface ResearchRunDetail extends ResearchRunSummary {
  manifest: Record<string, unknown>;
  result?: Record<string, unknown>;
  result_checksum?: string;
  error_code?: string;
}

export interface ResearchRunQueueIn {
  idempotency_key: string;
  strategy_id: string;
  strategy_version: number;
  dataset_release_ids: string[];
  factor_snapshot_ids: string[];
  parameters: Record<string, unknown>;
  validation_config: Record<string, unknown>;
  portfolio_config: Record<string, unknown>;
  risk_config: Record<string, unknown>;
  execution_config: Record<string, unknown>;
  fee_config: Record<string, unknown>;
  benchmark_config: Record<string, unknown>;
  code_version: string;
  initial_capital: number;
  requested_by: string;
}

export const researchRunApi = {
  list: (params?: { status?: string[]; strategy_kind?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.limit) q.set("limit", String(params.limit));
    return fetchJSON<ResearchRunSummary[]>(
      `/research/runs${q.toString() ? "?" + q : ""}`,
    );
  },
  get: (runId: string) =>
    fetchJSON<ResearchRunDetail>(`/research/runs/${runId}`),
  artifacts: (runId: string) =>
    fetchJSON<ResearchArtifact[]>(`/research/runs/${runId}/artifacts`),
  lineage: (runId: string, traceId: string) =>
    fetchJSON<ResearchLineage>(`/research/runs/${runId}/lineage/${traceId}`),
  cancel: (runId: string) =>
    fetchJSON<ResearchRunDetail>(`/research/runs/${runId}/cancel`, {
      method: "POST",
    }),
  replay: (runId: string, body: { idempotency_key: string; requested_by: string }) =>
    fetchJSON<ResearchRunSummary>(`/research/runs/${runId}/replay`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
};

/* ============================================================ */
/* Factor Lab                                                   */
/* ============================================================ */

export interface FactorCatalogEntry {
  name: string;
  role: string;
  description: string;
  version: string;
  dependencies: string[];
}

export interface FeatureSnapshot {
  snapshot_id: string;
  dataset_release_id: string;
  factor_names: string[];
  row_count: number;
  symbol_count: number;
  created_at: string;
  research_status: string;
  checksum: string;
}

export interface FactorSignal {
  signal_id: string;
  factor_name: string;
  research_status: string;
  snapshot_id: string;
  created_at: string;
  payload: Record<string, unknown>;
}

export interface FactorExperiment {
  factor_experiment_id: string;
  hypothesis: string;
  factor_names: string[];
  dataset_release_id: string;
  feature_snapshot_id: string;
  status: string;
  comparison_group?: string;
  validation_experiment_id?: string;
  created_at: string;
}

export interface FactorExperimentCreate {
  hypothesis: string;
  factor_names: string[];
  dataset_release_id: string;
  feature_snapshot_id: string;
  plan: Record<string, unknown>;
  comparison_group?: string;
  validation_experiment_id?: string;
}

export const factorLabApi = {
  catalog: (role?: string) => {
    const q = new URLSearchParams();
    if (role) q.set("role", role);
    return fetchJSON<FactorCatalogEntry[]>(
      `/research/factors/catalog${q.toString() ? "?" + q : ""}`,
    );
  },
  features: (datasetReleaseId?: string, limit?: number) => {
    const q = new URLSearchParams();
    if (datasetReleaseId) q.set("dataset_release_id", datasetReleaseId);
    if (limit) q.set("limit", String(limit));
    return fetchJSON<FeatureSnapshot[]>(
      `/research/factors/features${q.toString() ? "?" + q : ""}`,
    );
  },
  featureDetail: (snapshotId: string) =>
    fetchJSON<FeatureSnapshot>(`/research/factors/features/${snapshotId}`),
  signals: (params?: { factor_name?: string; research_status?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.factor_name) q.set("factor_name", params.factor_name);
    if (params?.research_status) q.set("research_status", params.research_status);
    if (params?.limit) q.set("limit", String(params.limit));
    return fetchJSON<FactorSignal[]>(`/research/factors/signals${q.toString() ? "?" + q : ""}`);
  },
  signalDetail: (signalId: string) =>
    fetchJSON<FactorSignal>(`/research/factors/signals/${signalId}`),
  createFactorExperiment: (body: FactorExperimentCreate) =>
    fetchJSON<FactorExperiment>(`/research/factors/experiments`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listFactorExperiments: (params?: { status?: string; comparison_group?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.status) q.set("status", params.status);
    if (params?.comparison_group) q.set("comparison_group", params.comparison_group);
    if (params?.limit) q.set("limit", String(params.limit));
    return fetchJSON<FactorExperiment[]>(`/research/factors/experiments${q.toString() ? "?" + q : ""}`);
  },
  factorExperimentDetail: (id: string) =>
    fetchJSON<FactorExperiment>(`/research/factors/experiments/${id}`),
  syncValidation: (id: string, validationExperimentId: string) =>
    fetchJSON<FactorExperiment>(
      `/research/factors/experiments/${id}/sync-validation`,
      { method: "POST", body: JSON.stringify({ validation_experiment_id: validationExperimentId }) },
    ),
};

/* ============================================================ */
/* Validation Experiments (#57)                                 */
/* ============================================================ */

export type ExperimentStatus =
  | "draft"
  | "registered"
  | "running"
  | "completed"
  | "failed"
  | "rejected";

export type TrialStatus = "pending" | "running" | "passed" | "failed" | "rejected";

export interface ValidationTrial {
  trial_id: string;
  parameters: Record<string, unknown>;
  status: TrialStatus;
  failure_reason?: string;
  created_at: string;
}

export interface ValidationExperiment {
  experiment_id: string;
  hypothesis: string;
  version_stamp: string;
  status: ExperimentStatus;
  plan: Record<string, unknown>;
  thresholds: Record<string, unknown>;
  robustness: Record<string, unknown>;
  strategy_params_space: Record<string, unknown>;
  supersedes_id?: string;
  notes?: string;
  created_at: string;
}

export interface ValidationExperimentDetail extends ValidationExperiment {
  trials: ValidationTrial[];
}

export const experimentApi = {
  list: (params?: { status?: ExperimentStatus; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.status) q.set("status", params.status);
    if (params?.limit) q.set("limit", String(params.limit));
    return fetchJSON<ValidationExperiment[]>(
      `/research/experiments${q.toString() ? "?" + q : ""}`,
    );
  },
  get: (id: string) =>
    fetchJSON<ValidationExperimentDetail>(`/research/experiments/${id}`),
  reject: (id: string, reason: string) =>
    fetchJSON<ValidationExperiment>(`/research/experiments/${id}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  delete: (id: string) =>
    fetchJSON<void>(`/research/experiments/${id}`, { method: "DELETE" }),
};

/* ============================================================ */
/* Strategy Specs (#79 no-code)                                 */
/* ============================================================ */

export interface StrategySpecRegistry {
  strategies: {
    kind: string;
    display_name: string;
    description: string;
    factor_sources: string[];
    lifecycle_stages: string[];
  }[];
  feature_sources: string[];
  operators: string[];
  lifecycle_stages: string[];
  publication_starts_run: boolean;
  accepts_python: boolean;
}

export interface ResearchStrategySpec {
  strategy_id: string;
  version: number;
  spec: Record<string, unknown>;
  checksum: string;
  published: boolean;
  created_at: string;
  created_by: string;
}

export interface StrategySpecValidation {
  checksum: string;
  feature_order: string[];
  required_factor_sources: string[];
  required_datasets: string[];
  dataset_release_ids: string[];
  lifecycle_stages: string[];
  can_execute: boolean;
  errors?: string[];
}

export interface StrategySpecDiffChange {
  path: string;
  old_value: unknown;
  new_value: unknown;
  change_type: "added" | "removed" | "modified";
}

export interface StrategySpecDiff {
  changes: StrategySpecDiffChange[];
}

export const strategySpecApi = {
  registry: () =>
    fetchJSON<StrategySpecRegistry>(`/research/strategy-specs/registry`),
  template: (kind: string, params?: { strategy_id?: string; dataset_release_ids?: string[] }) => {
    const q = new URLSearchParams();
    if (params?.strategy_id) q.set("strategy_id", params.strategy_id);
    if (params?.dataset_release_ids)
      params.dataset_release_ids.forEach((id) => q.append("dataset_release_ids", id));
    return fetchJSON<ResearchStrategySpec>(
      `/research/strategy-specs/templates/${kind}${q.toString() ? "?" + q : ""}`,
    );
  },
  validate: (spec: Record<string, unknown>, disabledFactors?: string[]) =>
    fetchJSON<StrategySpecValidation>(`/research/strategy-specs/validate`, {
      method: "POST",
      body: JSON.stringify({ spec, disabled_factors: disabledFactors ?? [] }),
    }),
  createDraft: (spec: Record<string, unknown>, expectedVersion: number) =>
    fetchJSON<ResearchStrategySpec>(`/research/strategy-specs/drafts`, {
      method: "POST",
      body: JSON.stringify({ spec, expected_version: expectedVersion }),
    }),
  supersede: (strategyId: string, spec: Record<string, unknown>, expectedVersion: number) =>
    fetchJSON<ResearchStrategySpec>(
      `/research/strategy-specs/${strategyId}/supersede`,
      { method: "POST", body: JSON.stringify({ spec, expected_version: expectedVersion }) },
    ),
  publish: (strategyId: string, version: number, expectedVersion: number) =>
    fetchJSON<ResearchStrategySpec>(
      `/research/strategy-specs/${strategyId}/publish`,
      { method: "POST", body: JSON.stringify({ version, expected_version: expectedVersion }) },
    ),
  rollback: (strategyId: string, targetVersion: number, expectedVersion: number) =>
    fetchJSON<ResearchStrategySpec>(
      `/research/strategy-specs/${strategyId}/rollback`,
      { method: "POST", body: JSON.stringify({ target_version: targetVersion, expected_version: expectedVersion }) },
    ),
  list: (limit?: number) => {
    const q = limit ? `?limit=${limit}` : "";
    return fetchJSON<ResearchStrategySpec[]>(`/research/strategy-specs${q}`);
  },
  history: (strategyId: string) =>
    fetchJSON<ResearchStrategySpec[]>(`/research/strategy-specs/${strategyId}/history`),
  version: (strategyId: string, version: number) =>
    fetchJSON<ResearchStrategySpec>(
      `/research/strategy-specs/${strategyId}/versions/${version}`,
    ),
  diff: (strategyId: string, fromVersion: number, toVersion: number) =>
    fetchJSON<StrategySpecDiff>(
      `/research/strategy-specs/${strategyId}/diff?from_version=${fromVersion}&to_version=${toVersion}`,
    ),
};

/* ============================================================ */
/* Datasets & Instruments metadata                              */
/* ============================================================ */

export interface DatasetReleaseSummary {
  release_id: string;
  dataset_name: string;
  version: string;
  schema_version: string;
  start_date: string;
  end_date: string;
  period: string;
  adjustment: string;
  symbol_count: number;
  row_count: number;
  coverage_pct: number;
  capabilities: string[];
  quality_status: string;
  release_checksum: string;
}

export interface DatasetManifest {
  dataset_name: string;
  source: string;
  version: string;
  row_count: number;
  symbol_count: number;
  coverage_pct: number;
  gaps: number;
  checksum: string;
  quality_status: string;
  quality_report: Record<string, unknown>;
}

export interface InstrumentMetadata {
  code: string;
  name: string;
  market: string;
  instrument_type: string;
  listed_date?: string;
  delisted_date?: string;
  is_st?: boolean;
  is_suspended?: boolean;
}

export interface LifecycleEvent {
  event_id: string;
  symbol: string;
  event_type: string;
  event_date: string;
  description: string;
  details: Record<string, unknown>;
}

export const datasetApi = {
  manifests: (params?: { dataset_name?: string; quality_status?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.dataset_name) q.set("dataset_name", params.dataset_name);
    if (params?.quality_status) q.set("quality_status", params.quality_status);
    if (params?.limit) q.set("limit", String(params.limit));
    return fetchJSON<DatasetManifest[]>(`/instruments/datasets/manifests${q.toString() ? "?" + q : ""}`);
  },
  releases: (params?: { dataset_name?: string; source?: string; quality_status?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.dataset_name) q.set("dataset_name", params.dataset_name);
    if (params?.source) q.set("source", params.source);
    if (params?.quality_status) q.set("quality_status", params.quality_status);
    if (params?.limit) q.set("limit", String(params.limit));
    return fetchJSON<DatasetReleaseSummary[]>(
      `/instruments/datasets/releases${q.toString() ? "?" + q : ""}`,
    );
  },
  releaseDetail: (releaseId: string) =>
    fetchJSON<Record<string, unknown>>(`/instruments/datasets/releases/${releaseId}`),
  instruments: (params?: { market?: string; instrument_type?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.market) q.set("market", params.market);
    if (params?.instrument_type) q.set("instrument_type", params.instrument_type);
    q.set("limit", String(params?.limit ?? 100));
    return fetchJSON<InstrumentMetadata[]>(`/instruments${q.toString() ? "?" + q : ""}`);
  },
  instrumentDetail: (code: string) =>
    fetchJSON<InstrumentMetadata>(`/instruments/${code}`),
  lifecycle: (symbol: string, params?: { event_type?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.event_type) q.set("event_type", params.event_type);
    q.set("limit", String(params?.limit ?? 50));
    return fetchJSON<LifecycleEvent[]>(`/instruments/lifecycle/${symbol}${q.toString() ? "?" + q : ""}`);
  },
};

export type { ApiError };
