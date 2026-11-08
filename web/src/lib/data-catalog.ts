/**
 * 全局数据目录只读客户端(issue #509 后端契约,issue #512 页面消费)。
 *
 * 目录只读:所有写入口仍是 dataset_sync / bulk_download / dataset_publish;
 * 百分比与分母一律取服务端字段,前端不得自算(#509 覆盖口径)。
 */
import { fetchJSON } from "@/lib/api";

/** 目录状态(与后端 CATALOG_STATUSES 逐字对齐)。 */
export type CatalogStatus =
  | "usable"
  | "ingested_not_published"
  | "blocked"
  | "not_ingested"
  | "stale"
  | "unknown";

export const CATALOG_STATUSES: readonly CatalogStatus[] = [
  "usable",
  "ingested_not_published",
  "blocked",
  "not_ingested",
  "stale",
  "unknown",
];

export interface CatalogRelease {
  release_id: string;
  dataset_name: string;
  source: string;
  version: string;
  dataset_kind: string;
  schema_version: string;
  start_date: string;
  end_date: string;
  adjustment: string;
  symbol_count: number;
  row_count: number;
  coverage_pct: number;
  quality_status: string;
  published_at: string;
  release_checksum: string;
  storage_uri: string;
  capabilities: Record<string, unknown>[];
}

export interface CatalogBatch {
  id: number;
  dataset: string;
  source: string;
  dataset_version: string;
  status: string;
  quality_status: string | null;
  expected_rows: number | null;
  received_rows: number;
  accepted_rows: number;
  rejected_rows: number;
  parameters: Record<string, unknown>;
  error_summary: string | null;
  started_at: string;
  completed_at: string | null;
  published_at: string | null;
}

export interface CatalogEntry {
  key: string;
  title: string;
  title_en: string;
  storage: string;
  publishable: boolean;
  source: string | null;
  status: CatalogStatus;
  release: CatalogRelease | null;
  batch: CatalogBatch | null;
  coverage_pct: number | null;
  symbol_count: number | null;
  row_count: number | null;
  denominator: string;
  local_start: string | null;
  local_end: string | null;
  updated_at: string | null;
  cache_files: number;
  cache_bytes: number;
  warnings: string[];
}

export interface CatalogPage {
  items: CatalogEntry[];
  total: number;
  limit: number;
  offset: number;
  generated_at: string;
}

export interface DatasetDetail {
  entry: CatalogEntry;
  fields: string[];
  unit_notes: string[];
  pit_rule: string;
  calendar_market: string;
  adjustment: string | null;
  publishable: boolean;
  releases: CatalogRelease[];
  batches: CatalogBatch[];
  failures: CatalogBatch[];
}

export interface InstrumentCoverage {
  code: string;
  name: string;
  market: string;
  instrument_type: string;
  asset_class: string;
  exchange: string | null;
  listing_board: string;
  start_date: string;
  end_date: string;
  row_count: number;
  expected_sessions: number;
  missing_sessions: number;
  suspended_sessions: number;
  anomaly_count: number;
  duplicate_count: number;
  coverage_pct: number;
  ready: boolean;
  issues: string[];
  list_date: string | null;
  delist_date: string | null;
  status: string;
}

export interface CoverageReport {
  dataset: string;
  denominator: string;
  release: CatalogRelease | null;
  batch: CatalogBatch | null;
  coverage_pct: number | null;
  expected_sessions: number;
  missing_sessions: number;
  symbols_with_gaps: number;
  missing_preview: InstrumentCoverage[];
  warnings: string[];
}

export interface InstrumentPage {
  release_id: string;
  items: InstrumentCoverage[];
  total: number;
  limit: number;
  offset: number;
}

export interface PresencePage {
  code: string;
  items: {
    release_id: string;
    dataset_kind: string;
    version: string;
    quality_status: string;
    published_at: string;
    start_date: string;
    end_date: string;
    coverage_pct: number;
    ready: boolean;
  }[];
  total: number;
  limit: number;
  offset: number;
  warnings: string[];
}

export interface ReleaseReference {
  kind: string;
  ref_id: string;
  status: string;
  created_at: string | null;
  deep_link: string | null;
}

export interface ReferencePage {
  release_id: string;
  items: ReleaseReference[];
}

export interface CatalogCalendar {
  market: string;
  start: string;
  end: string;
  days: string[];
  source: string;
}

/** 课题数据视图(issue #512):服务端解析引用并复用 #509 覆盖口径。 */
export interface TopicRelease {
  release_id: string;
  dataset: string;
  dataset_name: string;
  source: string;
  version: string;
  start_date: string;
  end_date: string;
  published_at: string;
  quality_status: string;
  coverage_pct: number | null;
  expected_sessions: number;
  missing_sessions: number;
  symbols_with_gaps: number;
  denominator: string;
  warnings: string[];
}

export interface TopicGap {
  release_id: string;
  dataset: string;
  /** 稳定原因码:release_not_found / run_not_found / dataset_not_published / ... */
  reason: string;
  message: string;
  coverage_pct: number | null;
  missing_sessions: number;
  symbols_with_gaps: number;
}

export interface TopicCoverage {
  topic_id: string;
  releases: TopicRelease[];
  coverage: {
    release_count: number;
    dataset_count: number;
    min_coverage_pct: number | null;
    missing_sessions: number;
    symbols_with_gaps: number;
    denominators: string[];
    /** data_scope(显式范围)或 refs(轮次引用推导)。 */
    resolved_by: "data_scope" | "refs" | string;
    warnings: string[];
  };
  gaps: TopicGap[];
  last_sync: string | null;
  /** 下一步动作码:ready / fix_gaps / blocked / no_refs。 */
  next_step: "ready" | "fix_gaps" | "blocked" | "no_refs" | string;
}

function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  return search.toString();
}

export const catalogApi = {
  list: (params?: {
    dataset?: string;
    status?: string;
    q?: string;
    source?: string;
    asset_class?: string;
    start?: string;
    end?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = query(params ?? {});
    return fetchJSON<CatalogPage>(`/data/catalog${qs ? `?${qs}` : ""}`);
  },
  dataset: (key: string) =>
    fetchJSON<DatasetDetail>(`/data/catalog/datasets/${encodeURIComponent(key)}`),
  coverage: (key: string, releaseId?: string) => {
    const qs = query({ release_id: releaseId });
    return fetchJSON<CoverageReport>(
      `/data/catalog/datasets/${encodeURIComponent(key)}/coverage${qs ? `?${qs}` : ""}`,
    );
  },
  release: (releaseId: string) =>
    fetchJSON<CatalogRelease>(`/data/catalog/releases/${encodeURIComponent(releaseId)}`),
  releaseInstruments: (
    releaseId: string,
    params?: {
      ready?: boolean;
      missing_only?: boolean;
      market?: string;
      instrument_type?: string;
      q?: string;
      limit?: number;
      offset?: number;
    },
  ) => {
    const qs = query(params ?? {});
    return fetchJSON<InstrumentPage>(
      `/data/catalog/releases/${encodeURIComponent(releaseId)}/instruments${qs ? `?${qs}` : ""}`,
    );
  },
  references: (releaseId: string, limit = 20) => {
    const qs = query({ limit });
    return fetchJSON<ReferencePage>(
      `/data/catalog/releases/${encodeURIComponent(releaseId)}/references${qs ? `?${qs}` : ""}`,
    );
  },
  calendar: (market: string, start: string, end: string) => {
    const qs = query({ market, start, end });
    return fetchJSON<CatalogCalendar>(`/data/catalog/calendar?${qs}`);
  },
  topicCoverage: (topicId: string) =>
    fetchJSON<TopicCoverage>(`/data/catalog/topics/${encodeURIComponent(topicId)}/coverage`),
};

/** 目录数据集 key → 中文/英文标题(仅用于过滤下拉兜底,标题仍以响应为准)。 */
export const CATALOG_DATASET_LABELS: Record<string, { zh: string; en: string }> = {
  bars: { zh: "行情 bars", en: "Market bars" },
  daily_metrics: { zh: "每日指标", en: "Daily metrics" },
  financial_indicators: { zh: "财务指标", en: "Financial indicators" },
  convertible_metrics: { zh: "可转债指标", en: "Convertible metrics" },
  income_statements: { zh: "利润表", en: "Income statements" },
  balance_sheets: { zh: "资产负债表", en: "Balance sheets" },
  cashflow_statements: { zh: "现金流量表", en: "Cashflow statements" },
  dividends: { zh: "分红送股", en: "Dividends" },
  profiles: { zh: "标的档案", en: "Instrument profiles" },
  suspensions: { zh: "停复牌", en: "Suspensions" },
};
