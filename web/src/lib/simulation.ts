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
  total_return: number | null;
  annual_return: number | null;
  sharpe_ratio: number | null;
  max_drawdown: number | null;
  win_rate: number | null;
  total_trades: number;
  equity_curve: { timestamp: string; equity: number }[];
  initial_cash?: number | null;
  final_equity?: number | null;
  validation_run_id?: string;
  data_release_id?: string;
  reference_backtest_final_equity?: number | null;
  equity_deviation?: number | null;
  commission_paid?: number | null;
  tax_paid?: number | null;
  fill_count?: number;
  promotion_status?: string;
  automatic_live_promotion?: boolean;
}

export interface SimulationProcessResult {
  source_event_id: string;
  duplicate: boolean;
  fill_ids: string[];
  rejected_order_ids: string[];
  equity: number;
  clock_at: string;
}

type WireRecord = Record<string, unknown>;

function asString(value: unknown, fallback = ""): string {
  return value === null || value === undefined ? fallback : String(value);
}

function asNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const result = Number(value);
  return Number.isFinite(result) ? result : null;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)) : [];
}

function normalizeAccount(row: WireRecord): SimulationAccount {
  return {
    account_id: asString(row.simulation_account_id ?? row.account_id),
    name: asString(row.name),
    initial_cash: asNumber(row.initial_cash) ?? 0,
    cash: asNumber(row.cash) ?? 0,
    frozen_cash: asNumber(row.frozen_cash) ?? 0,
    margin_used: asNumber(row.margin_used) ?? 0,
    equity: asNumber(row.equity) ?? 0,
    status: asString(row.status, "unknown"),
    mode: asString(row.mode, "simulation"),
    currency: asString(row.currency, "CNY"),
    created_at: asString(row.created_at),
  };
}

function normalizeSession(row: WireRecord): SimulationSession {
  return {
    session_id: asString(row.simulation_session_id ?? row.session_id),
    simulation_account_id: asString(row.simulation_account_id),
    strategy_id: asString(row.strategy_id),
    strategy_version: asNumber(row.strategy_version) ?? 0,
    status: asString(row.status, "unknown") as SimulationSessionStatus,
    source_mode: asString(row.source_mode),
    source_run_id: asString(row.validation_run_id ?? row.source_run_id) || undefined,
    data_release_id: asString(row.data_release_id) || undefined,
    validation_run_id: asString(row.validation_run_id) || undefined,
    clock_speed: asNumber(
      row.clock_speed ?? (row.clock as WireRecord | undefined)?.speed,
    ) ?? 1,
    promotion_status: asString(row.promotion_status, "not_evaluated"),
    reset_of_session_id: asString(row.reset_of_session_id) || undefined,
    recovery_count: asNumber(row.recovery_count) ?? 0,
    config: (row.config as Record<string, unknown> | undefined) ?? {},
    clock: (row.clock as Record<string, unknown> | undefined) ?? {},
    created_at: asString(row.created_at),
  };
}

function normalizeOrder(row: WireRecord): SimulationOrder {
  return {
    order_id: asString(row.simulation_order_id ?? row.order_id),
    session_id: asString(row.simulation_session_id ?? row.session_id),
    symbol: asString(row.symbol),
    side: asString(row.side),
    order_type: asString(row.order_type),
    quantity: asNumber(row.quantity) ?? 0,
    filled_quantity: asNumber(row.filled_quantity) ?? 0,
    price: asNumber(row.price) ?? undefined,
    avg_fill_price: asNumber(row.average_fill_price ?? row.avg_fill_price) ?? undefined,
    status: asString(row.status, "unknown"),
    time_in_force: asString(row.time_in_force),
    created_at: asString(row.created_at),
  };
}

function normalizeFill(row: WireRecord): SimulationFill {
  return {
    fill_id: asString(row.simulation_fill_id ?? row.fill_id),
    order_id: asString(row.simulation_order_id ?? row.order_id),
    symbol: asString(row.symbol),
    side: asString(row.side),
    quantity: asNumber(row.quantity) ?? 0,
    price: asNumber(row.price) ?? 0,
    commission: asNumber(row.commission) ?? 0,
    tax: asNumber(row.tax) ?? 0,
    timestamp: asString(row.filled_at ?? row.timestamp),
  };
}

function normalizePosition(row: WireRecord): SimulationPosition {
  return {
    symbol: asString(row.symbol),
    quantity: asNumber(row.total_quantity ?? row.quantity) ?? 0,
    avg_cost: asNumber(row.average_price ?? row.avg_cost) ?? 0,
    market_price: asNumber(row.last_price ?? row.market_price) ?? 0,
    market_value: asNumber(row.market_value) ?? 0,
    unrealized_pnl: asNumber(row.unrealized_pnl) ?? 0,
    side: asString(row.position_side ?? row.side),
  };
}

function normalizeLedger(row: WireRecord): SimulationLedgerEntry {
  const payload = row.payload;
  const description =
    typeof payload === "object" && payload !== null
      ? JSON.stringify(payload)
      : asString(payload);
  return {
    entry_id: asString(row.ledger_id ?? row.entry_id),
    timestamp: asString(row.occurred_at ?? row.timestamp),
    event_type: asString(row.event_type),
    cash_delta: asNumber(row.cash_delta) ?? 0,
    cash_after: asNumber(row.cash_after) ?? 0,
    description,
  };
}

function normalizeReport(row: WireRecord): SimulationReport {
  return {
    session_id: asString(row.session_id),
    total_return: asNumber(row.simulation_return ?? row.total_return),
    annual_return: asNumber(row.annual_return),
    sharpe_ratio: asNumber(row.sharpe_ratio),
    max_drawdown: asNumber(row.max_drawdown),
    win_rate: asNumber(row.win_rate),
    total_trades: asNumber(row.order_count ?? row.total_trades) ?? 0,
    equity_curve: Array.isArray(row.equity_curve)
      ? row.equity_curve
          .filter((item): item is WireRecord => typeof item === "object" && item !== null)
          .map((item) => ({
            timestamp: asString(item.timestamp),
            equity: asNumber(item.equity) ?? 0,
          }))
      : [],
    initial_cash: asNumber(row.initial_cash),
    final_equity: asNumber(row.final_equity),
    validation_run_id: asString(row.validation_run_id) || undefined,
    data_release_id: asString(row.data_release_id) || undefined,
    reference_backtest_final_equity: asNumber(row.reference_backtest_final_equity),
    equity_deviation: asNumber(row.equity_deviation),
    commission_paid: asNumber(row.commission_paid),
    tax_paid: asNumber(row.tax_paid),
    fill_count: asNumber(row.fill_count) ?? undefined,
    promotion_status: asString(row.promotion_status) || undefined,
    automatic_live_promotion:
      typeof row.automatic_live_promotion === "boolean"
        ? row.automatic_live_promotion
        : undefined,
  };
}

export const simulationApi = {
  /* Accounts */
  createAccount: (body: { name: string; initial_cash: number; actor: string; currency?: string }) =>
    fetchJSON<WireRecord>(`/simulation/accounts`, {
      method: "POST",
      body: JSON.stringify(body),
    }).then(normalizeAccount),
  listAccounts: (limit?: number) =>
    fetchJSON<WireRecord[]>(`/simulation/accounts${limit ? `?limit=${limit}` : ""}`).then((rows) =>
      rows.map(normalizeAccount),
    ),
  accountDetail: (id: string) =>
    fetchJSON<WireRecord>(`/simulation/accounts/${id}`).then(normalizeAccount),

  /* Sessions */
  createSession: (body: Record<string, unknown>) =>
    fetchJSON<WireRecord>(`/simulation/sessions`, {
      method: "POST",
      body: JSON.stringify(body),
    }).then(normalizeSession),
  listSessions: (params?: { account_id?: string; status?: string[]; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.account_id) q.set("account_id", params.account_id);
    if (params?.status) params.status.forEach((s) => q.append("status", s));
    if (params?.limit) q.set("limit", String(params.limit));
    return fetchJSON<WireRecord[]>(`/simulation/sessions${q.toString() ? "?" + q : ""}`).then((rows) =>
      rows.map(normalizeSession),
    );
  },
  sessionDetail: (id: string) =>
    fetchJSON<WireRecord>(`/simulation/sessions/${id}`).then(normalizeSession),
  startSession: (id: string, actor: string) =>
    fetchJSON<WireRecord>(`/simulation/sessions/${id}/start`, {
      method: "POST",
      body: JSON.stringify({ actor }),
    }).then(normalizeSession),
  pauseSession: (id: string, actor: string) =>
    fetchJSON<WireRecord>(`/simulation/sessions/${id}/pause`, {
      method: "POST",
      body: JSON.stringify({ actor }),
    }).then(normalizeSession),
  stopSession: (id: string, actor: string) =>
    fetchJSON<WireRecord>(`/simulation/sessions/${id}/stop`, {
      method: "POST",
      body: JSON.stringify({ actor }),
    }).then(normalizeSession),
  archiveSession: (id: string, actor: string) =>
    fetchJSON<WireRecord>(`/simulation/sessions/${id}/archive`, {
      method: "POST",
      body: JSON.stringify({ actor }),
    }).then(normalizeSession),

  /* Decisions */
  submitDecision: (sessionId: string, body: Record<string, unknown>) =>
    fetchJSON<{ decision: WireRecord; orders: WireRecord[]; duplicate: boolean }>(
      `/simulation/sessions/${sessionId}/decisions`,
      { method: "POST", body: JSON.stringify(body) },
    ).then((result) => ({
      decision: result.decision as unknown as SimulationDecision,
      orders: result.orders.map(normalizeOrder),
      duplicate: result.duplicate,
    })),
  processMarketEvent: (sessionId: string, body: Record<string, unknown>) =>
    fetchJSON<WireRecord>(`/simulation/sessions/${sessionId}/market-events`, {
      method: "POST",
      body: JSON.stringify(body),
    }).then((row) => ({
      source_event_id: asString(row.source_event_id),
      duplicate: row.duplicate === true,
      fill_ids: asStringArray(row.fill_ids),
      rejected_order_ids: asStringArray(row.rejected_order_ids),
      equity: asNumber(row.equity) ?? 0,
      clock_at: asString(row.clock_at),
    })),
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
    return fetchJSON<WireRecord[]>(
      `/simulation/sessions/${sessionId}/orders?${q}`,
    ).then((rows) => rows.map(normalizeOrder));
  },
  fills: (sessionId: string, limit?: number) =>
    fetchJSON<WireRecord[]>(
      `/simulation/sessions/${sessionId}/fills${limit ? `?limit=${limit}` : ""}`,
    ).then((rows) => rows.map(normalizeFill)),
  positions: (sessionId: string) =>
    fetchJSON<WireRecord[]>(`/simulation/sessions/${sessionId}/positions`).then((rows) =>
      rows.map(normalizePosition),
    ),
  ledger: (sessionId: string, limit?: number) =>
    fetchJSON<WireRecord[]>(
      `/simulation/sessions/${sessionId}/ledger${limit ? `?limit=${limit}` : ""}`,
    ).then((rows) => rows.map(normalizeLedger)),
  report: (sessionId: string) =>
    fetchJSON<WireRecord>(`/simulation/sessions/${sessionId}/report`).then(normalizeReport),
  evaluate: (sessionId: string, actor: string, minimumTradingDays?: number) =>
    fetchJSON<Record<string, unknown>>(`/simulation/sessions/${sessionId}/evaluate`, {
      method: "POST",
      body: JSON.stringify({ actor, minimum_trading_days: minimumTradingDays }),
    }),
};
