import { fetchJSON } from "./api";

/* ============================================================ */
/* Simulation (Paper Trading)                                   */
/* ============================================================ */

export type SimulationSessionStatus =
  | "created"
  | "running"
  | "paused"
  | "stopped"
  | "archived";

export interface SimulationAccount {
  account_id: string;
  name: string;
  initial_cash: number;
  cash: number;
  frozen_cash: number;
  margin_used: number;
  equity: number;
  status: string;
  mode: string;
  currency: string;
  created_at: string;
}

export interface SimulationSession {
  session_id: string;
  simulation_account_id: string;
  strategy_id: string;
  strategy_version: number;
  status: SimulationSessionStatus;
  source_mode: string;
  source_run_id?: string;
  data_release_id?: string;
  validation_run_id?: string;
  clock_speed: number;
  promotion_status: string;
  reset_of_session_id?: string;
  recovery_count: number;
  config: Record<string, unknown>;
  clock: Record<string, unknown>;
  created_at: string;
}

export interface SimulationOrder {
  order_id: string;
  session_id: string;
  symbol: string;
  side: string;
  order_type: string;
  quantity: number;
  filled_quantity: number;
  price?: number;
  avg_fill_price?: number;
  status: string;
  time_in_force: string;
  created_at: string;
}

export interface SimulationFill {
  fill_id: string;
  order_id: string;
  symbol: string;
  side: string;
  quantity: number;
  price: number;
  commission: number;
  tax: number;
  timestamp: string;
}

export interface SimulationPosition {
  symbol: string;
  quantity: number;
  avg_cost: number;
  market_price: number;
  market_value: number;
  unrealized_pnl: number;
  side: string;
}

export interface SimulationLedgerEntry {
  entry_id: string;
  timestamp: string;
  event_type: string;
  cash_delta: number;
  cash_after: number;
  description: string;
}

export interface SimulationDecision {
  decision_id: string;
  source_run_id?: string;
  source_decision_id?: string;
  status: string;
  targets: {
    symbol: string;
    market: string;
    instrument_type: string;
    target_quantity: number;
    signal_trace_id?: string;
    reason?: string;
    order_type?: string;
  }[];
  created_at: string;
}

export interface SimulationReport {
  session_id: string;
  total_return: number;
  annual_return: number;
  sharpe_ratio: number;
  max_drawdown: number;
  win_rate: number;
  total_trades: number;
  equity_curve: { timestamp: string; equity: number }[];
}

export const simulationApi = {
  /* Accounts */
  createAccount: (body: { name: string; initial_cash: number; actor: string; currency?: string }) =>
    fetchJSON<SimulationAccount>(`/simulation/accounts`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listAccounts: (limit?: number) =>
    fetchJSON<SimulationAccount[]>(`/simulation/accounts${limit ? `?limit=${limit}` : ""}`),
  accountDetail: (id: string) =>
    fetchJSON<SimulationAccount>(`/simulation/accounts/${id}`),

  /* Sessions */
  createSession: (body: Record<string, unknown>) =>
    fetchJSON<SimulationSession>(`/simulation/sessions`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listSessions: (params?: { account_id?: string; status?: string[]; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.account_id) q.set("account_id", params.account_id);
    if (params?.status) params.status.forEach((s) => q.append("status", s));
    if (params?.limit) q.set("limit", String(params.limit));
    return fetchJSON<SimulationSession[]>(`/simulation/sessions${q.toString() ? "?" + q : ""}`);
  },
  sessionDetail: (id: string) =>
    fetchJSON<SimulationSession>(`/simulation/sessions/${id}`),
  startSession: (id: string, actor: string) =>
    fetchJSON<SimulationSession>(`/simulation/sessions/${id}/start`, {
      method: "POST",
      body: JSON.stringify({ actor }),
    }),
  pauseSession: (id: string, actor: string) =>
    fetchJSON<SimulationSession>(`/simulation/sessions/${id}/pause`, {
      method: "POST",
      body: JSON.stringify({ actor }),
    }),
  stopSession: (id: string, actor: string) =>
    fetchJSON<SimulationSession>(`/simulation/sessions/${id}/stop`, {
      method: "POST",
      body: JSON.stringify({ actor }),
    }),
  archiveSession: (id: string, actor: string) =>
    fetchJSON<SimulationSession>(`/simulation/sessions/${id}/archive`, {
      method: "POST",
      body: JSON.stringify({ actor }),
    }),

  /* Decisions */
  submitDecision: (sessionId: string, body: Record<string, unknown>) =>
    fetchJSON<{ decision: SimulationDecision; orders: SimulationOrder[]; duplicate: boolean }>(
      `/simulation/sessions/${sessionId}/decisions`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  listDecisions: (sessionId: string, limit?: number) =>
    fetchJSON<SimulationDecision[]>(
      `/simulation/sessions/${sessionId}/decisions${limit ? `?limit=${limit}` : ""}`,
    ),

  /* Queries */
  orders: (sessionId: string, params?: { status?: string[]; symbol?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.status) params.status.forEach((s) => q.append("status", s));
    if (params?.symbol) q.set("symbol", params.symbol);
    q.set("limit", String(params?.limit ?? 100));
    return fetchJSON<SimulationOrder[]>(
      `/simulation/sessions/${sessionId}/orders?${q}`,
    );
  },
  fills: (sessionId: string, limit?: number) =>
    fetchJSON<SimulationFill[]>(
      `/simulation/sessions/${sessionId}/fills${limit ? `?limit=${limit}` : ""}`,
    ),
  positions: (sessionId: string) =>
    fetchJSON<SimulationPosition[]>(`/simulation/sessions/${sessionId}/positions`),
  ledger: (sessionId: string, limit?: number) =>
    fetchJSON<SimulationLedgerEntry[]>(
      `/simulation/sessions/${sessionId}/ledger${limit ? `?limit=${limit}` : ""}`,
    ),
  report: (sessionId: string) =>
    fetchJSON<SimulationReport>(`/simulation/sessions/${sessionId}/report`),
  evaluate: (sessionId: string, actor: string, minimumTradingDays?: number) =>
    fetchJSON<Record<string, unknown>>(`/simulation/sessions/${sessionId}/evaluate`, {
      method: "POST",
      body: JSON.stringify({ actor, minimum_trading_days: minimumTradingDays }),
    }),
};
