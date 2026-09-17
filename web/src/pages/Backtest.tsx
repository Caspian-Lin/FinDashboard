import { useState, useMemo, useCallback, useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useLocation, useSearchParams } from "react-router-dom";
import { Bookmark, ChevronDown } from "lucide-react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import InfoHint, { HintLabel } from "../components/InfoHint";
import FactorSelectionForm from "../components/FactorSelectionForm";
import StrategyParamForm from "../components/StrategyParamForm";
import { StrategyPresetsPanel } from "./Strategies";
import { PageHeader } from "@/components/ui/page-header";
import { Button } from "@/components/ui/button";
import { MasterList, MasterListItem } from "@/components/ui/master-list";
import { EmptyState } from "@/components/ui/states";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { SelectAllResultsButton } from "../components/selection/SelectAllResultsButton";
import {
  defaultStrategyParams,
  normalizeStrategyParams,
  strategyFieldErrorsFrom,
  type StrategyFieldErrors,
  type StrategyParams,
  validateStrategyParams,
} from "../lib/strategyParams";
import {
  api,
  DEFAULT_FACTOR_SELECTION,
  type BacktestHistoryItem,
  type BacktestResult,
  type FactorSelectionInput,
  isJobRunning,
} from "../lib/api";
import { INFO_HINTS } from "../lib/infoHints";
import { useT, type LocalizedText } from "@/i18n";

const MARKETS: { value: string; label: LocalizedText }[] = [
  { value: "", label: { zh: "全部市场", en: "All markets" } },
  { value: "a_share", label: { zh: "A股", en: "A-shares" } },
  { value: "hk", label: { zh: "港股", en: "HK stocks" } },
  { value: "us", label: { zh: "美股", en: "US stocks" } },
];
const TYPES: { value: string; label: LocalizedText }[] = [
  { value: "", label: { zh: "全部类型", en: "All types" } },
  { value: "stock", label: { zh: "股票", en: "Stocks" } },
  { value: "etf", label: { zh: "ETF", en: "ETF" } },
];

/** 历史条目创建时刻(MM-DD HH:MM):同参数多次运行靠它区分,完整时间戳放 title。 */
function formatHistoryTime(iso: string) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function Backtest() {
  const { tl } = useT();
  const qc = useQueryClient();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const [strategy, setStrategy] = useState("ma_cross");
  const [strategyParams, setStrategyParams] = useState<StrategyParams>({});
  const [strategyErrors, setStrategyErrors] = useState<StrategyFieldErrors>({});
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>(["510300.SH"]);
  const [start, setStart] = useState("2024-01-01");
  const [end, setEnd] = useState(new Date().toISOString().slice(0, 10));
  const [capital, setCapital] = useState("100000");
  const [selection, setSelection] = useState<FactorSelectionInput>({
    ...DEFAULT_FACTOR_SELECTION,
  });

  // 费用参数
  const [commissionRate, setCommissionRate] = useState("0.0003");
  const [commissionMin, setCommissionMin] = useState("1");
  const [stampTaxRate, setStampTaxRate] = useState("0.0005");
  const [slippageBps, setSlippageBps] = useState("0");

  // 标的搜索
  const [searchQuery, setSearchQuery] = useState("");
  const [marketFilter, setMarketFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [page, setPage] = useState(0);
  const PAGE_SIZE = 200;

  const resetPage = useCallback(() => setPage(0), []);
  const handleSearch = useCallback((v: string) => { setSearchQuery(v); resetPage(); }, [resetPage]);
  const handleMarket = useCallback((v: string) => { setMarketFilter(v); resetPage(); }, [resetPage]);
  const handleType = useCallback((v: string) => { setTypeFilter(v); resetPage(); }, [resetPage]);

  // 结果视图: 当前结果或历史记录
  const [activeResult, setActiveResult] = useState<BacktestResult | null>(null);
  const [activeHistoryId, setActiveHistoryId] = useState<number | null>(null);
  // 历史删除的二次确认目标(null=未打开确认弹窗)
  const [deleteTarget, setDeleteTarget] = useState<BacktestHistoryItem | null>(null);
  // 策略预设抽屉(原独立「策略预设」页并入)
  const [presetsOpen, setPresetsOpen] = useState(false);
  // 回测配置卡默认折叠:结果优先,摘要行常驻可一键运行
  const [configOpen, setConfigOpen] = useState(false);
  // 回测已迁移到统一任务队列(#144):POST /backtest/run 返回 JobOut,前端轮询
  // /api/jobs/{job_id},succeeded 后用 result_ref(run_id) 查历史详情拿 BacktestResult。
  const [backtestJobId, setBacktestJobId] = useState<string | null>(null);

  const { data: strategies } = useQuery({
    queryKey: ["strategies"],
    queryFn: api.getStrategies,
  });
  const backtestStrategies = useMemo(
    () => strategies?.filter((item) => item.supports_backtest) ?? [],
    [strategies],
  );
  const strategyDefinition = useMemo(
    () => backtestStrategies.find((item) => item.kind === strategy),
    [backtestStrategies, strategy],
  );

  const presetParam = searchParams.get("preset");
  const presetId = presetParam && /^\d+$/.test(presetParam) ? Number(presetParam) : null;
  const { data: requestedPreset } = useQuery({
    queryKey: ["strategy-preset", presetId],
    queryFn: () => api.getStrategyPreset(presetId as number),
    enabled: presetId !== null,
  });
  const requestedPresetDefinition = requestedPreset
    ? backtestStrategies.find((item) => item.kind === requestedPreset.strategy)
    : undefined;
  const appliedConfig = useRef<string | null>(null);

  useEffect(() => {
    if (backtestStrategies.length === 0) return;

    if (requestedPreset) {
      const definition = requestedPresetDefinition;
      const key = `preset:${requestedPreset.id}`;
      if (definition && appliedConfig.current !== key) {
        setStrategy(definition.kind);
        setStrategyParams({
          ...defaultStrategyParams(definition),
          ...requestedPreset.params,
        });
        setStrategyErrors({});
        setSelection(requestedPreset.selection);
        appliedConfig.current = key;
      }
      return;
    }

    const draft = (
      location.state as
        | {
            strategyDraft?: {
              strategy: string;
              params: StrategyParams;
              selection?: FactorSelectionInput;
            };
          }
        | null
    )?.strategyDraft;
    if (draft) {
      const definition = backtestStrategies.find((item) => item.kind === draft.strategy);
      const key = `draft:${draft.strategy}`;
      if (definition && appliedConfig.current !== key) {
        setStrategy(definition.kind);
        setStrategyParams({
          ...defaultStrategyParams(definition),
          ...draft.params,
        });
        setStrategyErrors({});
        setSelection(draft.selection ?? { ...DEFAULT_FACTOR_SELECTION });
        appliedConfig.current = key;
      }
      return;
    }

    if (Object.keys(strategyParams).length === 0 && strategyDefinition) {
      setStrategyParams(defaultStrategyParams(strategyDefinition));
      appliedConfig.current = `default:${strategyDefinition.kind}`;
    }
  }, [
    backtestStrategies,
    location.state,
    requestedPreset,
    requestedPresetDefinition,
    strategyDefinition,
    strategyParams,
  ]);

  const { data: history } = useQuery({
    queryKey: ["backtest-history"],
    queryFn: () => api.getBacktestHistory(30),
    refetchInterval: false,
  });

  // 标的列表(搜索 + 筛选统一走分页端点)
  const hasSearch = searchQuery.length >= 2;
  const { data: listData } = useQuery({
    queryKey: ["instruments-list", hasSearch ? searchQuery : "", marketFilter, typeFilter, page],
    queryFn: () =>
      api.getInstruments({
        q: hasSearch ? searchQuery : undefined,
        market: marketFilter || undefined,
        instrument_type: typeFilter || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
  });

  const totalCount = listData?.total ?? 0;

  const candidateList = useMemo(() => listData?.items ?? [], [listData]);

  const runBacktest = useMutation({
    mutationFn: (normalizedParams: StrategyParams) =>
      api.runBacktest({
        strategy,
        symbols: selectedSymbols,
        start,
        end,
        capital,
        params: normalizedParams,
        selection,
        commission_rate: commissionRate,
        commission_min: commissionMin,
        stamp_tax_rate: stampTaxRate,
        slippage_bps: slippageBps,
      }),
    onSuccess: (job) => {
      setStrategyErrors({});
      // 清空旧结果,进入"运行中"态;真正的 BacktestResult 在任务完成后由 detailQuery 注入。
      setActiveResult(null);
      setActiveHistoryId(null);
      setBacktestJobId(job.job_id);
    },
    onError: (error) => setStrategyErrors(strategyFieldErrorsFrom(error)),
  });

  // 轮询回测任务直到终态。result_ref 是 run_id(数字字符串)。
  const { data: backtestJob } = useQuery({
    queryKey: ["job", backtestJobId],
    queryFn: () => api.getJob(backtestJobId as string),
    enabled: Boolean(backtestJobId),
    refetchInterval: (query) => (isJobRunning(query.state.data) ? 1500 : false),
  });

  const backtestRunId =
    backtestJob && !isJobRunning(backtestJob) && backtestJob.status === "succeeded" && backtestJob.result_ref
      ? Number(backtestJob.result_ref)
      : null;

  // 任务完成后,用 run_id 拉历史详情,重构成 BacktestResult 注入 activeResult。
  const { data: backtestDetail } = useQuery({
    queryKey: ["backtest-history", backtestRunId],
    queryFn: () => api.getBacktestHistoryDetail(backtestRunId as number),
    enabled: backtestRunId !== null,
    staleTime: Infinity,
  });

  const appliedJobRunId = useRef<number | null>(null);
  useEffect(() => {
    if (!backtestDetail || appliedJobRunId.current === backtestDetail.id) return;
    appliedJobRunId.current = backtestDetail.id;
    setActiveResult({
      metrics: backtestDetail.metrics,
      equity_curve: backtestDetail.equity_curve,
      fills: backtestDetail.fills,
      summary: backtestDetail.summary,
      run_id: backtestDetail.id,
      selection_snapshots: backtestDetail.selection_snapshots,
      dataset_versions: backtestDetail.dataset_versions,
      factor_version: backtestDetail.factor_version,
    });
    setFillsPage(0);
    setActiveHistoryId(backtestDetail.id);
    qc.invalidateQueries({ queryKey: ["backtest-history"] });
  }, [backtestDetail, qc]);

  const loadHistory = useMutation({
    mutationFn: (id: number) => api.getBacktestHistoryDetail(id),
    onSuccess: (detail) => {
      setActiveResult({
        metrics: detail.metrics,
        equity_curve: detail.equity_curve,
        fills: detail.fills,
        summary: detail.summary,
        run_id: detail.id,
        selection_snapshots: detail.selection_snapshots,
        dataset_versions: detail.dataset_versions,
        factor_version: detail.factor_version,
      });
      setFillsPage(0);
      setActiveHistoryId(detail.id);
      setStrategy(detail.strategy);
      const definition = backtestStrategies.find(
        (item) => item.kind === detail.strategy,
      );
      setStrategyParams(
        definition
          ? { ...defaultStrategyParams(definition), ...detail.params }
          : detail.params,
      );
      setStrategyErrors({});
      setSelectedSymbols(detail.symbols);
      setStart(detail.start);
      setEnd(detail.end);
      setCapital(detail.capital);
      setSelection(detail.selection);
    },
  });

  const deleteHistory = useMutation({
    mutationFn: (id: number) => api.deleteBacktestHistory(id),
    onSuccess: () => {
      setDeleteTarget(null);
      qc.invalidateQueries({ queryKey: ["backtest-history"] });
    },
  });

  const toggleSymbol = (code: string) => {
    setSelectedSymbols((prev) =>
      prev.includes(code) ? prev.filter((s) => s !== code) : [...prev, code],
    );
  };

  const selectAllCandidates = useMutation({
    mutationFn: async () => {
      return api.getInstrumentCodes({
        q: hasSearch ? searchQuery : undefined,
        market: marketFilter || undefined,
        instrument_type: typeFilter || undefined,
      });
    },
    onSuccess: (codes) => {
      setSelectedSymbols((prev) => Array.from(new Set([...prev, ...codes])));
    },
  });

  const result = activeResult;
  const m = result?.metrics;

  // 交易明细分页:10 条/页;新结果载入时回第一页,越界由 clamp 兜底
  const FILLS_PAGE_SIZE = 10;
  const [fillsPage, setFillsPage] = useState(0);
  const fillsPageCount = Math.max(
    1,
    Math.ceil((result?.fills.length ?? 0) / FILLS_PAGE_SIZE),
  );
  const fillsPageClamped = Math.min(fillsPage, fillsPageCount - 1);
  const backtestRunning = runBacktest.isPending || isJobRunning(backtestJob);
  const backtestFailed =
    backtestJob && !isJobRunning(backtestJob) && backtestJob.status !== "succeeded"
      ? backtestJob
      : null;

  const changeStrategy = (kind: string) => {
    const definition = backtestStrategies.find((item) => item.kind === kind);
    setStrategy(kind);
    setStrategyParams(definition ? defaultStrategyParams(definition) : {});
    setStrategyErrors({});
    runBacktest.reset();
    setBacktestJobId(null);
    appliedJobRunId.current = null;
    appliedConfig.current = `manual:${kind}`;
  };

  const startBacktest = () => {
    if (!strategyDefinition) return;
    const errors = validateStrategyParams(strategyDefinition, strategyParams);
    setStrategyErrors(errors);
    if (Object.keys(errors).length > 0) {
      // 校验失败时展开配置卡,让字段级错误可见(错误展示在 StrategyParamForm 内)
      setConfigOpen(true);
      return;
    }
    runBacktest.mutate(
      normalizeStrategyParams(strategyDefinition, strategyParams),
    );
  };

  // 配置摘要(折叠态也可见):策略名 / 标的数 / 区间 / 资金
  const strategyName = strategyDefinition?.name ?? strategy;
  const capitalNum = Number(capital);
  const capitalDisplay = Number.isFinite(capitalNum) ? capitalNum.toLocaleString() : capital;
  const configSummary = [
    strategyName,
    tl({ zh: `${selectedSymbols.length} 标的`, en: `${selectedSymbols.length} symbols` }),
    `${start} ~ ${end}`,
    `¥${capitalDisplay}`,
  ].join(" · ");

  // 回测配置卡(可折叠,默认折叠):摘要行常驻,折叠态也能一键运行
  const configCard = (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex flex-wrap items-center gap-2 px-5 py-4">
        <button
          type="button"
          onClick={() => setConfigOpen((v) => !v)}
          aria-expanded={configOpen}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <ChevronDown
            className={`h-4 w-4 shrink-0 text-muted-foreground transition-transform ${
              configOpen ? "" : "-rotate-90"
            }`}
          />
          <h2 className="text-base font-semibold text-foreground">
            {tl({ zh: "回测配置", en: "Backtest Configuration" })}
          </h2>
          <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
            {configSummary}
          </span>
        </button>
        <Button
          size="sm"
          onClick={startBacktest}
          disabled={backtestRunning || selectedSymbols.length === 0}
        >
          {backtestRunning
            ? tl({
                zh: `回测中…${backtestJob?.phase ? `（${backtestJob.phase}）` : ""}`,
                en: `Running…${backtestJob?.phase ? ` (${backtestJob.phase})` : ""}`,
              })
            : tl({ zh: "运行回测", en: "Run backtest" })}
        </Button>
      </div>
      {(runBacktest.error || backtestFailed) && (
        <div className="space-y-1 px-5 pb-4">
          {runBacktest.error && (
            <p className="text-sm text-destructive">{(runBacktest.error as Error).message}</p>
          )}
          {backtestFailed && (
            <p className="text-sm text-destructive">
              {tl({ zh: "回测任务失败: ", en: "Backtest job failed: " })}
              {backtestFailed.error_summary ?? backtestFailed.status}
            </p>
          )}
        </div>
      )}
      {configOpen && (
        <div className="border-t border-border p-5">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
            <div>
              <HintLabel htmlFor="backtest-strategy" hint={INFO_HINTS.backtest.strategy}>
                {tl({ zh: "策略", en: "Strategy" })}
              </HintLabel>
              <select
                id="backtest-strategy"
                value={strategy}
                onChange={(e) => changeStrategy(e.target.value)}
                className="w-full border border-input bg-card text-foreground rounded px-3 py-2 text-sm"
              >
                {backtestStrategies.map((s) => (
                  <option key={s.kind} value={s.kind}>
                    {s.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <HintLabel htmlFor="backtest-start" hint={INFO_HINTS.backtest.startDate}>
                {tl({ zh: "开始", en: "Start" })}
              </HintLabel>
              <input
                id="backtest-start"
                type="date"
                value={start}
                onChange={(e) => setStart(e.target.value)}
                className="w-full border border-input bg-card text-foreground rounded px-3 py-2 text-sm"
              />
            </div>
            <div>
              <HintLabel htmlFor="backtest-end" hint={INFO_HINTS.backtest.endDate}>
                {tl({ zh: "结束", en: "End" })}
              </HintLabel>
              <input
                id="backtest-end"
                type="date"
                value={end}
                onChange={(e) => setEnd(e.target.value)}
                className="w-full border border-input bg-card text-foreground rounded px-3 py-2 text-sm"
              />
            </div>
            <div>
              <HintLabel
                htmlFor="backtest-capital"
                hint={INFO_HINTS.backtest.initialCapital}
              >
                {tl({ zh: "初始资金(¥)", en: "Initial capital (¥)" })}
              </HintLabel>
              <input
                id="backtest-capital"
                type="number"
                value={capital}
                onChange={(e) => setCapital(e.target.value)}
                className="w-full border border-input bg-card text-foreground rounded px-3 py-2 text-sm"
              />
            </div>
          </div>

          {requestedPreset && requestedPresetDefinition && (
            <div className="mt-4 flex flex-wrap items-center justify-between gap-2 rounded-lg bg-primary/10 px-3 py-2 text-sm text-primary">
              <span>
                {tl({ zh: "已载入策略预设：", en: "Strategy preset loaded: " })}
                <strong>{requestedPreset.name}</strong>
              </span>
              <span className="text-xs text-primary">{tl({ zh: "仅用于本次回测配置", en: "Applied to this backtest only" })}</span>
            </div>
          )}
          {requestedPreset && !requestedPresetDefinition && (
            <p className="mt-4 rounded-md bg-warning/10 px-3 py-2 text-sm text-warning">
              {tl({ zh: "预设“", en: 'Preset "' })}
              {requestedPreset.name}
              {tl({ zh: "”依赖实时时钟事件，当前回测引擎无法运行。", en: '" relies on real-time clock events, which the current backtest engine cannot run.' })}
            </p>
          )}

          {strategyDefinition && (
            <div className="mt-4 border-t border-border pt-4">
              <div className="mb-3 flex items-center gap-1">
                <h3 className="text-sm font-semibold text-muted-foreground">{tl({ zh: "策略参数", en: "Strategy Parameters" })}</h3>
                <InfoHint content={INFO_HINTS.backtest.strategyParams} />
              </div>
              <StrategyParamForm
                definition={strategyDefinition}
                values={strategyParams}
                onChange={(next) => {
                  setStrategyParams(next);
                  setStrategyErrors({});
                  runBacktest.reset();
                }}
                errors={strategyErrors}
                disabled={backtestRunning}
              />
            </div>
          )}

          <FactorSelectionForm
            value={selection}
            onChange={(next) => {
              setSelection(next);
              runBacktest.reset();
            }}
          />

          {/* Fee params */}
          <details className="mt-3">
            <summary className="text-sm text-muted-foreground cursor-pointer hover:text-muted-foreground">
              {tl({ zh: "费用参数(佣金 / 印花税 / 滑点)", en: "Fee Parameters (commission / stamp tax / slippage)" })}
            </summary>
            <div className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <div>
                <HintLabel
                  htmlFor="backtest-commission-rate"
                  hint={INFO_HINTS.backtest.commissionRate}
                  labelClassName="text-xs text-muted-foreground"
                >
                  {tl({ zh: "佣金率(万N)", en: "Commission rate (per 10k)" })}
                </HintLabel>
                <input
                  id="backtest-commission-rate"
                  type="number"
                  step="0.0001"
                  value={commissionRate}
                  onChange={(e) => setCommissionRate(e.target.value)}
                  className="w-full border border-input bg-card text-foreground rounded px-2 py-1.5 text-sm"
                  placeholder={tl({ zh: "0.0003 = 万3", en: "0.0003 = 0.03%" })}
                />
              </div>
              <div>
                <HintLabel
                  htmlFor="backtest-minimum-commission"
                  hint={INFO_HINTS.backtest.minimumCommission}
                  labelClassName="text-xs text-muted-foreground"
                >
                  {tl({ zh: "最低佣金(¥)", en: "Minimum commission (¥)" })}
                </HintLabel>
                <input
                  id="backtest-minimum-commission"
                  type="number"
                  value={commissionMin}
                  onChange={(e) => setCommissionMin(e.target.value)}
                  className="w-full border border-input bg-card text-foreground rounded px-2 py-1.5 text-sm"
                  placeholder="1"
                />
              </div>
              <div>
                <HintLabel
                  htmlFor="backtest-stamp-tax"
                  hint={INFO_HINTS.backtest.stampTax}
                  labelClassName="text-xs text-muted-foreground"
                >
                  {tl({ zh: "印花税(万N,卖出)", en: "Stamp tax (per 10k, sell only)" })}
                </HintLabel>
                <input
                  id="backtest-stamp-tax"
                  type="number"
                  step="0.0001"
                  value={stampTaxRate}
                  onChange={(e) => setStampTaxRate(e.target.value)}
                  className="w-full border border-input bg-card text-foreground rounded px-2 py-1.5 text-sm"
                  placeholder={tl({ zh: "0.0005 = 万5, ETF 填 0", en: "0.0005 = 0.05%, enter 0 for ETF" })}
                />
              </div>
              <div>
                <HintLabel
                  htmlFor="backtest-slippage"
                  hint={INFO_HINTS.backtest.slippage}
                  labelClassName="text-xs text-muted-foreground"
                >
                  {tl({ zh: "滑点(bps)", en: "Slippage (bps)" })}
                </HintLabel>
                <input
                  id="backtest-slippage"
                  type="number"
                  value={slippageBps}
                  onChange={(e) => setSlippageBps(e.target.value)}
                  className="w-full border border-input bg-card text-foreground rounded px-2 py-1.5 text-sm"
                  placeholder="0"
                />
              </div>
            </div>
            <p className="text-xs text-muted-foreground/70 mt-1">
              {tl({
                zh: "ETF 免印花税(填 0);股票卖出收万5。佣金 = max(成交额 × 佣金率, 最低佣金)。",
                en: "ETFs are exempt from stamp tax (enter 0); sells on stocks are charged 0.05%. Commission = max(trade value × rate, minimum commission).",
              })}
            </p>
          </details>

          {/* Symbol selector */}
          <SymbolSelector
            searchQuery={searchQuery}
            setSearchQuery={handleSearch}
            marketFilter={marketFilter}
            setMarketFilter={handleMarket}
            typeFilter={typeFilter}
            setTypeFilter={handleType}
            candidates={candidateList}
            totalCount={totalCount}
            page={page}
            pageSize={PAGE_SIZE}
            onPageChange={setPage}
            selectedSymbols={selectedSymbols}
            toggleSymbol={toggleSymbol}
            selectAllCandidates={selectAllCandidates}
            clearSelection={() => setSelectedSymbols([])}
            onSymbolsLoaded={(codes) => setSelectedSymbols(codes)}
          />

        </div>
      )}
    </div>
  );

  // 结果区:指标卡 / 候选池审计 / 权益曲线 / 成交明细(主列首位,配置卡之前)
  const resultsSection = m ? (
    <>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <MetricCard label={tl({ zh: "总收益", en: "Total return" })} value={`${(m.total_return * 100).toFixed(2)}%`} positive={m.total_return >= 0} />
              <MetricCard label={tl({ zh: "年化", en: "Annualized" })} value={`${(m.annualized_return * 100).toFixed(2)}%`} positive={m.annualized_return >= 0} />
              <MetricCard label={tl({ zh: "夏普", en: "Sharpe" })} value={m.sharpe_ratio.toFixed(2)} />
              <MetricCard label={tl({ zh: "最大回撤", en: "Max drawdown" })} value={`${(m.max_drawdown * 100).toFixed(2)}%`} positive={false} />
              <MetricCard label={tl({ zh: "胜率", en: "Win rate" })} value={`${(m.win_rate * 100).toFixed(1)}%`} />
              <MetricCard label={tl({ zh: "交易次数", en: "Trades" })} value={String(m.trade_count)} />
              <MetricCard label={tl({ zh: "换手率", en: "Turnover" })} value={m.turnover.toFixed(2)} />
              <MetricCard
                label={tl({ zh: "超额收益", en: "Excess return" })}
                value={
                  m.excess_return !== null
                    ? `${(m.excess_return * 100).toFixed(2)}%`
                    : "—"
                }
                positive={m.excess_return !== null && m.excess_return >= 0}
              />
            </div>

            {result.selection_snapshots.length > 0 && (
              <div className="rounded-lg border border-border bg-card p-5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h2 className="text-lg font-semibold text-foreground">{tl({ zh: "候选池审计", en: "Candidate Pool Audit" })}</h2>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {tl({
                        zh: `因子版本 ${result.factor_version} · ${result.selection_snapshots.length} 个日快照`,
                        en: `Factor version ${result.factor_version} · ${result.selection_snapshots.length} daily snapshots`,
                      })}
                    </p>
                  </div>
                  <span className="rounded-full bg-secondary px-2.5 py-1 text-xs text-muted-foreground">
                    {tl({ zh: "数据版本已归档", en: "Data versions archived" })}
                  </span>
                </div>
                <div className="mt-4 max-h-56 divide-y divide-border overflow-y-auto">
                  {result.selection_snapshots.map((snapshot) => (
                    <div
                      key={snapshot.checksum}
                      className="grid gap-1 py-2 text-sm sm:grid-cols-[7rem_7rem_1fr]"
                    >
                      <span className="font-mono text-xs text-muted-foreground">
                        {snapshot.effective_date}
                      </span>
                      <span
                        className={
                          snapshot.status === "published"
                            ? "text-success"
                            : "text-warning"
                        }
                      >
                        {snapshot.status === "published" ? tl({ zh: "已发布", en: "Published" }) : tl({ zh: "跳过调仓", en: "Rebalance skipped" })}
                      </span>
                      <span className="text-foreground">
                        {snapshot.status === "published"
                          ? tl({
                              zh: `${snapshot.selected_symbols.length} 个标的`,
                              en: `${snapshot.selected_symbols.length} symbols`,
                            })
                          : snapshot.skip_reason}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {result.equity_curve.length > 0 && (
              <div className="rounded-lg border border-border bg-card p-5">
                <h2 className="text-lg font-semibold mb-4">{tl({ zh: "权益曲线", en: "Equity Curve" })}</h2>
                <ResponsiveContainer width="100%" height={350}>
                  <LineChart data={result.equity_curve}>
                    <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                    <XAxis dataKey="date" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                    <YAxis tick={{ fontSize: 11 }} />
                    <Tooltip
                      formatter={(v) =>
                        `¥${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
                      }
                    />
                    <Legend />
                    <Line type="monotone" dataKey="equity" stroke="hsl(var(--chart-1))" name={tl({ zh: "策略", en: "Strategy" })} dot={false} strokeWidth={2} />
                    {result.equity_curve.some((p) => p.benchmark !== null) && (
                      <Line type="monotone" dataKey="benchmark" stroke="hsl(var(--muted-foreground))" name={tl({ zh: "买入持有", en: "Buy & hold" })} dot={false} strokeWidth={1.5} strokeDasharray="5 5" />
                    )}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}

            {result.fills.length > 0 && (
              <div className="rounded-lg border border-border bg-card">
                <div className="px-5 py-3 border-b">
                  <h2 className="text-lg font-semibold">{tl({ zh: "交易明细", en: "Fills" })} ({result.fills.length})</h2>
                </div>
                <div className="overflow-x-auto scrollbar-thin">
                <table className="w-full text-sm">
                  <thead className="bg-background text-muted-foreground">
                    <tr>
                      <th className="px-4 py-2 text-left">{tl({ zh: "日期", en: "Date" })}</th>
                      <th className="px-4 py-2 text-left">{tl({ zh: "标的", en: "Symbol" })}</th>
                      <th className="px-4 py-2 text-left">{tl({ zh: "方向", en: "Side" })}</th>
                      <th className="px-4 py-2 text-right">{tl({ zh: "数量", en: "Quantity" })}</th>
                      <th className="px-4 py-2 text-right">{tl({ zh: "价格", en: "Price" })}</th>
                      <th className="px-4 py-2 text-right">{tl({ zh: "佣金", en: "Commission" })}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.fills
                      .slice(
                        fillsPageClamped * FILLS_PAGE_SIZE,
                        (fillsPageClamped + 1) * FILLS_PAGE_SIZE,
                      )
                      .map((f, i) => (
                      <tr key={fillsPageClamped * FILLS_PAGE_SIZE + i} className="border-t">
                        <td className="px-4 py-2 text-muted-foreground">{f.date}</td>
                        <td className="px-4 py-2 font-mono">{f.symbol}</td>
                        <td className={`px-4 py-2 ${f.side === "buy" ? "text-up" : "text-down"}`}>
                          {f.side === "buy" ? tl({ zh: "买入", en: "Buy" }) : tl({ zh: "卖出", en: "Sell" })}
                        </td>
                        <td className="px-4 py-2 text-right">{f.quantity}</td>
                        <td className="px-4 py-2 text-right font-mono">{Number(f.price).toFixed(2)}</td>
                        <td className="px-4 py-2 text-right text-muted-foreground">{Number(f.commission).toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                </div>
                {fillsPageCount > 1 && (
                  <div className="flex items-center justify-end gap-2 border-t px-4 py-2 text-xs text-muted-foreground">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 px-2"
                      disabled={fillsPageClamped === 0}
                      onClick={() => setFillsPage(fillsPageClamped - 1)}
                    >
                      {tl({ zh: "上一页", en: "Prev" })}
                    </Button>
                    <span className="tabular-nums">
                      {tl({
                        zh: `第 ${fillsPageClamped + 1} / ${fillsPageCount} 页`,
                        en: `Page ${fillsPageClamped + 1} / ${fillsPageCount}`,
                      })}
                    </span>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 px-2"
                      disabled={fillsPageClamped >= fillsPageCount - 1}
                      onClick={() => setFillsPage(fillsPageClamped + 1)}
                    >
                      {tl({ zh: "下一页", en: "Next" })}
                    </Button>
                  </div>
                )}
              </div>
            )}
    </>
  ) : null;

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={tl({ zh: "回测", en: "Backtest" })}
        description={tl({
          zh: "历史行情回放 + 纸面撮合;回测任务进入统一队列,可在「任务中心」跟踪。",
          en: "Historical bar replay + paper matching; backtest jobs enter the unified queue and can be tracked in the Task Center.",
        })}
        actions={
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" className="gap-1.5" onClick={() => setPresetsOpen(true)}>
              <Bookmark className="h-4 w-4" />
              {tl({ zh: "策略预设", en: "Strategy presets" })}
            </Button>
          </div>
        }
      />

      <div className="flex flex-col gap-6 lg:flex-row">
        {/* 历史侧栏:DOM 首位 = lg+ 左列并吸附;移动端 order 排到主列之后 */}
        <div className="order-2 w-full shrink-0 lg:order-1 lg:w-80 lg:self-start lg:sticky lg:top-16">
          <MasterList
            title={tl({ zh: "回测历史", en: "Backtest History" })}
            count={history?.length}
            toolbar={
              <p className="text-xs text-muted-foreground/70">
                {tl({
                  zh: "只收录成功完成的回测;失败或被拒的任务不写入历史,可在「任务中心」查看。",
                  en: "Only successfully completed backtests are recorded; failed or rejected jobs are not listed here and can be viewed in the Task Center.",
                })}
              </p>
            }
          >
            {history && history.length === 0 && (
              <p className="text-xs text-muted-foreground/70">{tl({ zh: "暂无历史记录", en: "No history yet" })}</p>
            )}
            {history?.map((h) => (
              <HistoryCard
                key={h.id}
                item={h}
                active={h.id === activeHistoryId}
                loading={loadHistory.isPending && loadHistory.variables === h.id}
                onClick={() => loadHistory.mutate(h.id)}
                onDelete={() => setDeleteTarget(h)}
              />
            ))}
          </MasterList>
        </div>

        {/* 主列:结果优先,其后是折叠的配置卡与空态引导 */}
        <div className="order-1 flex min-w-0 flex-1 flex-col gap-6 lg:order-2">
          {resultsSection}
          {configCard}
          {!m && (
            <EmptyState
              title={
                backtestRunning
                  ? tl({ zh: "回测运行中…", en: "Backtest running…" })
                  : tl({ zh: "尚无回测结果", en: "No backtest result yet" })
              }
              description={
                backtestRunning
                  ? tl({
                      zh: `任务已进入统一队列${backtestJob?.phase ? `（${backtestJob.phase}）` : ""},完成后结果会自动展示;可在「任务中心」跟踪进度。`,
                      en: `The job is in the unified queue${backtestJob?.phase ? ` (${backtestJob.phase})` : ""}; the result will appear here when it completes. Track progress in the Task Center.`,
                    })
                  : tl({
                      zh: "展开「回测配置」设置参数后点击「运行回测」,或从左侧回测历史载入一次运行。",
                      en: "Expand Backtest Configuration, set parameters and press \"Run backtest\", or load a previous run from the history list.",
                    })
              }
            />
          )}
        </div>
      </div>

      {/* 删除二次确认:取消不发请求 */}
      <Dialog open={!!deleteTarget} onOpenChange={(v) => !v && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{tl({ zh: "确认删除回测记录", en: "Delete backtest record" })}</DialogTitle>
            <DialogDescription>
              {tl({
                zh: "删除后该记录不再出现在回测历史中,行情缓存与数据发布不受影响。",
                en: "The record will no longer appear in backtest history; bar caches and data releases are unaffected.",
              })}
            </DialogDescription>
          </DialogHeader>
          {deleteTarget && (
            <div className="space-y-2 rounded-lg bg-muted/50 p-3 text-sm">
              <div className="flex justify-between">
                <span className="text-muted-foreground">{tl({ zh: "策略", en: "Strategy" })}</span>
                <span className="font-mono">{deleteTarget.strategy}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">{tl({ zh: "标的数", en: "Symbols" })}</span>
                <span className="tabular-nums">{deleteTarget.symbols.length}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">{tl({ zh: "区间", en: "Window" })}</span>
                <span className="tabular-nums">
                  {deleteTarget.start.slice(0, 10)} ~ {deleteTarget.end.slice(0, 10)}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">{tl({ zh: "创建时间", en: "Created" })}</span>
                <span className="tabular-nums">{new Date(deleteTarget.created_at).toLocaleString()}</span>
              </div>
            </div>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setDeleteTarget(null)}
              disabled={deleteHistory.isPending}
            >
              {tl({ zh: "取消", en: "Cancel" })}
            </Button>
            <Button
              variant="destructive"
              onClick={() => deleteTarget && deleteHistory.mutate(deleteTarget.id)}
              disabled={deleteHistory.isPending}
            >
              {deleteHistory.isPending
                ? tl({ zh: "删除中…", en: "Deleting…" })
                : tl({ zh: "确认删除", en: "Delete" })}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Sheet open={presetsOpen} onOpenChange={setPresetsOpen}>
        <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-2xl">
          <SheetHeader>
            <SheetTitle>{tl({ zh: "策略预设", en: "Strategy presets" })}</SheetTitle>
            <SheetDescription>
              {tl({
                zh: "内置策略的参数预设库;选择预设后点「带入回测」即可填充到当前配置。",
                en: "Parameter presets for built-in strategies; pick one and press \"Load into backtest\" to fill the current configuration.",
              })}
            </SheetDescription>
          </SheetHeader>
          <div className="pb-6">
            <StrategyPresetsPanel onLoadedPreset={() => setPresetsOpen(false)} />
          </div>
        </SheetContent>
      </Sheet>
    </div>
  );
}

// ---- Symbol selector with search + filter + watchlist ----
function SymbolSelector({
  searchQuery,
  setSearchQuery,
  marketFilter,
  setMarketFilter,
  typeFilter,
  setTypeFilter,
  candidates,
  totalCount,
  page,
  pageSize,
  onPageChange,
  selectedSymbols,
  toggleSymbol,
  selectAllCandidates,
  clearSelection,
  onSymbolsLoaded,
}: {
  searchQuery: string;
  setSearchQuery: (v: string) => void;
  marketFilter: string;
  setMarketFilter: (v: string) => void;
  typeFilter: string;
  setTypeFilter: (v: string) => void;
  candidates: { code: string; name: string; market: string; instrument_type: string }[];
  totalCount: number;
  page: number;
  pageSize: number;
  onPageChange: (p: number) => void;
  selectedSymbols: string[];
  toggleSymbol: (code: string) => void;
  selectAllCandidates: { mutate: () => void; isPending: boolean };
  clearSelection: () => void;
  onSymbolsLoaded: (codes: string[]) => void;
}) {
  const { tl } = useT();
  const qc = useQueryClient();
  const [showWatchlist, setShowWatchlist] = useState(false);
  const [newWlName, setNewWlName] = useState("");

  const { data: watchlists } = useQuery({
    queryKey: ["watchlists"],
    queryFn: api.getWatchlists,
    enabled: showWatchlist,
  });

  const createWl = useMutation({
    mutationFn: (name: string) => api.createWatchlist({ name }),
    onSuccess: (wl) => {
      if (selectedSymbols.length > 0) {
        return api.addWatchlistSymbols(wl.id, selectedSymbols);
      }
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["watchlists"] });
      setNewWlName("");
    },
  });

  const loadWl = useMutation({
    mutationFn: (id: number) => api.getWatchlist(id),
    onSuccess: (detail) => onSymbolsLoaded(detail.symbols),
  });

  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));
  const rangeStart = totalCount > 0 ? page * pageSize + 1 : 0;
  const rangeEnd = Math.min((page + 1) * pageSize, totalCount);

  // 选中的标的中,当前页有几个
  const currentPageCodes = new Set(candidates.map((c) => c.code));
  const selectedOnPage = selectedSymbols.filter((c) => currentPageCodes.has(c)).length;

  return (
    <div className="mt-4 border rounded-lg p-4 bg-background">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-1 text-sm font-medium text-muted-foreground">
          <span>
            {tl({ zh: "标的选择", en: "Symbol Selection" })}{" "}
            <span className="font-normal text-muted-foreground/70">({selectedSymbols.length}{tl({ zh: " 个)", en: " selected)" })}</span>
          </span>
          <InfoHint content={INFO_HINTS.backtest.symbols} />
        </div>
        <div className="flex gap-2">
          <SelectAllResultsButton
            totalCount={totalCount}
            isPending={selectAllCandidates.isPending}
            onSelectAll={() => selectAllCandidates.mutate()}
          />
          {selectedSymbols.length > 0 && (
            <button onClick={clearSelection} className="text-xs text-muted-foreground hover:underline">
              {tl({ zh: "清空", en: "Clear" })}
            </button>
          )}
          <button onClick={() => setShowWatchlist((v) => !v)} className="text-xs text-primary hover:underline">
            {tl({ zh: "标的组", en: "Watchlists" })}
          </button>
        </div>
      </div>

      {/* Search + filter row */}
      <div className="mb-2 flex flex-wrap gap-2">
        <label className="flex min-w-0 flex-1 items-center gap-2">
          <span className="sr-only">{tl({ zh: "搜索代码/名称", en: "Search code or name" })}</span>
          <input
            placeholder={tl({ zh: "搜索代码/名称(至少2字符)...", en: "Search code or name (min 2 chars)..." })}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="min-w-0 flex-1 rounded-md border border-input bg-card px-3 py-1.5 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-1 focus-visible:ring-ring"
          />
        </label>
        <label className="flex items-center gap-2">
          <span className="sr-only">{tl({ zh: "市场筛选", en: "Market filter" })}</span>
          <select value={marketFilter} onChange={(e) => setMarketFilter(e.target.value)} className="rounded-md border border-input bg-card px-2 py-1.5 text-sm text-foreground">
            {MARKETS.map((m) => (<option key={m.value} value={m.value}>{tl(m.label)}</option>))}
          </select>
        </label>
        <label className="flex items-center gap-2">
          <span className="sr-only">{tl({ zh: "类型筛选", en: "Type filter" })}</span>
          <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} className="rounded-md border border-input bg-card px-2 py-1.5 text-sm text-foreground">
            {TYPES.map((t) => (<option key={t.value} value={t.value}>{tl(t.label)}</option>))}
          </select>
        </label>
      </div>

      {/* Candidate list */}
      <div className="max-h-48 overflow-y-auto border rounded bg-card">
        {candidates.length === 0 && (
          <p className="text-xs text-muted-foreground/70 p-3 text-center">
            {searchQuery.length >= 2 ? tl({ zh: "无匹配结果", en: "No matching results" }) : tl({ zh: "输入搜索或选择筛选条件", en: "Enter a search or pick a filter" })}
          </p>
        )}
        {candidates.map((c) => {
          const checked = selectedSymbols.includes(c.code);
          return (
            <label key={c.code} className="flex items-center gap-2 px-3 py-1.5 hover:bg-accent cursor-pointer text-sm">
              <input type="checkbox" checked={checked} onChange={() => toggleSymbol(c.code)} />
              <span className="font-mono text-xs">{c.code}</span>
              <span className="text-muted-foreground text-xs truncate">{c.name}</span>
            </label>
          );
        })}
      </div>

      {/* Pagination */}
      {totalCount > pageSize && (
        <div className="flex items-center justify-between mt-2 text-xs text-muted-foreground">
          <span>
            {tl({ zh: `第 ${rangeStart}-${rangeEnd} 条 / 共 ${totalCount} 条`, en: `${rangeStart}-${rangeEnd} of ${totalCount}` })}
            {selectedOnPage > 0 && <span className="ml-2 text-primary">{tl({ zh: `本页已选 ${selectedOnPage}`, en: `${selectedOnPage} selected on this page` })}</span>}
          </span>
          <div className="flex gap-1">
            <button
              onClick={() => onPageChange(page - 1)}
              disabled={page === 0}
              className="px-2 py-0.5 border rounded hover:bg-secondary disabled:opacity-30"
            >
              {tl({ zh: "上一页", en: "Prev" })}
            </button>
            <span className="px-1 py-0.5">{page + 1}/{totalPages}</span>
            <button
              onClick={() => onPageChange(page + 1)}
              disabled={page + 1 >= totalPages}
              className="px-2 py-0.5 border rounded hover:bg-secondary disabled:opacity-30"
            >
              {tl({ zh: "下一页", en: "Next" })}
            </button>
          </div>
        </div>
      )}
      {totalCount > 0 && totalCount <= pageSize && (
        <div className="mt-2 text-xs text-muted-foreground/70">{tl({ zh: `共 ${totalCount} 条`, en: `${totalCount} total` })}{selectedOnPage > 0 && tl({ zh: ` · 已选 ${selectedSymbols.length}`, en: ` · ${selectedSymbols.length} selected` })}</div>
      )}

      {/* Selected symbols summary */}
      {selectedSymbols.length > 0 && (
        <div className="mt-2">
          {selectedSymbols.length <= 50 ? (
            <div className="flex flex-wrap gap-1">
              {selectedSymbols.map((code) => (
                <span key={code} className="inline-flex items-center gap-1 rounded-md bg-primary/15 px-2 py-0.5 font-mono text-xs text-primary">
                  {code}
                  <button onClick={() => toggleSymbol(code)} className="text-primary hover:text-primary">×</button>
                </span>
              ))}
            </div>
          ) : (
            <div className="text-xs text-muted-foreground">
              {tl({ zh: `已选 ${selectedSymbols.length} 个标的`, en: `${selectedSymbols.length} symbols selected` })}
              <button onClick={clearSelection} className="ml-2 text-destructive hover:underline">{tl({ zh: "清空", en: "Clear" })}</button>
            </div>
          )}
        </div>
      )}

      {/* Watchlist panel */}
      {showWatchlist && (
        <div className="mt-3 border-t pt-3 space-y-2">
          <div className="flex gap-1">
            <input
              placeholder={tl({ zh: "新标的组名称...", en: "New watchlist name..." })}
              value={newWlName}
              onChange={(e) => setNewWlName(e.target.value)}
              className="flex-1 border border-input bg-card text-foreground rounded px-2 py-1 text-sm placeholder:text-muted-foreground focus-visible:ring-1 focus-visible:ring-ring outline-none"
            />
            <button
              onClick={() => newWlName.trim() && createWl.mutate(newWlName.trim())}
              disabled={!newWlName.trim() || createWl.isPending}
              className="text-xs bg-primary text-primary-foreground rounded px-3 py-1 hover:bg-primary/90 disabled:opacity-50"
            >
              {tl({ zh: "存当前选择", en: "Save selection" })}
            </button>
          </div>
          {watchlists?.map((wl) => (
            <div key={wl.id} className="flex items-center justify-between text-sm bg-card border rounded px-2 py-1">
              <span className="truncate">
                {wl.name} <span className="text-muted-foreground/70">({wl.item_count})</span>
              </span>
              <div className="flex gap-2 shrink-0">
                <button onClick={() => loadWl.mutate(wl.id)} className="text-xs text-primary hover:underline">{tl({ zh: "载入", en: "Load" })}</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---- History card ----
function HistoryCard({
  item,
  active,
  loading,
  onClick,
  onDelete,
}: {
  item: BacktestHistoryItem;
  active: boolean;
  loading: boolean;
  onClick: () => void;
  onDelete: () => void;
}) {
  const { tl } = useT();
  const ret = item.metrics?.total_return;
  const tradeCount = item.metrics?.trade_count;
  const sharpe = item.metrics?.sharpe_ratio;
  const summaryParts: string[] = [];
  if (tradeCount !== undefined) {
    summaryParts.push(tl({ zh: `成交 ${tradeCount} 笔`, en: `${tradeCount} trades` }));
  }
  if (sharpe !== undefined) {
    summaryParts.push(tl({ zh: `夏普 ${sharpe.toFixed(2)}`, en: `Sharpe ${sharpe.toFixed(2)}` }));
  }
  return (
    <MasterListItem
      selected={active}
      onClick={() => {
        if (!loading) onClick();
      }}
    >
      <div className="flex items-start justify-between">
        <div className="min-w-0">
          <div className="text-xs font-mono text-muted-foreground truncate">{item.strategy}</div>
          <div className="text-xs text-muted-foreground/70 mt-0.5">
            {tl({ zh: `${item.symbols.length} 标的 · ${item.start.slice(0, 10)} ~ ${item.end.slice(0, 10)}`, en: `${item.symbols.length} symbols · ${item.start.slice(0, 10)} ~ ${item.end.slice(0, 10)}` })}
          </div>
          {summaryParts.length > 0 && (
            <div className="mt-0.5 text-xs tabular-nums text-muted-foreground/70">
              {summaryParts.join(" · ")}
            </div>
          )}
          {ret !== undefined && (
            <div className={`text-sm font-bold mt-1 ${ret >= 0 ? "text-success" : "text-destructive"}`}>
              {(ret * 100).toFixed(2)}%
            </div>
          )}
        </div>
        <div className="flex flex-col items-end gap-1 shrink-0">
          <span
            className="text-xs tabular-nums text-muted-foreground"
            title={new Date(item.created_at).toLocaleString()}
          >
            {formatHistoryTime(item.created_at)}
          </span>
          <span
            role="button"
            tabIndex={0}
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                e.stopPropagation();
                onDelete();
              }
            }}
            className="cursor-pointer text-xs text-muted-foreground hover:text-destructive"
          >
            {tl({ zh: "删除", en: "Delete" })}
          </span>
        </div>
      </div>
      {loading && <div className="text-xs text-primary mt-1">{tl({ zh: "加载中...", en: "Loading..." })}</div>}
    </MasterListItem>
  );
}

function MetricCard({
  label,
  value,
  positive,
}: {
  label: string;
  value: string;
  positive?: boolean;
}) {
  const color =
    positive === undefined ? "" : positive ? "text-success" : "text-destructive";
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="text-muted-foreground text-sm">{label}</div>
      <div className={`text-xl font-bold mt-1 ${color}`}>{value}</div>
    </div>
  );
}
