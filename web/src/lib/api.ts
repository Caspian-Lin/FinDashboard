const BASE = "/api";

// ---- 统一后台任务队列(background_jobs, BJ- ID) ----
// 与后端 JobOut(job_schemas.py)逐字段对齐;POST 提交端点统一返回 202 + JobOut,
// 前端拿到 job_id 后用 getJob 轮询 /api/jobs/{job_id} 直到终态再按 result_ref 取详情。
export type JobStatus =
  | "queued"
  | "running"
  | "retry_waiting"
  | "succeeded"
  | "failed"
  | "cancel_requested"
  | "cancelled"
  | "interrupted";

export const TERMINAL_JOB_STATUSES: ReadonlySet<JobStatus> = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
]);

export interface JobOut {
  job_id: string;
  kind: string;
  queue: string;
  status: JobStatus;
  priority: number;
  payload: Record<string, unknown>;
  payload_checksum: string;
  idempotency_key: string;
  progress_total: number;
  progress_done: number;
  phase: string | null;
  result_ref: string | null;
  error_code: string | null;
  error_summary: string | null;
  attempt: number;
  max_attempts: number;
  worker_id: string | null;
  heartbeat_at: string | null;
  lease_until: string | null;
  requested_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string;
}

export function isJobRunning(job: Pick<JobOut, "status"> | undefined | null): boolean {
  if (!job) return false;
  return job.status === "queued" || job.status === "running" || job.status === "retry_waiting" || job.status === "cancel_requested";
}

export function isJobTerminal(job: Pick<JobOut, "status"> | undefined | null): boolean {
  return Boolean(job && TERMINAL_JOB_STATUSES.has(job.status));
}

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function errorMessage(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) =>
        typeof item === "object" && item !== null && "msg" in item
          ? String(item.msg)
          : String(item),
      )
      .join("；");
  }
  if (detail && typeof detail === "object") {
    const record = detail as Record<string, unknown>;
    if (typeof record.message === "string") return record.message;
    if (typeof record.msg === "string") return record.msg;
    try {
      return JSON.stringify(detail);
    } catch {
      return fallback;
    }
  }
  return fallback;
}

export async function fetchJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new ApiError(
      resp.status,
      body.detail,
      errorMessage(body.detail, resp.statusText || `${resp.status}`),
    );
  }
  if (resp.status === 204) return undefined as T;
  return resp.json();
}

export interface Health {
  status: string;
  kernel_ready: boolean;
  kill_switch_level: string;
}

export interface Account {
  account_id: string;
  broker_kind: string;
  total_asset: string;
  cash: string;
  frozen_cash: string;
  margin_used: string;
  updated_at: string;
}

export interface Position {
  symbol: string;
  market: string;
  position_side: string;
  total_quantity: string;
  available_quantity: string;
  frozen_quantity: string;
  average_price: string;
  market_value: string;
  unrealized_pnl: string;
  updated_at: string;
}

export interface Order {
  client_order_id: string;
  symbol: string;
  market: string;
  side: string;
  order_type: string;
  quantity: string;
  strategy_id: string | null;
  price: string | null;
  status: string;
  filled_quantity: string;
  average_fill_price: string | null;
  is_active: boolean;
  is_terminal: boolean;
  remaining_quantity: string;
  created_at: string;
  updated_at: string;
}

export interface Fill {
  fill_id: string;
  client_order_id: string;
  symbol: string;
  side: string;
  quantity: string;
  price: string;
  commission: string;
  tax: string;
  filled_at: string;
}

export interface KillSwitch {
  level: string;
  allows_new_orders: boolean;
  allows_reduce_only: boolean;
}

export interface ReconcileResult {
  ok: boolean;
  summary: string;
}

export interface PageResponse<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface OrderCreate {
  symbol: string;
  market?: string;
  side: string;
  order_type: string;
  quantity: string;
  price?: string;
  time_in_force?: string;
}

export const api = {
  health: () => fetchJSON<Health>("/health"),
  getAccount: () => fetchJSON<Account>("/account"),
  refreshAccount: () => fetchJSON<Account>("/account/refresh", { method: "POST" }),
  getPositions: (source: "local" | "broker") =>
    fetchJSON<PageResponse<Position>>(`/positions?source=${source}`),
  getOrders: (params?: { status?: string; symbol?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.status) q.set("status", params.status);
    if (params?.symbol) q.set("symbol", params.symbol);
    q.set("limit", String(params?.limit ?? 100));
    return fetchJSON<PageResponse<Order>>(`/orders?${q}`);
  },
  getOrder: (cid: string) => fetchJSON<Order>(`/orders/${cid}`),
  placeOrder: (body: OrderCreate) =>
    fetchJSON<Order>("/orders", { method: "POST", body: JSON.stringify(body) }),
  cancelOrder: (cid: string) =>
    fetch(`${BASE}/orders/${cid}`, { method: "DELETE" }),
  getFills: (limit = 100) =>
    fetchJSON<PageResponse<Fill>>(`/fills?limit=${limit}`),
  getKillSwitch: () => fetchJSON<KillSwitch>("/kill-switch"),
  activateKillSwitch: (level: string, reason: string) =>
    fetchJSON<KillSwitch>("/kill-switch", {
      method: "POST",
      body: JSON.stringify({ level, reason }),
    }),
  triggerReconcile: () =>
    fetchJSON<ReconcileResult>("/reconcile", { method: "POST" }),
  getAuditLogs: (limit = 100) =>
    fetchJSON<PageResponse<{ id: number; actor: string; action: string; target: string | null; payload: string | null; created_at: string }>>(
      `/audit-logs?limit=${limit}`,
    ),

  // ---- Data ----
  getDataStatus: () => fetchJSON<DataStatus[]>("/data/status"),
  getDataStatusPage: (limit = 200, offset = 0) =>
    fetchJSON<DataStatusList>(`/data/status-page?limit=${limit}&offset=${offset}`),
  getTushareQuota: () => fetchJSON<TushareQuota>("/data/tushare-quota"),
  fetchData: (body: DataFetchRequest) =>
    fetchJSON<FetchResult>("/data/fetch", { method: "POST", body: JSON.stringify(body) }),
  fetchAllData: () =>
    fetchJSON<JobOut>("/data/fetch-all", { method: "POST" }),
  getSymbolPool: () => fetchJSON<SymbolPool>("/data/symbols"),
  updateSymbolPool: (body: SymbolPoolUpdate) =>
    fetchJSON<SymbolPool>("/data/symbols", { method: "PUT", body: JSON.stringify(body) }),

  // ---- Backtest ----
  getStrategies: () => fetchJSON<StrategyInfo[]>("/backtest/strategies"),
  runBacktest: (body: BacktestRunRequest) =>
    fetchJSON<JobOut>("/backtest/run", { method: "POST", body: JSON.stringify(body) }),
  getBacktestHistory: (limit = 50) =>
    fetchJSON<BacktestHistoryItem[]>(`/backtest/history?limit=${limit}`),
  getBacktestHistoryDetail: (id: number) =>
    fetchJSON<BacktestHistoryDetail>(`/backtest/history/${id}`),
  deleteBacktestHistory: (id: number) =>
    fetch(`${BASE}/backtest/history/${id}`, { method: "DELETE" }),

  // ---- Strategy presets ----
  getStrategyPresets: () => fetchJSON<StrategyPreset[]>("/strategy-presets"),
  getStrategyPreset: (id: number) =>
    fetchJSON<StrategyPreset>(`/strategy-presets/${id}`),
  createStrategyPreset: (body: StrategyPresetInput) =>
    fetchJSON<StrategyPreset>("/strategy-presets", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateStrategyPreset: (id: number, body: Partial<StrategyPresetInput>) =>
    fetchJSON<StrategyPreset>(`/strategy-presets/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteStrategyPreset: (id: number) =>
    fetchJSON<void>(`/strategy-presets/${id}`, { method: "DELETE" }),

  // ---- Watchlists ----
  getWatchlists: () => fetchJSON<Watchlist[]>("/watchlists"),
  createWatchlist: (body: { name: string; description?: string }) =>
    fetchJSON<Watchlist>("/watchlists", { method: "POST", body: JSON.stringify(body) }),
  getWatchlist: (id: number) => fetchJSON<WatchlistDetail>(`/watchlists/${id}`),
  updateWatchlist: (id: number, body: { name?: string; description?: string }) =>
    fetchJSON<Watchlist>(`/watchlists/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteWatchlist: (id: number) =>
    fetch(`${BASE}/watchlists/${id}`, { method: "DELETE" }),
  addWatchlistSymbols: (id: number, symbols: string[]) =>
    fetchJSON<WatchlistDetail>(`/watchlists/${id}/symbols`, {
      method: "POST",
      body: JSON.stringify({ symbols }),
    }),
  removeWatchlistSymbol: (id: number, code: string) =>
    fetchJSON<WatchlistDetail>(`/watchlists/${id}/symbols/${code}`, { method: "DELETE" }),

  // ---- Instruments ----
  getInstruments: (params?: {
    market?: string;
    instrument_type?: string;
    exchange?: string;
    listing_boards?: string[];
    q?: string;
    limit?: number;
    offset?: number;
  }) => {
    const q = new URLSearchParams();
    if (params?.market) q.set("market", params.market);
    if (params?.instrument_type) q.set("instrument_type", params.instrument_type);
    if (params?.exchange) q.set("exchange", params.exchange);
    params?.listing_boards?.forEach((board) => q.append("listing_board", board));
    if (params?.q) q.set("q", params.q);
    q.set("limit", String(params?.limit ?? 200));
    q.set("offset", String(params?.offset ?? 0));
    return fetchJSON<InstrumentList>(`/data/instruments?${q}`);
  },
  searchInstruments: (query: string) =>
    fetchJSON<InstrumentItem[]>(`/data/instruments/search?q=${encodeURIComponent(query)}`),
  getInstrumentCodes: (params?: { market?: string; instrument_type?: string; exchange?: string; listing_boards?: string[]; q?: string }) => {
    const q = new URLSearchParams();
    if (params?.market) q.set("market", params.market);
    if (params?.instrument_type) q.set("instrument_type", params.instrument_type);
    if (params?.exchange) q.set("exchange", params.exchange);
    params?.listing_boards?.forEach((board) => q.append("listing_board", board));
    if (params?.q) q.set("q", params.q);
    return fetchJSON<string[]>(`/data/instruments/codes?${q}`);
  },

  // ---- Data Sync & Bulk Download ----
  syncUniverse: () =>
    fetchJSON<JobOut>("/data/sync", { method: "POST" }),
  startBulkDownload: (body: {
    market?: string;
    instrument_type?: string;
    exchange?: string;
    listing_boards?: string[];
    start?: string;
    source?: string;
  }) =>
    fetchJSON<JobOut>("/data/bulk-download", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // GET /data/bulk-download/status 已随 #144 删除;前端统一用 getJob 轮询 /api/jobs/{job_id}。
  checkQuality: (symbols?: string, adjust?: string) => {
    const q = new URLSearchParams();
    if (symbols) q.set("symbols", symbols);
    if (adjust) q.set("adjust", adjust);
    const qs = q.toString();
    return fetchJSON<QualityReport[]>(`/data/quality${qs ? `?${qs}` : ""}`);
  },
  repairQuality: (body: { symbols: string[]; source: "akshare" | "yfinance" | "tushare"; adjust?: string }) =>
    fetchJSON<JobOut>("/data/quality/repair", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // ---- Unified Job Queue (background_jobs) ----
  getJob: (jobId: string) => fetchJSON<JobOut>(`/jobs/${encodeURIComponent(jobId)}`),
  listJobs: (params?: {
    kind?: string[];
    status?: JobStatus[];
    queue?: string[];
    limit?: number;
  }) => {
    const q = new URLSearchParams();
    params?.kind?.forEach((k) => q.append("kind", k));
    params?.status?.forEach((s) => q.append("status", s));
    params?.queue?.forEach((qq) => q.append("queue", qq));
    if (params?.limit) q.set("limit", String(params.limit));
    return fetchJSON<JobOut[]>(`/jobs${q.toString() ? "?" + q : ""}`);
  },
  cancelJob: (jobId: string, reason?: string) =>
    fetchJSON<JobOut>(`/jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    }),

  // ---- Scheduler Config ----
  getConfig: () => fetchJSON<SchedulerConfig>("/data/config"),
  updateConfig: (body: Partial<SchedulerConfig>) =>
    fetchJSON<SchedulerConfig>("/data/config", { method: "PUT", body: JSON.stringify(body) }),

  // ---- LLM Provider Config (persisted to .env) ----
};

// ---- Data types ----
export interface DataStatus {
  symbol: string;
  period: string;
  adjust: string;
  bar_count: number;
  first_date: string | null;
  last_date: string | null;
  last_close: string | null;
  source: string | null;
}

export interface DataStatusList {
  items: DataStatus[];
  total: number;
  limit: number;
  offset: number;
}

export interface DataFetchRequest {
  symbol: string;
  start: string;
  end: string;
  adjust?: string;
  source?: string;
}

export interface FetchResult {
  symbol: string;
  bar_count: number;
  first_date: string | null;
  last_date: string | null;
  source: string | null;
  fallback_used: boolean;
  fallback_source: string | null;
  lifecycle_events: number;
  lifecycle_sync_failed: boolean;
  lifecycle_sync_error: string | null;
}

export interface BatchFetchResult {
  total: number;
  success: number;
  failed: number;
  details: FetchResult[];
}

export interface SymbolEntry {
  code: string;
  name: string;
}

export interface SymbolPool {
  symbols: SymbolEntry[];
  fetch_period: string;
  fetch_lookback_days: number;
  fetch_adjust: string;
}

export interface SymbolPoolUpdate {
  symbols: SymbolEntry[];
  fetch_period: string;
  fetch_lookback_days: number;
  fetch_adjust: string;
}

// ---- Backtest types ----
export interface StrategyParamInfo {
  name: string;
  label: string;
  type: string;
  default: StrategyParamValue;
  required: boolean;
  description: string;
  enum: StrategyParamValue[] | null;
  minimum: number | null;
  maximum: number | null;
  exclusive_minimum: number | null;
  exclusive_maximum: number | null;
  min_length: number | null;
  max_length: number | null;
  nullable: boolean;
  ui_hidden: boolean;
}

export interface StrategyInfo {
  kind: string;
  name: string;
  description: string;
  supports_backtest: boolean;
  params: StrategyParamInfo[];
}

export type StrategyParamValue = string | number | boolean | null;

export interface FactorSelectionInput {
  enabled: boolean;
  source: string;
  factor_version: string;
  max_symbols: number;
  ranking_factor:
    | "market_cap"
    | "pb"
    | "turnover_rate"
    | "momentum"
    | "roe"
    | "gross_profit_margin"
    | "revenue_yoy";
  ranking_scope: "global" | "industry";
  ranking_ascending: boolean;
  max_per_industry: number | null;
  min_listing_days: number;
  exclude_st: boolean;
  exclude_suspended: boolean;
  momentum_lookback: number;
  min_market_cap: string | null;
  max_market_cap: string | null;
  min_pb: string | null;
  max_pb: string | null;
  min_turnover_rate: string | null;
  max_turnover_rate: string | null;
  min_momentum: string | null;
  min_roe: string | null;
  min_gross_profit_margin: string | null;
  min_revenue_yoy: string | null;
  dataset_versions: Record<string, string>;
}

export const DEFAULT_FACTOR_SELECTION: FactorSelectionInput = {
  enabled: false,
  source: "tushare",
  factor_version: "v1",
  max_symbols: 20,
  ranking_factor: "market_cap",
  ranking_scope: "global",
  ranking_ascending: false,
  max_per_industry: null,
  min_listing_days: 60,
  exclude_st: true,
  exclude_suspended: true,
  momentum_lookback: 20,
  min_market_cap: null,
  max_market_cap: null,
  min_pb: null,
  max_pb: null,
  min_turnover_rate: null,
  max_turnover_rate: null,
  min_momentum: null,
  min_roe: null,
  min_gross_profit_margin: null,
  min_revenue_yoy: null,
  dataset_versions: {},
};

export interface StrategyPresetInput {
  name: string;
  strategy: string;
  params: Record<string, StrategyParamValue>;
  selection: FactorSelectionInput;
}

export interface StrategyPreset extends StrategyPresetInput {
  id: number;
  created_at: string;
  updated_at: string;
}

export interface BacktestRunRequest {
  strategy: string;
  symbols: string[];
  start: string;
  end: string;
  capital: string;
  adjust?: string;
  params: Record<string, StrategyParamValue>;
  selection: FactorSelectionInput;
  commission_rate?: string;
  commission_min?: string;
  stamp_tax_rate?: string;
  slippage_bps?: string;
}

export interface BacktestMetrics {
  total_return: number;
  annualized_return: number;
  sharpe_ratio: number;
  max_drawdown: number;
  win_rate: number;
  trade_count: number;
  turnover: number;
  commission_paid: string;
  stamp_tax_paid: string;
  benchmark_return: number;
  excess_return: number;
  initial_capital: string;
  final_equity: string;
}

export interface EquityPoint {
  date: string;
  equity: number;
  benchmark: number | null;
}

export interface BacktestFill {
  date: string;
  symbol: string;
  side: string;
  quantity: string;
  price: string;
  commission: string;
}

export interface BacktestResult {
  metrics: BacktestMetrics;
  equity_curve: EquityPoint[];
  fills: BacktestFill[];
  summary: string;
  run_id: number | null;
  selection_snapshots: FactorSnapshot[];
  dataset_versions: Record<string, string[]>;
  factor_version: string | null;
}

export interface FactorSnapshot {
  id: number | null;
  decision_at: string;
  business_date: string;
  effective_date: string;
  selected_symbols: string[];
  status: "published" | "skipped";
  skip_reason: string | null;
  dataset_versions: Record<string, string>;
  factor_version: string;
  checksum: string;
}

export interface BacktestHistoryItem {
  id: number;
  strategy: string;
  symbols: string[];
  start: string;
  end: string;
  capital: string;
  adjust: string;
  metrics: Partial<BacktestMetrics>;
  factor_version: string | null;
  created_at: string;
}

export interface BacktestHistoryDetail {
  id: number;
  strategy: string;
  symbols: string[];
  start: string;
  end: string;
  capital: string;
  adjust: string;
  params: Record<string, StrategyParamValue>;
  selection: FactorSelectionInput;
  metrics: BacktestMetrics;
  equity_curve: EquityPoint[];
  fills: BacktestFill[];
  summary: string;
  selection_snapshots: FactorSnapshot[];
  dataset_versions: Record<string, string[]>;
  factor_version: string | null;
  created_at: string;
}

// ---- Watchlist types ----
export interface Watchlist {
  id: number;
  name: string;
  description: string | null;
  item_count: number;
  created_at: string;
}

export interface WatchlistDetail extends Watchlist {
  symbols: string[];
}

// ---- Instrument types ----
export interface InstrumentItem {
  code: string;
  name: string;
  market: string;
  instrument_type: string;
  exchange: string | null;
  listing_board: string;
  status: string;
}

export interface InstrumentList {
  items: InstrumentItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface BarAnomaly {
  date: string;
  source: string;
  reasons: string[];
}

export interface QualityReport {
  symbol: string;
  total_bars: number;
  anomaly_count: number;
  duplicate_count: number;
  sources: string[];
  anomalies: BarAnomaly[];
  passed: boolean;
  primary_source: string;
  fallback_used: boolean;
  fallback_source: string | null;
  corrected_dates: string[];
  error: string | null;
}

export interface QualityRepairResult {
  total: number;
  repaired: number;
  failed: number;
  corrected_bars: number;
  reports: QualityReport[];
}

export interface ActiveSymbol {
  code: string;
  reason: string;
}

export interface BulkDownloadLog {
  seq: number;
  timestamp: string;
  event: "fetching" | "completed" | "cache_hit" | "failed";
  code: string;
  reason: string | null;
}

export interface BulkDownloadStatus {
  status: string;  // idle / running / done / error / cancelled
  done: number;
  total: number;
  success: number;
  failed: number;
  current_symbol: string | null;
  phase: string | null;
  error: string | null;
  quality_passed?: number;
  quality_failed?: number;
  fallback_used?: number;
  lifecycle_events?: number;
  lifecycle_sync_failed?: number;
  cache_hits?: number;
  cache_misses?: number;
  started_at?: string | null;
  active_symbols?: ActiveSymbol[];
  logs?: BulkDownloadLog[];
  quality_reports?: QualityReport[];
}

export interface TushareQuota {
  date: string;
  requests_per_minute: number;
  daily_limit: number;
  used: number;
  remaining: number;
}

export interface SchedulerConfig {
  sync_enabled: boolean;
  sync_time: string;
  download_enabled: boolean;
  download_time: string;
  download_lookback_days: number;
  download_markets: string[];
  download_types: string[];
  data_provider: string;
}

// ---- LLM Provider types ----
// api_key: GET 返回固定掩码 "********"(已设置时);PUT 回传 "********" 表示不改。
