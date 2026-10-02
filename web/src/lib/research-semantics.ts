import type { ResearchRunSummary } from "./research";

const record = (v: unknown): Record<string, unknown> =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};

/** Completed describes execution, never promotion or hypothesis support. */
export function runSchedule(run: Pick<ResearchRunSummary, "manifest" | "result">) {
  const p = record(run.manifest?.parameters);
  const s = record(p.decision_schedule);
  const kinds = ["daily", "weekly", "monthly", "quarterly", "custom"];
  const dates = Array.isArray(s.dates)
    ? s.dates.filter((d): d is string => typeof d === "string")
    : [];
  const validDates =
    dates.length > 0 &&
    dates.every(
      (d, i) =>
        /^\d{4}-\d{2}-\d{2}$/.test(d) &&
        (Number.isNaN(Date.parse(`${d}T00:00:00Z`))
          ? "invalid"
          : new Date(`${d}T00:00:00Z`).toISOString()
        ).slice(0, 10) === d &&
        (i === 0 || d > dates[i - 1]),
    );
  const conflict = p.decision_schedule != null && p.rebalance_frequency != null;
  const validSchedule =
    kinds.includes(String(s.kind)) &&
    Object.keys(s).every((k) => ["kind", "dates"].includes(k)) &&
    (s.dates == null || Array.isArray(s.dates)) &&
    (s.kind === "custom"
      ? validDates && dates.length === (s.dates as unknown[]).length
      : dates.length === 0);
  const legacy = ["daily", "weekly", "monthly", "quarterly"].includes(
    String(p.rebalance_frequency),
  );
  const reported = run.result?.execution_mode;
  const mode =
    reported === "multi_period" || reported === "single_shot"
      ? reported
      : conflict
        ? "unknown"
        : validSchedule || legacy
          ? "multi_period"
          : p.decision_schedule != null || p.rebalance_frequency != null
            ? "unknown"
            : Array.isArray(run.manifest?.factor_snapshots) &&
                run.manifest.factor_snapshots.length > 0
              ? "single_shot"
              : "unknown";
  return {
    mode,
    conflict,
    frequency: conflict
      ? "配置冲突"
      : validSchedule
        ? String(s.kind)
        : legacy
          ? String(p.rebalance_frequency)
          : "未记录",
    count: s.kind === "custom" && validDates && !conflict ? dates.length : null,
    first: s.kind === "custom" && validDates && !conflict ? dates[0] : null,
    last: s.kind === "custom" && validDates && !conflict ? dates.at(-1) : null,
  };
}

export function oosLabel(outcome: string | null | undefined, lang: "zh" | "en" = "zh") {
  const labels: Record<string, [string, string]> = {
    supported: ["OOS：假设获支持", "OOS: supported"],
    not_supported: ["OOS：假设未获支持", "OOS: not supported"],
    inconclusive: ["OOS：证据不足", "OOS: inconclusive"],
  };
  return (labels[outcome ?? ""] ?? ["OOS：结论未知", "OOS: unknown"])[lang === "zh" ? 0 : 1];
}
