import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, LoadingState } from "@/components/ui/states";
import { StatusBadge } from "@/components/ui/status-badge";
import { useT, type LocalizedText } from "@/i18n";
import { ApiError, api, type InstrumentItem } from "@/lib/api";
import { backfillApi, type BackfillReceipt, type BackfillSpec } from "@/lib/data-backfill";
import { researchRunApi } from "@/lib/research";
import { topicRunApi } from "@/lib/research-workspace";
import { dataHref } from "@/lib/data-links";

const datasetLabels: Record<string, LocalizedText> = {
  bars: { zh: "日线行情", en: "Daily bars" },
  daily_metrics: { zh: "每日指标", en: "Daily metrics" },
  financial_indicators: { zh: "财务指标", en: "Financial indicators" },
  convertible_metrics: { zh: "可转债指标", en: "Convertible-bond metrics" },
};

const actions: Record<string, LocalizedText> = {
  noop: { zh: "已覆盖", en: "Already covered" },
  reuse: { zh: "复用在途任务", en: "Reuse pending work" },
  sync: { zh: "补齐数据", en: "Fill gaps" },
  sync_publish: { zh: "补齐并生成新发布", en: "Fill gaps and publish a new release" },
  repair: { zh: "按缺口修复", en: "Repair gaps" },
};

function object(value: unknown) {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((entry) => typeof entry === "string") : [];
}

type SelectedInstrument = Pick<InstrumentItem, "code" | "market" | "name" | "instrument_type">;

async function findTopicScope(topicId: string) {
  const page = await topicRunApi.list(topicId, 5);
  const latest = page.items.find((item) => item.run_status === "completed");
  if (!latest) return null;
  const run = await researchRunApi.get(latest.run_id);
  const manifest = run.manifest;
  const scope = object(object(manifest.data_scope).scope);
  const spec = object(manifest.strategy_spec);
  const universe = object(scope.universe ?? spec.universe);
  const window = object(scope.decision ?? object(manifest.parameters).research_window);
  const plan = object(spec.validation_plan);
  const benchmarks = strings(scope.benchmarks);
  if (benchmarks.length > 1) return null;
  const benchmark = benchmarks[0] ?? object(manifest.benchmark_config).symbol ?? plan.benchmark_symbol;
  const symbols = strings(universe.explicit_symbols);
  const start = window.decision_start ?? window.start ?? plan.test_start;
  const end = window.decision_end ?? window.end ?? plan.test_end;
  if (!symbols.length || typeof start !== "string" || typeof end !== "string") return null;
  if (scope.bars_adjustment && scope.bars_adjustment !== "qfq") return null;
  return { symbols, start, end, benchmark };
}

export function BackfillRequestCard({ topicId }: { topicId: string | null }) {
  const { tl } = useT();
  const [params, setParams] = useSearchParams();
  const requestId = params.get("backfill");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<SelectedInstrument[]>([]);
  const [benchmark, setBenchmark] = useState<SelectedInstrument | null>(null);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [warmup, setWarmup] = useState(0);
  const [tail, setTail] = useState(1);
  const [source, setSource] = useState("");
  const [datasets, setDatasets] = useState(["bars"]);
  const [publish, setPublish] = useState(true);
  const [strict, setStrict] = useState(false);
  const reuseScope = useMutation({
    mutationFn: findTopicScope,
    onSuccess: (scope) => {
      if (!scope) return;
      setSelected(scope.symbols.map((code) => ({ code, name: code, market: "", instrument_type: "" })));
      setStart(scope.start);
      setEnd(scope.end);
      setBenchmark(typeof scope.benchmark === "string"
        ? { code: scope.benchmark, market: "", name: scope.benchmark, instrument_type: "" }
        : null);
    },
  });
  const choices = useQuery({
    queryKey: ["backfill-symbol-search", search.trim()],
    queryFn: () => api.getInstruments({ q: search.trim(), limit: 12 }),
    enabled: search.trim().length > 0,
  });
  const preview = useMutation({ mutationFn: backfillApi.preview });
  const [receipt, setReceipt] = useState<BackfillReceipt | null>(null);
  const [confirmRetry, setConfirmRetry] = useState(false);
  function rememberReceipt(nextReceipt: BackfillReceipt) {
    setReceipt(nextReceipt);
    const next = new URLSearchParams(params);
    next.set("backfill", nextReceipt.request_id);
    setParams(next);
  }
  const startRequest = useMutation({
    mutationFn: ({ spec, checksum, key }: { spec: BackfillSpec; checksum: string; key: string }) =>
      backfillApi.submit(spec, checksum, key),
    onSuccess: rememberReceipt,
  });
  const retry = useMutation({
    mutationFn: backfillApi.retry,
    onSuccess: (next) => {
      rememberReceipt(next);
      setConfirmRetry(false);
    },
  });
  const savedReceipt = receipt?.request_id === requestId ? receipt : null;
  const detail = useQuery({
    queryKey: ["backfill-receipt", requestId],
    queryFn: () => backfillApi.get(requestId!),
    enabled: !!requestId,
    refetchInterval: (query) => ["queued", "running"].includes(query.state.data?.status ?? savedReceipt?.status ?? "") ? 3000 : false,
  });
  const lastReceipt = detail.data ?? savedReceipt;
  const spec: BackfillSpec = {
    scope: {
      universe: { explicit_symbols: selected.map((item) => item.code),
        markets: [...new Set(selected.map((item) => item.market).filter(Boolean))] },
      decision: { start, end }, benchmarks: benchmark ? [benchmark.code] : [],
      datasets: { required: datasets }, warmup_sessions: warmup, tail_sessions: tail, bars_adjustment: "qfq",
    },
    datasets: datasets.map((dataset) => ({
      dataset, source: dataset === "bars" ? source || null : "tushare",
      adjustment: dataset === "bars" ? "qfq" : "none", publish,
    })),
    quality_profile: strict ? "strict" : "standard",
  };
  const valid = selected.length > 0 && start && end && start <= end
    && Number.isInteger(warmup) && warmup >= 0 && warmup <= 5000
    && Number.isInteger(tail) && tail >= 0 && tail <= 250;
  const matching = JSON.stringify(spec) === JSON.stringify(preview.variables);
  const plan = matching ? preview.data : undefined;
  const blocked = !!plan?.blockers.length || plan?.items.some((item) => item.blockers.length > 0);
  const expired = startRequest.error instanceof ApiError && startRequest.error.status === 409;
  const canRetry = lastReceipt && ["failed", "partial", "cancelled"].includes(detail.data?.status ?? lastReceipt.status);
  const idempotencyKey = plan && `web-${plan.plan_checksum}`;

  return (
    <Card aria-label={tl({ zh: "共享数据补数", en: "Shared data backfill" })} role="region">
      <CardHeader className="pb-3">
        <CardTitle className="text-base">{tl({ zh: "共享数据补数", en: "Shared data backfill" })}</CardTitle>
        {topicId && <Link className="text-sm text-primary underline" to={dataHref({ tab: "overview", topic: topicId })}>
          {tl({ zh: "查看来源课题数据范围", en: "Review originating topic scope" })}
        </Link>}
      </CardHeader>
      <CardContent>
        <form className="space-y-4" onSubmit={(event) => {
          event.preventDefault();
          if (!plan || blocked || expired || startRequest.isPending || startRequest.isSuccess || !idempotencyKey) return;
          startRequest.mutate({ spec, checksum: plan.plan_checksum, key: idempotencyKey });
        }}>
          {topicId && (
            <div className="space-y-2">
              <Button type="button" variant="outline" disabled={reuseScope.isPending || startRequest.isPending} onClick={() => reuseScope.mutate(topicId)}>
                {tl({ zh: "沿用课题最近的冻结范围", en: "Use the Latest Frozen Topic Scope" })}
              </Button>
              {reuseScope.isSuccess && !reuseScope.data && <p className="text-sm text-muted-foreground">
                {tl({ zh: "没有可沿用的前复权逐标的范围，请从目录选标的并填写窗口。", en: "No reusable adjusted instrument scope. Choose instruments and dates below." })}
              </p>}
              {reuseScope.error && <p role="alert" className="break-words text-sm text-destructive">{reuseScope.error.message}</p>}
            </div>
          )}
          <fieldset className="space-y-2" disabled={startRequest.isPending}>
            <label className="grid gap-1 text-sm">
              {tl({ zh: "搜索标的或基准", en: "Search instruments or benchmarks" })}
              <Input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={tl({ zh: "输入名称或代码", en: "Enter a name or symbol" })} />
            </label>
            {choices.isLoading && <LoadingState rows={1} />}
            {choices.isError && <ErrorState message={choices.error.message} onRetry={() => void choices.refetch()} />}
            {choices.data?.items.length === 0 && <p className="text-sm text-muted-foreground">{tl({ zh: "没有符合的标的", en: "No matching instruments" })}</p>}
            <ul className="max-h-52 space-y-1 overflow-auto">
              {choices.data?.items.map((item) => (
                <li key={item.code} className="flex flex-wrap items-center justify-between gap-1 border-b pb-1 text-sm">
                  <span className="break-words">{item.name} · {item.code}</span>
                  <div className="flex flex-wrap gap-1">
                    {item.instrument_type !== "index" && (
                      <Button type="button" variant="outline" size="sm" disabled={selected.some((row) => row.code === item.code)}
                        onClick={() => setSelected([...selected, item])} aria-label={`${tl({ zh: "选择标的", en: "Select instrument" })} ${item.name}`}>
                        {tl({ zh: "加入标的", en: "Add instrument" })}
                      </Button>
                    )}
                    <Button type="button" variant="ghost" size="sm" onClick={() => setBenchmark(item)}
                      aria-label={`${tl({ zh: "设定基准", en: "Set benchmark" })} ${item.name}`}>
                      {tl({ zh: "用作基准", en: "Use as benchmark" })}
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
            <div className="flex flex-wrap gap-2">
              {selected.map((item) => <Button key={item.code} type="button" variant="secondary" size="sm" onClick={() => setSelected(selected.filter((row) => row.code !== item.code))}>
                {item.name} · {tl({ zh: "移除", en: "Remove" })}
              </Button>)}
              {benchmark && <Button type="button" variant="secondary" size="sm" onClick={() => setBenchmark(null)}>
                {tl({ zh: "基准", en: "Benchmark" })} · {benchmark.name} · {tl({ zh: "移除", en: "Remove" })}
              </Button>}
            </div>
          </fieldset>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="grid gap-1 text-sm">{tl({ zh: "补数决策开始", en: "Backfill decision start" })}<Input type="date" value={start} onChange={(event) => setStart(event.target.value)} /></label>
            <label className="grid gap-1 text-sm">{tl({ zh: "补数决策结束", en: "Backfill decision end" })}<Input type="date" value={end} onChange={(event) => setEnd(event.target.value)} /></label>
          </div>
          <details className="space-y-3 rounded-md border p-3 text-sm">
            <summary className="min-h-8 cursor-pointer font-medium">{tl({ zh: "数据集、来源与覆盖选项", en: "Dataset, source, and coverage options" })}</summary>
            <p className="text-muted-foreground">{tl({ zh: "行情补数使用前复权。研究指标使用 Tushare 原始口径。", en: "Bar backfills use forward adjustment. Research metrics use their original Tushare values." })}</p>
            <label className="grid gap-1">
              {tl({ zh: "行情数据源", en: "Market data provider" })}
              <select className="h-11 rounded-md border bg-background px-2" value={source} onChange={(event) => setSource(event.target.value)}>
                <option value="">{tl({ zh: "实例默认", en: "Instance default" })}</option>
                <option value="tushare">Tushare</option><option value="akshare">AkShare</option><option value="yfinance">yfinance</option>
              </select>
            </label>
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="grid gap-1">{tl({ zh: "预热交易日数", en: "Warmup sessions" })}<Input type="number" min={0} max={5000} value={warmup} onChange={(event) => setWarmup(Number(event.target.value))} /></label>
              <label className="grid gap-1">{tl({ zh: "估值尾部交易日数", en: "Valuation tail sessions" })}<Input type="number" min={0} max={250} value={tail} onChange={(event) => setTail(Number(event.target.value))} /></label>
            </div>
            <div className="flex flex-wrap gap-4">
              {Object.entries(datasetLabels).map(([name, label]) => (
                <label key={name} className="flex items-center gap-1.5">
                  <input type="checkbox" disabled={name === "bars"} checked={datasets.includes(name)}
                    onChange={(event) => setDatasets(event.target.checked ? [...datasets, name] : datasets.filter((item) => item !== name))} />
                  {tl(label)}
                </label>
              ))}
            </div>
            <label className="flex items-center gap-1.5"><input type="checkbox" checked={publish} onChange={(event) => setPublish(event.target.checked)} />
              {tl({ zh: "通过质量门后生成新发布", en: "Publish a new release after quality checks" })}
            </label>
            <label className="flex items-center gap-1.5"><input type="checkbox" checked={strict} onChange={(event) => setStrict(event.target.checked)} />
              {tl({ zh: "使用严格质量档", en: "Use strict quality checks" })}
            </label>
          </details>
          {plan && (
            <div className="space-y-2 rounded-md border p-3 text-sm" aria-live="polite">
              <p className="font-medium">{tl({ zh: "预检结果", en: "Preflight result" })}</p>
              <p className="text-muted-foreground">
                {tl({ zh: "预计请求 / 今日余量", en: "Estimated requests / remaining today" })} · {plan.quota.estimated_total} / {plan.quota.remaining_today ?? "—"}
                {" · "}{tl({ zh: "预计新增发布", en: "Planned new releases" })} {plan.publish_impact.new_releases}
              </p>
              {plan.quota.status === "defer" && <p className="text-warning">{tl({ zh: "配额不足时任务等待并重试", en: "The job waits and retries when quota is low" })}</p>}
              <ul className="space-y-1">{plan.items.map((item) => (
                <li key={`${item.dataset}:${item.source}:${item.adjustment}`} className="break-words">
                  {tl(datasetLabels[item.dataset] ?? { zh: item.dataset, en: item.dataset })} · {item.source} · {item.adjustment} · {tl(actions[item.action] ?? { zh: item.action, en: item.action })}
                  {item.missing_ranges.length > 0 && <span className="block text-muted-foreground">{item.missing_ranges.map(([from, to]) => `${from} – ${to}`).join(" / ")}</span>}
                  {item.publish_preview && <span className="block break-all">{item.publish_preview.release_id}</span>}
                </li>
              ))}</ul>
              {plan.blockers.map((blocker, index) => <p role="alert" className="break-words text-destructive" key={`${blocker.code}:${index}`}>{blocker.message}</p>)}
              {plan.warnings.map((warning) => <p className="break-words text-warning" key={warning}>{warning}</p>)}
            </div>
          )}
          {matching && preview.error && <p role="alert" className="break-words text-sm text-destructive">{preview.error.message}</p>}
          {startRequest.error && <p role="alert" className="break-words text-sm text-destructive">{expired
            ? tl({ zh: "补数计划已变化，请重新预检。", en: "The backfill plan changed. Preview it again." }) : startRequest.error.message}</p>}
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" disabled={!valid || preview.isPending || startRequest.isPending} onClick={() => {
              startRequest.reset();
              retry.reset();
              preview.mutate(spec);
            }}>{tl({ zh: "范围预检", en: "Preview Scope" })}</Button>
            <Button type="submit" disabled={!plan || blocked || expired || startRequest.isPending || startRequest.isSuccess}>
              {tl({ zh: "确认提交补数请求", en: "Submit Backfill Request" })}
            </Button>
          </div>
          {requestId && detail.isError && <ErrorState message={detail.error.message} onRetry={() => void detail.refetch()} />}
          {lastReceipt && (
            <div className="space-y-2 rounded-md border p-3 text-sm" role="status">
              <p className="flex flex-wrap gap-2"><span className="break-all">{lastReceipt.request_id}</span><StatusBadge status={detail.data?.status ?? lastReceipt.status} /></p>
              {lastReceipt.job_id && <Link className="text-primary underline" to={`/jobs?job=${encodeURIComponent(lastReceipt.job_id)}`}>
                {tl({ zh: "查看补数任务", en: "View backfill job" })}
              </Link>}
              {detail.data?.error_summary && <p className="break-words text-destructive">{detail.data.error_summary}</p>}
              {canRetry && <div>
                <Button type="button" variant="outline" onClick={() => { retry.reset(); setConfirmRetry(true); }}>
                  {tl({ zh: "重试未完成项", en: "Retry Incomplete Items" })}
                </Button>
              </div>}
              {detail.data?.release_ids.map((release) => <p key={release} className="break-all"><Link
                className="text-primary underline" to={dataHref({ tab: "datasets", release })}>{release}</Link></p>)}
            </div>
          )}
        </form>
        <Dialog open={confirmRetry} onOpenChange={(open) => { if (!open && !retry.isPending) setConfirmRetry(false); }}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{tl({ zh: "确认续补失败项", en: "Confirm Backfill Retry" })}</DialogTitle>
              <DialogDescription>{tl({ zh: "按原冻结范围为未完成项创建新请求，已完成的发布继续可用。", en: "Create a new request for incomplete items using the original frozen scope. Existing releases remain available." })}</DialogDescription>
            </DialogHeader>
            {retry.error && <p role="alert" className="text-sm text-destructive">{retry.error.message}</p>}
            <DialogFooter>
              <Button type="button" variant="outline" disabled={retry.isPending} onClick={() => setConfirmRetry(false)}>{tl({ zh: "返回", en: "Back" })}</Button>
              <Button type="button" disabled={retry.isPending || !canRetry} onClick={() => lastReceipt && retry.mutate(lastReceipt.request_id)}>
                {tl({ zh: "确认提交续补", en: "Confirm Retry" })}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  );
}
