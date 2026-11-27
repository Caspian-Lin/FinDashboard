import { useId, useState, type ReactNode } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { BlockedState, ErrorState, LoadingState } from "@/components/ui/states";
import { useT } from "@/i18n";
import { ApiError } from "@/lib/api";
import {
  factorLabApi,
  featureSnapshotNames,
  researchRunApi,
  strategySpecApi,
  type ResearchRunQueueIn,
} from "@/lib/research";

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === "string") : [];
}

function visible(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : "—";
}

function mapRows(record: Record<string, unknown>): string {
  return Object.entries(record)
    .filter(([, value]) => typeof value === "number" || typeof value === "string" || typeof value === "boolean")
    .map(([key, value]) => `${key} ${String(value)}`)
    .join(" · ");
}

function createRunOperationId() {
  const token = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `web-run-${token}`;
}

interface ResearchRunSetupProps {
  initialVersion?: { strategyId: string; version: number };
  onStart: (payload: ResearchRunQueueIn) => void;
  onCancel: () => void;
  onRepreview: () => void;
  pending: boolean;
  error?: Error | null;
}

export default function ResearchRunSetup({ onStart, onCancel, onRepreview, pending, error, initialVersion }: ResearchRunSetupProps) {
  const { tl } = useT();
  const fields = useId();
  const [strategyKey, setStrategyKey] = useState(initialVersion ? `${initialVersion.strategyId}@${initialVersion.version}` : "");
  const [snapshotIds, setSnapshotIds] = useState<string[]>([]);
  const [seriesIds, setSeriesIds] = useState<string[]>([]);
  const [capitalInput, setCapitalInput] = useState("100000");
  const [schedule, setSchedule] = useState("");
  const [customWindow, setCustomWindow] = useState(false);
  const [windowStart, setWindowStart] = useState("");
  const [windowEnd, setWindowEnd] = useState("");
  const [warmup, setWarmup] = useState("0");
  const [tail, setTail] = useState("1");
  const [maxRisk, setMaxRisk] = useState("");
  const [commission, setCommission] = useState("");
  const [slippage, setSlippage] = useState("");
  const [idempotencyKey] = useState(createRunOperationId);
  const strategies = useQuery({ queryKey: ["spec-list", "run-setup"], queryFn: () => strategySpecApi.list(100, true) });
  const listed = strategies.data?.filter((item) => item.published) ?? [];
  const inList = listed.some((item) => item.strategy_id === initialVersion?.strategyId && item.version === initialVersion.version);
  const linked = useQuery({
    queryKey: ["spec-version", initialVersion?.strategyId, initialVersion?.version],
    queryFn: () => strategySpecApi.version(initialVersion!.strategyId, initialVersion!.version),
    enabled: !!initialVersion && !inList,
  });
  const published = linked.data?.published && !inList ? [linked.data, ...listed] : listed;
  const selected = published.find((item) => `${item.strategy_id}@${item.version}` === strategyKey);
  const validation = object(selected?.spec.validation_plan);
  const releaseIds = strings(validation.dataset_release_ids);
  const nodes = object(selected?.spec.feature_graph).nodes;
  const factorSources = (Array.isArray(nodes) ? nodes : [])
    .map(object)
    .filter((node) => node.kind === "factor" || node.kind === "risk_factor")
    .flatMap((node) => typeof node.source === "string" ? [node.source] : []);
  const usesFactors = Boolean(selected && (factorSources.length || selected.strategy_kind === "user_code"));

  const snapshots = useQuery({
    queryKey: ["feature-snapshots", "run-setup", releaseIds],
    queryFn: async () => {
      const lists = await Promise.all(releaseIds.map((id) => factorLabApi.features(id, 100)));
      return [...new Map(lists.flat().map((item) => [item.snapshot_id, item])).values()];
    },
    enabled: usesFactors && releaseIds.length > 0,
  });
  const series = useQuery({
    queryKey: ["factor-series-options", releaseIds],
    queryFn: () => researchRunApi.factorSeriesOptions(releaseIds),
    enabled: usesFactors && releaseIds.length > 0,
  });
  const preview = useMutation({ mutationFn: researchRunApi.preview });
  const capital = Number(capitalInput);
  const valid = Number.isFinite(capital) && capital >= 100000 && capital <= 500000
    && (!customWindow || Boolean(windowStart && windowEnd && windowStart <= windowEnd
      && Number.isInteger(Number(warmup)) && Number(warmup) >= 0 && Number(warmup) <= 5000
      && Number.isInteger(Number(tail)) && Number(tail) >= 0 && Number(tail) <= 250))
    && (!maxRisk || Number(maxRisk) > 0 && Number(maxRisk) <= 1)
    && (!commission || Number(commission) >= 0 && Number(commission) <= 1000)
    && (!slippage || Number(slippage) >= 0 && Number(slippage) <= 10000);
  const payload: ResearchRunQueueIn | null = selected && releaseIds.length && valid ? {
    idempotency_key: idempotencyKey,
    strategy_id: selected.strategy_id,
    strategy_version: selected.version,
    dataset_release_ids: releaseIds,
    factor_snapshot_ids: snapshotIds,
    factor_series_ids: seriesIds,
    parameters: {
      ...(schedule ? { decision_schedule: { kind: schedule } } : {}),
      ...(commission || slippage ? { fee_policy_version: "explicit_overrides_v1" } : {}),
    },
    validation_config: {},
    portfolio_config: maxRisk ? { overrides: { max_risk_contribution: Number(maxRisk) } } : {},
    risk_config: {},
    execution_config: {},
    fee_config: commission || slippage ? { overrides: {
      ...(commission ? { commission_rate: Number(commission) / 10_000 } : {}),
      ...(slippage ? { slippage_bps: Number(slippage) } : {}),
    } } : {},
    benchmark_config: {},
    code_version: "web-ui-v1",
    initial_capital: capital,
    requested_by: "user:web",
  } : null;
  const previewPayload = payload ? {
    ...payload,
    ...(customWindow && { preview_window: {
      start: windowStart, end: windowEnd, warmup_sessions: Number(warmup), tail_sessions: Number(tail),
    } }),
  } : null;
  const currentPreview = previewPayload && JSON.stringify(preview.variables) === JSON.stringify(previewPayload);
  const resolved = currentPreview ? preview.data : undefined;
  const previewRejected = error instanceof ApiError && error.status === 409;
  const manifest = object(resolved?.manifest);
  const parameters = object(manifest.parameters);
  const scopedWindow = object(parameters.research_window);
  const frozenSpec = object(manifest.strategy_spec);
  const frozenPlan = object(frozenSpec.validation_plan);
  const effectivePortfolio = {
    ...object(frozenSpec.portfolio_policy),
    ...object(object(manifest.portfolio_config).overrides),
  };
  const effectiveFees = {
    ...object(frozenSpec.execution_model),
    ...object(object(manifest.fee_config).overrides),
  };

  function optionField(label: string, input: ReactNode) {
    return <label className="grid gap-1 text-sm">{label}{input}</label>;
  }

  function toggle(values: string[], id: string) {
    return values.includes(id) ? values.filter((value) => id !== value) : [...values, id];
  }

  return (
    <form className="space-y-4" onSubmit={(event) => {
      event.preventDefault();
      if (payload && resolved && !pending && !preview.isPending && !previewRejected) {
        const frozenScope = object(manifest.data_scope);
        onStart({
          ...payload,
          ...(typeof frozenScope.resolution_id === "string" && {
            data_scope: object(frozenScope.scope),
            data_scope_resolution_id: frozenScope.resolution_id,
          }),
          expected_manifest_checksum: resolved.manifest_checksum,
        });
      }
    }}>
      <div className="space-y-2">
        <Label htmlFor={`${fields}-strategy`}>{tl({ zh: "已发布策略版本", en: "Published strategy version" })}</Label>
        {(strategies.isPending || linked.isLoading) && <LoadingState rows={1} />}
        {linked.isError && <ErrorState message={linked.error.message} onRetry={() => void linked.refetch()} />}
        {strategies.isError && <ErrorState onRetry={() => void strategies.refetch()} />}
        {strategies.isSuccess && !published.length && (
          <BlockedState title={tl({ zh: "请先发布策略版本", en: "Publish a strategy version first" })} />
        )}
        <select
          id={`${fields}-strategy`}
          value={strategyKey}
          disabled={pending || !published.length}
          onChange={(event) => {
            setStrategyKey(event.target.value);
            setSnapshotIds([]);
            setSeriesIds([]);
            setCustomWindow(false);
          }}
          className="h-11 w-full rounded-md border border-input bg-background px-3 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
        >
          <option value="">{tl({ zh: "选择策略版本", en: "Choose a strategy version" })}</option>
          {published.map((item) => <option key={`${item.strategy_id}@${item.version}`} value={`${item.strategy_id}@${item.version}`}>
            {item.name || item.strategy_id} · v{item.version}
          </option>)}
        </select>
        <Link className="inline-flex min-h-8 items-center text-sm text-primary underline" to="/research/strategy">
          {tl({ zh: "打开策略 Studio", en: "Open Strategy Studio" })}
        </Link>
      </div>
      {selected && (
        <section aria-label={tl({ zh: "冻结数据与窗口", en: "Frozen data and window" })} className="divide-y rounded-lg border bg-card text-sm">
          <div className="flex flex-wrap items-baseline gap-2 p-3">
            <span className="font-medium">{tl({ zh: "冻结发布", en: "Frozen releases" })}</span>
            {releaseIds.map((id) => (
              <Link key={id} to={`/data?tab=datasets&release=${encodeURIComponent(id)}`} className="break-all text-primary underline">{id}</Link>
            ))}
            {!releaseIds.length && <span className="text-warning">{tl({ zh: "策略缺少冻结发布", en: "The strategy has no frozen release" })}</span>}
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-1 p-3 text-muted-foreground">
            <span>{tl({ zh: "策略测试窗", en: "Strategy test window" })} · {visible(validation.test_start)} – {visible(validation.test_end)}</span>
            <span>{tl({ zh: "基准", en: "Benchmark" })} · {visible(validation.benchmark_symbol)}</span>
          </div>
        </section>
      )}
      {selected && (
        <details className="rounded-lg border bg-card px-3 py-2">
          <summary className="min-h-8 cursor-pointer py-1 text-sm font-medium">{tl({ zh: "研究数据范围", en: "Research data scope" })}</summary>
          <label className="flex min-h-10 items-center gap-2 text-sm">
            <input type="checkbox" checked={customWindow} disabled={pending} onChange={(event) => setCustomWindow(event.target.checked)} />
            {tl({ zh: "在上述冻结发布中选择决策日期", en: "Choose decision dates within the frozen releases" })}
          </label>
          {customWindow && (
            <div className="grid gap-3 sm:grid-cols-2">
              {optionField(tl({ zh: "决策开始日期", en: "Decision start" }),
                <Input type="date" value={windowStart} onChange={(event) => setWindowStart(event.target.value)} disabled={pending} />)}
              {optionField(tl({ zh: "决策结束日期", en: "Decision end" }),
                <Input type="date" value={windowEnd} onChange={(event) => setWindowEnd(event.target.value)} disabled={pending} />)}
              {optionField(tl({ zh: "预热交易日数", en: "Warmup sessions" }),
                <Input type="number" min={0} max={5000} value={warmup} onChange={(event) => setWarmup(event.target.value)} disabled={pending} />)}
              {optionField(tl({ zh: "尾部估值交易日数", en: "Valuation tail sessions" }),
                <Input type="number" min={0} max={250} value={tail} onChange={(event) => setTail(event.target.value)} disabled={pending} />)}
            </div>
          )}
        </details>
      )}
      {usesFactors && (
        <details className="rounded-lg border bg-card px-3 py-2">
          <summary className="min-h-8 cursor-pointer py-1 text-sm font-medium">
            {tl({ zh: "选择因子快照或序列", en: "Choose factor snapshots or series" })} ({snapshotIds.length + seriesIds.length})
          </summary>
          {(snapshots.isLoading || series.isLoading) && <LoadingState rows={2} />}
          {(snapshots.isError || series.isError) && <ErrorState onRetry={() => {
            if (snapshots.isError) void snapshots.refetch();
            if (series.isError) void series.refetch();
          }} />}
          <div className="max-h-64 divide-y overflow-y-auto">
            {snapshots.data?.map((item) => (
              <label key={item.snapshot_id} className="flex min-h-11 items-center gap-2 py-2 text-sm">
                <input type="checkbox" disabled={pending} checked={snapshotIds.includes(item.snapshot_id)}
                  onChange={() => setSnapshotIds(toggle(snapshotIds, item.snapshot_id))} />
                <span className="break-words">{featureSnapshotNames(item).join("、") || tl({ zh: "因子快照", en: "Factor snapshot" })} · {item.snapshot_id}</span>
              </label>
            ))}
            {series.data?.map((item) => (
              <label key={item.series_id} className="flex min-h-11 items-center gap-2 py-2 text-sm">
                <input type="checkbox" disabled={pending} checked={seriesIds.includes(item.series_id)}
                  onChange={() => setSeriesIds(toggle(seriesIds, item.series_id))} />
                <span className="break-words">{item.code_artifact} · {item.window_start} – {item.window_end} · {item.code_commit.slice(0, 8)}</span>
              </label>
            ))}
          </div>
          {snapshots.isSuccess && series.isSuccess && !snapshots.data.length && !series.data.length && (
            <p className="text-sm text-muted-foreground">{tl({ zh: "当前发布没有可选的因子快照或序列。", en: "This release has no selectable factor snapshot or series." })}</p>
          )}
        </details>
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        {optionField(tl({ zh: "初始资金（元）", en: "Initial capital (CNY)" }),
          <Input type="number" min={100000} max={500000} value={capitalInput} onChange={(event) => setCapitalInput(event.target.value)} disabled={pending} />)}
        {optionField(tl({ zh: "决策频率", en: "Decision schedule" }),
          <select className="h-9 rounded-md border border-input bg-background px-3" value={schedule} onChange={(event) => setSchedule(event.target.value)} disabled={pending}>
            <option value="">{tl({ zh: "按快照时点执行", en: "Use snapshot decision time" })}</option>
            <option value="daily">{tl({ zh: "日度", en: "Daily" })}</option>
            <option value="weekly">{tl({ zh: "周度", en: "Weekly" })}</option>
            <option value="monthly">{tl({ zh: "月度", en: "Monthly" })}</option>
            <option value="quarterly">{tl({ zh: "季度", en: "Quarterly" })}</option>
          </select>)}
      </div>
      <details className="rounded-lg border bg-card px-3 py-2">
        <summary className="min-h-8 cursor-pointer py-1 text-sm font-medium">{tl({ zh: "组合约束和费用覆盖", en: "Portfolio constraints and fee overrides" })}</summary>
        <p className="mb-2 text-xs text-muted-foreground">{tl({ zh: "留空会使用冻结策略和资产的既有规则。", en: "Leave fields empty to use the frozen strategy and asset rules." })}</p>
        <div className="grid gap-3 sm:grid-cols-3">
          {optionField(tl({ zh: "风险贡献上限（比例）", en: "Risk contribution cap (ratio)" }),
            <Input type="number" min="0.001" max="1" step="any" value={maxRisk} onChange={(event) => setMaxRisk(event.target.value)} disabled={pending} />)}
          {optionField(tl({ zh: "佣金（基点）", en: "Commission (bps)" }),
            <Input type="number" min="0" max="1000" step="any" value={commission} onChange={(event) => setCommission(event.target.value)} disabled={pending} />)}
          {optionField(tl({ zh: "滑点（基点）", en: "Slippage (bps)" }),
            <Input type="number" min="0" max="10000" step="any" value={slippage} onChange={(event) => setSlippage(event.target.value)} disabled={pending} />)}
        </div>
      </details>
      {currentPreview && preview.error && <p role="alert" className="break-words text-sm text-destructive">{preview.error.message}</p>}
      {error && <p role="alert" className="break-words text-sm text-destructive">{error.message}</p>}
      {resolved && (
        <section className="space-y-2 rounded-lg border bg-card p-3 text-sm" aria-live="polite">
          <h3 className="font-semibold">{tl({ zh: "预检通过", en: "Preflight passed" })}</h3>
          <dl className="grid gap-2 text-xs sm:grid-cols-[auto_1fr]">
            <dt>{tl({ zh: "冻结策略", en: "Frozen strategy" })}</dt>
            <dd className="break-words font-medium">{visible(frozenSpec.name)} · v{visible(manifest.strategy_version)}</dd>
            <dt>{tl({ zh: "窗口", en: "Window" })}</dt>
            <dd>{visible(scopedWindow.decision_start ?? frozenPlan.test_start)} – {visible(scopedWindow.decision_end ?? frozenPlan.test_end)}</dd>
            <dt>{tl({ zh: "基准", en: "Benchmark" })}</dt>
            <dd>{visible(object(manifest.benchmark_config).symbol ?? frozenPlan.benchmark_symbol)}</dd>
            <dt>{tl({ zh: "组合约束", en: "Portfolio constraints" })}</dt>
            <dd className="break-words">{mapRows(effectivePortfolio) || "—"}</dd>
            <dt>{tl({ zh: "冻结费用假设", en: "Frozen fee assumptions" })}</dt>
            <dd className="break-words">{mapRows(effectiveFees) || "—"}</dd>
          </dl>
          {strings(object(manifest.data_scope).warnings).map((warning) => (
            <p key={warning} className="break-words text-xs text-warning">{warning}</p>
          ))}
          <details className="text-xs">
            <summary className="min-h-8 cursor-pointer py-1">{tl({ zh: "冻结摘要", en: "Manifest checksum" })}</summary>
            <p className="break-all font-mono text-muted-foreground">{resolved.manifest_checksum}</p>
          </details>
        </section>
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant={resolved ? "outline" : "default"} disabled={!payload || pending || preview.isPending}
          onClick={() => {
            if (previewPayload) {
              onRepreview();
              preview.mutate(previewPayload);
            }
          }}>
          {preview.isPending
            ? tl({ zh: "正在预检", en: "Checking" })
            : tl({ zh: "预览生效输入", en: "Preview effective inputs" })}
        </Button>
        <Button type="submit" disabled={!resolved || pending || preview.isPending || previewRejected} variant={resolved ? "default" : "outline"}>
          {tl({ zh: "启动研究运行", en: "Start research run" })}
        </Button>
        <Button type="button" variant="ghost" onClick={onCancel} disabled={pending}>
          {tl({ zh: "取消", en: "Cancel" })}
        </Button>
      </div>
    </form>
  );
}