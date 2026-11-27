import { type Page } from "@playwright/test";

export const stamp = "2026-11-01T00:00:00Z";
export const topicId = "RT-fixture-study-82ebbc3";
export const runId = "RR-19cae1ffd4034b148db0a68d506164f75";
export const jobId = "BJ-fixture-research-failure";
export const releaseId = "DR-bars-qfq-fixture-frozen-release-4e71c0";
export const expId = "EXP-fixture-not-supported";
export const accountId = "SIM-A-fixture";
export const sessionId = "SIM-S-frozen-fixture";

export const goal = {
  version: "v1", criteria: "核对冻结证据", source: { kind: "document", ref_id: "README.md" },
};
export const activeTopic = {
  topic_id: topicId, title: "Check exposure drift", status: "active", goal,
  question: "Does staggered rebalancing change exposure?", updated_at: stamp, created_at: stamp,
  created_by: "agent:test", revision: 2, summary: "", next_step: "Review the frozen inputs",
  open_questions: [], source_refs: [],
};
export const evidenceRun = {
  run_id: runId, strategy_id: "frozen-paper-strategy", strategy_kind: "multi_factor", status: "completed",
  created_at: stamp, completed_at: stamp, requested_by: "user", job_id: jobId,
  manifest: {
    strategy_version: 2, initial_capital: 100000,
    benchmark_config: { symbol: "000001.SH" },
    parameters: { fee_policy_version: "explicit_overrides_v1" },
    dataset_releases: [{ artifact_id: releaseId }],
  },
  result: { strategy_return: 0.022, benchmark_return: null, excess_return: null, commission_paid: "10.02" },
};
export const finalExperiment = {
  experiment_id: expId, status: "validated_oos", oos_outcome: "not_supported", final_test_unsealed: true,
  rejection_reason: "合成验证未达门槛", version_stamp: "frozen", hypothesis: "Synthetic test",
  plan: {}, thresholds: {}, robustness: {}, strategy_params_space: {}, trials: [], created_at: stamp,
};
export const failedJob = {
  job_id: jobId, kind: "echo", queue: "research", status: "failed", priority: 0, payload: { steps: 2 },
  payload_checksum: "safe", idempotency_key: "e2e", progress_done: 1, progress_total: 3, phase: "loading",
  attempt: 3, max_attempts: 3, worker_id: "worker:test", heartbeat_at: stamp, lease_until: null,
  requested_by: "user:test", created_at: stamp, started_at: stamp, finished_at: stamp, archived_at: stamp,
  updated_at: stamp, run_status: null, timing: null, result_ref: null, error_code: "worker_interrupted",
  error_summary: "工作器在加载研究输入时中断。",
};
export const paperAccount = {
  simulation_account_id: accountId, status: "active", name: "Paper workbench", cash: "100000", equity: "100000", created_at: stamp,
};
export const paperSession = {
  simulation_session_id: sessionId, simulation_account_id: accountId, strategy_id: evidenceRun.strategy_id,
  strategy_version: 2, status: "paused", source_mode: "historical_replay", validation_run_id: runId,
  clock: { current_at: stamp }, config: {}, recovery_count: 0, created_at: stamp,
};
export const order = {
  client_order_id: "ORD-FIXTURE-ONLY", symbol: "510300.SH", side: "buy", status: "submitted", quantity: 100,
  filled_quantity: 0, order_type: "limit", price: 4.2, is_active: true, created_at: stamp, strategy_id: "manual",
};

const defaults: Record<string, unknown> = {
  "/api/health": { status: "ok", kernel_ready: true, kill_switch_level: "off" },
  "/api/research-edition/health": { status: "ready", edition: "research", sandbox_enabled: false, tushare_configured: false },
  "/api/data/source-status": { provider: "tushare", tushare_configured: false, sandbox_enabled: false },
  "/api/data/config": {
    data_provider: "tushare", sync_enabled: true, sync_time: "15:35", download_enabled: true, download_time: "15:45",
    download_lookback_days: 5, download_markets: ["a_share"], download_types: ["stock", "etf"], revision: "0".repeat(64),
  },
  "/api/data/catalog": { items: [], total: 0 },
  "/api/research/topics": { items: [activeTopic], total: 1 },
  [`/api/research/topics/${topicId}`]: activeTopic,
  [`/api/research/topics/${topicId}/entries`]: { items: [], total: 0 },
  [`/api/research/topics/${topicId}/context`]: {
    topic: activeTopic, recent_rounds: [], open_questions: [], source_refs: [], auto_facts: [],
    agent_interpretations: [], reviewed_conclusions: [], suggested_next_reads: [],
  },
  [`/api/research/topics/${topicId}/runs`]: { items: [], has_more: false },
  [`/api/research/topics/${topicId}/artifacts`]: { items: [], total: 0 },
  "/api/research/experiments": [finalExperiment],
  [`/api/research/experiments/${expId}`]: finalExperiment,
  "/api/research/runs": [evidenceRun],
  [`/api/research/runs/${runId}`]: evidenceRun,
  [`/api/research/runs/${runId}/topics`]: { items: [], total: 0 },
  "/api/research/docs": { docs: [] },
  "/api/research/strategy-specs/registry": { strategies: [], feature_sources: [], operators: [], lifecycle_stages: [] },
  "/api/jobs": [],
  [`/api/jobs/${jobId}`]: failedJob,
  [`/api/jobs/${jobId}/research-links`]: { topic_ids: [topicId], run_ids: [runId] },
  "/api/jobs/flamegraph/meta": { replayable_kinds: {}, rejected_kinds: { echo: "自检任务" } },
  "/api/instruments": { items: [], total: 0, limit: 50, offset: 0 },
  "/api/orders": { items: [order], total: 1, limit: 200, offset: 0 },
  "/api/simulation/accounts": [paperAccount],
  "/api/simulation/sessions": [paperSession],
  [`/api/simulation/sessions/${sessionId}`]: paperSession,
  [`/api/simulation/sessions/${sessionId}/audit`]: [{ audit_id: "SIM-X-log", action: "paused", actor: "fixture", created_at: stamp }],
  [`/api/simulation/sessions/${sessionId}/orders`]: [{ simulation_order_id: "SIM-O-rejected", symbol: "TEST", status: "rejected", reject_reason: "fixture_rejection" }],
  [`/api/simulation/sessions/${sessionId}/decisions`]: [{ decision_id: "paper-decision", status: "accepted", created_at: stamp }],
};

interface WireRequest {
  method: string;
  url: URL;
  body: Record<string, unknown>;
  headers: Record<string, string>;
}
interface Reply { json: unknown; status?: number; headers?: Record<string, string>; }

export async function mockApi(
  page: Page,
  respond?: (request: WireRequest) => Reply | undefined | Promise<Reply | undefined>,
) {
  const requests: WireRequest[] = [];
  const unexpected: string[] = [];
  await page.routeWebSocket("**/ws/**", (socket) => socket.close());
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const info: WireRequest = {
      method: request.method(),
      url: new URL(request.url()),
      body: request.postDataJSON() ?? {},
      headers: request.headers(),
    };
    requests.push(info);
    const answer = await respond?.(info);
    if (!answer && info.method !== "GET") {
      unexpected.push(`${info.method} ${info.url.pathname}`);
      return route.fulfill({ status: 405, json: { detail: "No fixture authorization for this request." } });
    }
    return route.fulfill(answer ?? {
      json: defaults[info.url.pathname] ?? [],
      headers: { "X-Total-Count": "0" },
    });
  });
  return { requests, unexpected, writes: () => requests.filter((entry) => entry.method !== "GET") };
}

export async function preferences(page: Page, lang: "zh" | "en", theme: "dark" | "light") {
  await page.addInitScript(({ locale, color }) => {
    localStorage.setItem("finboard-lang", locale);
    localStorage.setItem("findashboard-theme", color);
  }, { locale: lang, color: theme });
}
