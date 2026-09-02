import type { FactorSelectionInput } from "../lib/api";
import { useT } from "@/i18n";

interface Props {
  value: FactorSelectionInput;
  onChange: (value: FactorSelectionInput) => void;
}

const FACTORS: Array<{
  value: FactorSelectionInput["ranking_factor"];
  label: "marketCap" | "pb" | "turnoverRate" | "momentum" | "roe" | "grossMargin" | "revenueYoy";
}> = [
  { value: "market_cap", label: "marketCap" },
  { value: "pb", label: "pb" },
  { value: "turnover_rate", label: "turnoverRate" },
  { value: "momentum", label: "momentum" },
  { value: "roe", label: "roe" },
  { value: "gross_profit_margin", label: "grossMargin" },
  { value: "revenue_yoy", label: "revenueYoy" },
];

type DecimalKey =
  | "min_market_cap"
  | "max_market_cap"
  | "min_pb"
  | "max_pb"
  | "min_turnover_rate"
  | "max_turnover_rate"
  | "min_momentum"
  | "min_roe"
  | "min_gross_profit_margin"
  | "min_revenue_yoy";

export default function FactorSelectionForm({ value, onChange }: Props) {
  const { t } = useT();
  const set = <K extends keyof FactorSelectionInput>(
    key: K,
    next: FactorSelectionInput[K],
  ) => onChange({ ...value, [key]: next });

  const setDecimal = (key: DecimalKey, raw: string) =>
    set(key, raw.trim() === "" ? null : raw);

  return (
    <section className="mt-4 border-t border-border pt-4" aria-labelledby="factor-selection-title">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 id="factor-selection-title" className="text-sm font-semibold text-foreground">
            {t("factorSelection.title")}
          </h3>
          <p className="mt-1 max-w-2xl text-xs leading-5 text-muted-foreground">
            {t("factorSelection.description")}
          </p>
        </div>
        <label className="inline-flex cursor-pointer items-center gap-2 text-sm font-medium text-foreground">
          <input
            type="checkbox"
            checked={value.enabled}
            onChange={(event) => set("enabled", event.target.checked)}
            className="h-4 w-4 rounded border-border text-primary focus:ring-ring"
          />
          {t("factorSelection.enable")}
        </label>
      </div>

      {!value.enabled ? (
        <p className="mt-3 rounded-md bg-muted/50 px-3 py-2 text-xs text-muted-foreground">
          {t("factorSelection.disabledHint")}
        </p>
      ) : (
        <>
          <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Field label={t("factorSelection.source")}>
              <select
                value={value.source}
                onChange={(event) => set("source", event.target.value)}
                className={controlClass}
              >
                <option value="tushare">{t("factorSelection.sourceTushare")}</option>
              </select>
            </Field>
            <Field label={t("factorSelection.rankingFactor")}>
              <select
                value={value.ranking_factor}
                onChange={(event) =>
                  set(
                    "ranking_factor",
                    event.target.value as FactorSelectionInput["ranking_factor"],
                  )
                }
                className={controlClass}
              >
                {FACTORS.map((factor) => (
                  <option key={factor.value} value={factor.value}>
                    {t(`factorSelection.factors.${factor.label}`)}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t("factorSelection.rankingScope")}>
              <select
                value={value.ranking_scope}
                onChange={(event) =>
                  set(
                    "ranking_scope",
                    event.target.value as FactorSelectionInput["ranking_scope"],
                  )
                }
                className={controlClass}
              >
                <option value="global">{t("factorSelection.scopeGlobal")}</option>
                <option value="industry">{t("factorSelection.scopeIndustry")}</option>
              </select>
            </Field>
            <Field label={t("factorSelection.maxSymbols")}>
              <input
                type="number"
                min={1}
                max={5000}
                value={value.max_symbols}
                onChange={(event) => set("max_symbols", Number(event.target.value))}
                className={controlClass}
              />
            </Field>
          </div>

          <div className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-sm text-foreground">
            <Check
              label={t("factorSelection.ascending")}
              checked={value.ranking_ascending}
              onChange={(checked) => set("ranking_ascending", checked)}
            />
            <Check
              label={t("factorSelection.excludeSt")}
              checked={value.exclude_st}
              onChange={(checked) => set("exclude_st", checked)}
            />
            <Check
              label={t("factorSelection.excludeSuspended")}
              checked={value.exclude_suspended}
              onChange={(checked) => set("exclude_suspended", checked)}
            />
          </div>

          <details className="mt-4 rounded-lg border border-border">
            <summary className="cursor-pointer px-3 py-2 text-sm font-medium text-foreground">
              {t("factorSelection.advanced")}
            </summary>
            <div className="grid grid-cols-1 gap-4 border-t border-border p-3 sm:grid-cols-2 xl:grid-cols-4">
              <NumberField
                label={t("factorSelection.minListingDays")}
                value={value.min_listing_days}
                min={0}
                onChange={(next) => set("min_listing_days", next)}
              />
              <OptionalNumberField
                label={t("factorSelection.maxPerIndustry")}
                value={value.max_per_industry}
                min={1}
                onChange={(next) => set("max_per_industry", next)}
              />
              <NumberField
                label={t("factorSelection.momentumLookback")}
                value={value.momentum_lookback}
                min={1}
                onChange={(next) => set("momentum_lookback", next)}
              />
              <DecimalField label={t("factorSelection.minMarketCap")} value={value.min_market_cap} onChange={(raw) => setDecimal("min_market_cap", raw)} />
              <DecimalField label={t("factorSelection.maxMarketCap")} value={value.max_market_cap} onChange={(raw) => setDecimal("max_market_cap", raw)} />
              <DecimalField label={t("factorSelection.minPb")} value={value.min_pb} onChange={(raw) => setDecimal("min_pb", raw)} />
              <DecimalField label={t("factorSelection.maxPb")} value={value.max_pb} onChange={(raw) => setDecimal("max_pb", raw)} />
              <DecimalField label={t("factorSelection.minTurnoverRate")} value={value.min_turnover_rate} onChange={(raw) => setDecimal("min_turnover_rate", raw)} />
              <DecimalField label={t("factorSelection.maxTurnoverRate")} value={value.max_turnover_rate} onChange={(raw) => setDecimal("max_turnover_rate", raw)} />
              <DecimalField label={t("factorSelection.minMomentum")} value={value.min_momentum} onChange={(raw) => setDecimal("min_momentum", raw)} />
              <DecimalField label={t("factorSelection.minRoe")} value={value.min_roe} onChange={(raw) => setDecimal("min_roe", raw)} />
              <DecimalField label={t("factorSelection.minGrossMargin")} value={value.min_gross_profit_margin} onChange={(raw) => setDecimal("min_gross_profit_margin", raw)} />
              <DecimalField label={t("factorSelection.minRevenueYoy")} value={value.min_revenue_yoy} onChange={(raw) => setDecimal("min_revenue_yoy", raw)} />
            </div>
          </details>

          <p className="mt-3 text-xs text-muted-foreground">
            {t("factorSelection.skipHint")}
          </p>
        </>
      )}
    </section>
  );
}

const controlClass =
  "mt-1 w-full rounded-md border border-border px-3 py-2 text-sm text-foreground focus:border-ring focus:outline-none focus:ring-1 focus:ring-ring";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="text-xs font-medium text-muted-foreground">
      {label}
      {children}
    </label>
  );
}

function Check({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="inline-flex cursor-pointer items-center gap-2">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="h-4 w-4 rounded border-border text-primary focus:ring-ring"
      />
      {label}
    </label>
  );
}

function NumberField({
  label,
  value,
  min,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  onChange: (value: number) => void;
}) {
  return (
    <Field label={label}>
      <input
        type="number"
        min={min}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className={controlClass}
      />
    </Field>
  );
}

function OptionalNumberField({
  label,
  value,
  min,
  onChange,
}: {
  label: string;
  value: number | null;
  min: number;
  onChange: (value: number | null) => void;
}) {
  const { t } = useT();
  return (
    <Field label={label}>
      <input
        type="number"
        min={min}
        value={value ?? ""}
        placeholder={t("factorSelection.unlimited")}
        onChange={(event) =>
          onChange(event.target.value === "" ? null : Number(event.target.value))
        }
        className={controlClass}
      />
    </Field>
  );
}

function DecimalField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string | null;
  onChange: (value: string) => void;
}) {
  const { t } = useT();
  return (
    <Field label={label}>
      <input
        type="number"
        step="any"
        value={value ?? ""}
        placeholder={t("factorSelection.unlimited")}
        onChange={(event) => onChange(event.target.value)}
        className={controlClass}
      />
    </Field>
  );
}
