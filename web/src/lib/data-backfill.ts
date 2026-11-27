import { fetchJSON } from "@/lib/api";

export interface BackfillSpec {
  scope: {
    universe: { explicit_symbols: string[]; markets: string[] };
    decision: { start: string; end: string };
    benchmarks: string[];
    datasets: { required: string[] };
    warmup_sessions: number;
    tail_sessions: number;
    bars_adjustment: "qfq";
  };
  datasets: { dataset: string; source: string | null; adjustment: "qfq" | "none"; publish: boolean }[];
  quality_profile: "standard" | "strict";
}

interface Blocker { code: string; message: string; dataset?: string | null; }
interface PublishPreview { release_id: string; version: string; }
interface PreviewItem {
  dataset: string;
  source: string;
  adjustment: string;
  action: string;
  missing_ranges: [string, string][];
  estimated_requests: number;
  blockers: Blocker[];
  publish_preview: PublishPreview | null;
}
interface Plan {
  plan_checksum: string;
  items: PreviewItem[];
  quota: { status: string; estimated_total: number; remaining_today: number | null };
  blockers: Blocker[];
  warnings: string[];
  overlaps: { request_id: string; reuse: string }[];
  publish_impact: { new_releases: number; affected_specs: number };
}
export interface BackfillReceipt {
  request_id: string;
  job_id: string | null;
  status: string;
  reused: boolean;
}
interface BackfillDetail extends BackfillReceipt {
  release_ids: string[];
  error_summary: string | null;
  items: { id: number; dataset: string; status: string; release_id: string | null; error_summary: string | null }[];
}

export const backfillApi = {
  preview: (spec: BackfillSpec) => fetchJSON<Plan>("/data/backfills/preview", {
    method: "POST", body: JSON.stringify({ spec }),
  }),
  submit: (spec: BackfillSpec, checksum: string, idempotencyKey: string) => fetchJSON<BackfillReceipt>("/data/backfills", {
    method: "POST", body: JSON.stringify({ spec, expected_plan_checksum: checksum, idempotency_key: idempotencyKey }),
  }),
  retry: (id: string) => fetchJSON<BackfillReceipt>(`/data/backfills/${encodeURIComponent(id)}/retry`, {
    method: "POST", body: JSON.stringify({ idempotency_key: `web-retry-${id}` }),
  }),
  get: (id: string) => fetchJSON<BackfillDetail>(`/data/backfills/${encodeURIComponent(id)}`),
};
