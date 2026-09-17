import { fetchJSON } from "./api";

/* ============================================================ */
/* Portfolio Construction                                       */
/* ============================================================ */

export type AllocationMethod = "equal_weight" | "inverse_volatility" | "erc";

export interface AllocateRequest {
  signals: { symbol: string; score: number; direction?: "long" | "short" }[];
  method: AllocationMethod;
  as_of?: string;
  max_weight_per_asset?: number;
  max_weight_per_sleeve?: number;
  min_cash_buffer?: number;
  max_leverage?: number;
  long_only?: boolean;
  target_volatility?: number;
  max_risk_contribution?: number;
  returns_by_ticker?: Record<string, number[]>;
  sleeve_map?: Record<string, string>;
  betas?: Record<string, number>;
  conflict_policy?: string;
}

export interface RiskReport {
  portfolio_volatility?: number;
  diversification_ratio?: number;
  risk_contributions?: { symbol: string; contribution: number; weight: number }[];
  var_95?: number;
  cvar_95?: number;
  max_drawdown?: number;
  sharpe_ratio?: number;
}

export interface AllocateResponse {
  weights: { symbol: string; weight: number; signal_score?: number }[];
  weights_before_constraints?: { symbol: string; weight: number }[];
  adjustments?: { symbol: string; from: number; to: number; reason: string }[];
  risk?: RiskReport;
}

export interface SizingRequest {
  weights: { symbol: string; weight: number }[];
  capital: number;
  lot_info: { symbol: string; lot_size: number; price: number }[];
  prices: Record<string, number>;
  commission_rate?: number;
  stamp_tax_rate?: number;
}

export interface SizingResponse {
  trades: { symbol: string; lots: number; shares: number; notional: number; side: "buy" | "sell" }[];
  cash_before: number;
  cash_after: number;
  est_commission: number;
  est_tax: number;
  est_slippage: number;
  margin_required: number;
}

export interface TierFeasibility {
  tier: string;
  capital: number;
  feasible: boolean;
  tracking_error: number;
  unfillable_symbols: string[];
  capacity_pressure: number;
  margin_required: number;
}

export interface AttributionResult {
  by_asset: { symbol: string; return_contribution: number; risk_contribution: number }[];
  by_sleeve?: { sleeve: string; return_contribution: number; risk_contribution: number }[];
  total_return: number;
  total_risk: number;
  turnover: number;
  max_drawdown: number;
  leverage_ratio: number;
}

export const portfolioApi = {
  allocate: (body: AllocateRequest) =>
    fetchJSON<AllocateResponse>(`/portfolio/allocate`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  sizing: (body: SizingRequest) =>
    fetchJSON<SizingResponse>(`/portfolio/sizing`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  feasibility: (body: Record<string, unknown>) =>
    fetchJSON<TierFeasibility[]>(`/portfolio/feasibility`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  attribution: (body: Record<string, unknown>) =>
    fetchJSON<AttributionResult>(`/portfolio/attribution`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
};
