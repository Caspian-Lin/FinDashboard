import { useState, useMemo, useCallback, useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useLocation, useSearchParams } from "react-router-dom";
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
} from "../lib/api";
import { INFO_HINTS } from "../lib/infoHints";

const MARKETS = [
  { value: "", label: "全部市场" },
  { value: "a_share", label: "A股" },
  { value: "hk", label: "港股" },
  { value: "us", label: "美股" },
];
const TYPES = [
  { value: "", label: "全部类型" },
  { value: "stock", label: "股票" },
  { value: "etf", label: "ETF" },
];

export default function Backtest() {
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
    onSuccess: (data) => {
      setStrategyErrors({});
      setActiveResult(data);
      setActiveHistoryId(data.run_id);
      qc.invalidateQueries({ queryKey: ["backtest-history"] });
    },
    onError: (error) => setStrategyErrors(strategyFieldErrorsFrom(error)),
  });

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
    onSuccess: () => qc.invalidateQueries({ queryKey: ["backtest-history"] }),
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

  const changeStrategy = (kind: string) => {
    const definition = backtestStrategies.find((item) => item.kind === kind);
    setStrategy(kind);
    setStrategyParams(definition ? defaultStrategyParams(definition) : {});
    setStrategyErrors({});
    runBacktest.reset();
    appliedConfig.current = `manual:${kind}`;
  };

  const startBacktest = () => {
    if (!strategyDefinition) return;
    const errors = validateStrategyParams(strategyDefinition, strategyParams);
    setStrategyErrors(errors);
    if (Object.keys(errors).length > 0) return;
    runBacktest.mutate(
      normalizeStrategyParams(strategyDefinition, strategyParams),
    );
  };

  return (
    <div className="flex gap-6">
      {/* Main column */}
      <div className="flex-1 min-w-0">
        <h1 className="text-2xl font-bold mb-6">回测</h1>

        {/* Config form */}
        <div className="bg-card rounded-lg shadow p-5 mb-6">
          <h2 className="text-lg font-semibold mb-4">回测配置</h2>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
            <div>
              <HintLabel htmlFor="backtest-strategy" hint={INFO_HINTS.backtest.strategy}>
                策略
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
                开始
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
                结束
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
                初始资金(¥)
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
                已载入策略预设：<strong>{requestedPreset.name}</strong>
              </span>
              <span className="text-xs text-primary">仅用于本次回测配置</span>
            </div>
          )}
          {requestedPreset && !requestedPresetDefinition && (
            <p className="mt-4 rounded-lg bg-warning/10 px-3 py-2 text-sm text-amber-800">
              预设“{requestedPreset.name}”依赖实时时钟事件，当前回测引擎无法运行。
            </p>
          )}

          {strategyDefinition && (
            <div className="mt-4 border-t border-border pt-4">
              <div className="mb-3 flex items-center gap-1">
                <h3 className="text-sm font-semibold text-muted-foreground">策略参数</h3>
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
                disabled={runBacktest.isPending}
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
              费用参数(佣金 / 印花税 / 滑点)
            </summary>
            <div className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <div>
                <HintLabel
                  htmlFor="backtest-commission-rate"
                  hint={INFO_HINTS.backtest.commissionRate}
                  labelClassName="text-xs text-muted-foreground"
                >
                  佣金率(万N)
                </HintLabel>
                <input
                  id="backtest-commission-rate"
                  type="number"
                  step="0.0001"
                  value={commissionRate}
                  onChange={(e) => setCommissionRate(e.target.value)}
                  className="w-full border border-input bg-card text-foreground rounded px-2 py-1.5 text-sm"
                  placeholder="0.0003 = 万3"
                />
              </div>
              <div>
                <HintLabel
                  htmlFor="backtest-minimum-commission"
                  hint={INFO_HINTS.backtest.minimumCommission}
                  labelClassName="text-xs text-muted-foreground"
                >
                  最低佣金(¥)
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
                  印花税(万N,卖出)
                </HintLabel>
                <input
                  id="backtest-stamp-tax"
                  type="number"
                  step="0.0001"
                  value={stampTaxRate}
                  onChange={(e) => setStampTaxRate(e.target.value)}
                  className="w-full border border-input bg-card text-foreground rounded px-2 py-1.5 text-sm"
                  placeholder="0.0005 = 万5, ETF 填 0"
                />
              </div>
              <div>
                <HintLabel
                  htmlFor="backtest-slippage"
                  hint={INFO_HINTS.backtest.slippage}
                  labelClassName="text-xs text-muted-foreground"
                >
                  滑点(bps)
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
              ETF 免印花税(填 0);股票卖出收万5。佣金 = max(成交额 × 佣金率, 最低佣金)。
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

          <button
            onClick={startBacktest}
            disabled={runBacktest.isPending || selectedSymbols.length === 0}
            className="mt-4 bg-primary text-white rounded px-6 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
          >
            {runBacktest.isPending ? "回测中..." : "运行回测"}
          </button>
          {runBacktest.error && (
            <p className="mt-2 text-sm text-destructive">
              {(runBacktest.error as Error).message}
            </p>
          )}
        </div>

        {/* Results */}
        {m && (
          <>
            <div className="grid grid-cols-4 gap-4 mb-6">
              <MetricCard label="总收益" value={`${(m.total_return * 100).toFixed(2)}%`} positive={m.total_return >= 0} />
              <MetricCard label="年化" value={`${(m.annualized_return * 100).toFixed(2)}%`} positive={m.annualized_return >= 0} />
              <MetricCard label="夏普" value={m.sharpe_ratio.toFixed(2)} />
              <MetricCard label="最大回撤" value={`${(m.max_drawdown * 100).toFixed(2)}%`} positive={false} />
              <MetricCard label="胜率" value={`${(m.win_rate * 100).toFixed(1)}%`} />
              <MetricCard label="交易次数" value={String(m.trade_count)} />
              <MetricCard label="换手率" value={m.turnover.toFixed(2)} />
              <MetricCard label="超额收益" value={`${(m.excess_return * 100).toFixed(2)}%`} positive={m.excess_return >= 0} />
            </div>

            {result.selection_snapshots.length > 0 && (
              <div className="mb-6 rounded-lg border border-border bg-card p-5 shadow-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h2 className="text-lg font-semibold text-foreground">候选池审计</h2>
                    <p className="mt-1 text-xs text-muted-foreground">
                      因子版本 {result.factor_version} · {result.selection_snapshots.length} 个日快照
                    </p>
                  </div>
                  <span className="rounded-full bg-secondary px-2.5 py-1 text-xs text-muted-foreground">
                    数据版本已归档
                  </span>
                </div>
                <div className="mt-4 max-h-56 divide-y divide-slate-100 overflow-y-auto">
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
                            ? "text-emerald-700"
                            : "text-amber-700"
                        }
                      >
                        {snapshot.status === "published" ? "已发布" : "跳过调仓"}
                      </span>
                      <span className="text-foreground">
                        {snapshot.status === "published"
                          ? `${snapshot.selected_symbols.length} 个标的`
                          : snapshot.skip_reason}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {result.equity_curve.length > 0 && (
              <div className="bg-card rounded-lg shadow p-5 mb-6">
                <h2 className="text-lg font-semibold mb-4">权益曲线</h2>
                <ResponsiveContainer width="100%" height={350}>
                  <LineChart data={result.equity_curve}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
                    <XAxis dataKey="date" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                    <YAxis tick={{ fontSize: 11 }} />
                    <Tooltip
                      formatter={(v) =>
                        `¥${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
                      }
                    />
                    <Legend />
                    <Line type="monotone" dataKey="equity" stroke="#2563eb" name="策略" dot={false} strokeWidth={2} />
                    {result.equity_curve.some((p) => p.benchmark !== null) && (
                      <Line type="monotone" dataKey="benchmark" stroke="#9ca3af" name="买入持有" dot={false} strokeWidth={1.5} strokeDasharray="5 5" />
                    )}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}

            {result.fills.length > 0 && (
              <div className="bg-card rounded-lg shadow">
                <div className="px-5 py-3 border-b">
                  <h2 className="text-lg font-semibold">交易明细 ({result.fills.length})</h2>
                </div>
                <table className="w-full text-sm">
                  <thead className="bg-background text-muted-foreground">
                    <tr>
                      <th className="px-4 py-2 text-left">日期</th>
                      <th className="px-4 py-2 text-left">标的</th>
                      <th className="px-4 py-2 text-left">方向</th>
                      <th className="px-4 py-2 text-right">数量</th>
                      <th className="px-4 py-2 text-right">价格</th>
                      <th className="px-4 py-2 text-right">佣金</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.fills.map((f, i) => (
                      <tr key={i} className="border-t">
                        <td className="px-4 py-2 text-muted-foreground">{f.date}</td>
                        <td className="px-4 py-2 font-mono">{f.symbol}</td>
                        <td className={`px-4 py-2 ${f.side === "buy" ? "text-red-500" : "text-green-500"}`}>
                          {f.side === "buy" ? "买入" : "卖出"}
                        </td>
                        <td className="px-4 py-2 text-right">{f.quantity}</td>
                        <td className="px-4 py-2 text-right font-mono">{Number(f.price).toFixed(2)}</td>
                        <td className="px-4 py-2 text-right text-muted-foreground">{Number(f.commission).toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>

      {/* History sidebar */}
      <div className="w-72 shrink-0">
        <h2 className="text-sm font-semibold text-muted-foreground mb-3">回测历史</h2>
        <div className="space-y-2">
          {history && history.length === 0 && (
            <p className="text-xs text-muted-foreground/70">暂无历史记录</p>
          )}
          {history?.map((h) => (
            <HistoryCard
              key={h.id}
              item={h}
              active={h.id === activeHistoryId}
              loading={loadHistory.isPending && loadHistory.variables === h.id}
              onClick={() => loadHistory.mutate(h.id)}
              onDelete={() => deleteHistory.mutate(h.id)}
            />
          ))}
        </div>
      </div>
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
            标的选择{" "}
            <span className="font-normal text-muted-foreground/70">({selectedSymbols.length} 个)</span>
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
              清空
            </button>
          )}
          <button onClick={() => setShowWatchlist((v) => !v)} className="text-xs text-primary hover:underline">
            标的组
          </button>
        </div>
      </div>

      {/* Search + filter row */}
      <div className="flex gap-2 mb-2">
        <input
          placeholder="搜索代码/名称(至少2字符)..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="flex-1 border border-input bg-card text-foreground rounded px-3 py-1.5 text-sm placeholder:text-muted-foreground focus-visible:ring-1 focus-visible:ring-ring outline-none"
        />
        <select value={marketFilter} onChange={(e) => setMarketFilter(e.target.value)} className="border border-input bg-card text-foreground rounded px-2 py-1.5 text-sm">
          {MARKETS.map((m) => (<option key={m.value} value={m.value}>{m.label}</option>))}
        </select>
        <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} className="border border-input bg-card text-foreground rounded px-2 py-1.5 text-sm">
          {TYPES.map((t) => (<option key={t.value} value={t.value}>{t.label}</option>))}
        </select>
      </div>

      {/* Candidate list */}
      <div className="max-h-48 overflow-y-auto border rounded bg-card">
        {candidates.length === 0 && (
          <p className="text-xs text-muted-foreground/70 p-3 text-center">
            {searchQuery.length >= 2 ? "无匹配结果" : "输入搜索或选择筛选条件"}
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
            第 {rangeStart}-{rangeEnd} 条 / 共 {totalCount} 条
            {selectedOnPage > 0 && <span className="ml-2 text-primary">本页已选 {selectedOnPage}</span>}
          </span>
          <div className="flex gap-1">
            <button
              onClick={() => onPageChange(page - 1)}
              disabled={page === 0}
              className="px-2 py-0.5 border rounded hover:bg-secondary disabled:opacity-30"
            >
              上一页
            </button>
            <span className="px-1 py-0.5">{page + 1}/{totalPages}</span>
            <button
              onClick={() => onPageChange(page + 1)}
              disabled={page + 1 >= totalPages}
              className="px-2 py-0.5 border rounded hover:bg-secondary disabled:opacity-30"
            >
              下一页
            </button>
          </div>
        </div>
      )}
      {totalCount > 0 && totalCount <= pageSize && (
        <div className="mt-2 text-xs text-muted-foreground/70">共 {totalCount} 条{selectedOnPage > 0 && ` · 已选 ${selectedSymbols.length}`}</div>
      )}

      {/* Selected symbols summary */}
      {selectedSymbols.length > 0 && (
        <div className="mt-2">
          {selectedSymbols.length <= 50 ? (
            <div className="flex flex-wrap gap-1">
              {selectedSymbols.map((code) => (
                <span key={code} className="inline-flex items-center gap-1 bg-blue-100 text-primary rounded px-2 py-0.5 text-xs font-mono">
                  {code}
                  <button onClick={() => toggleSymbol(code)} className="text-primary hover:text-primary">×</button>
                </span>
              ))}
            </div>
          ) : (
            <div className="text-xs text-muted-foreground">
              已选 {selectedSymbols.length} 个标的
              <button onClick={clearSelection} className="ml-2 text-red-500 hover:underline">清空</button>
            </div>
          )}
        </div>
      )}

      {/* Watchlist panel */}
      {showWatchlist && (
        <div className="mt-3 border-t pt-3 space-y-2">
          <div className="flex gap-1">
            <input
              placeholder="新标的组名称..."
              value={newWlName}
              onChange={(e) => setNewWlName(e.target.value)}
              className="flex-1 border border-input bg-card text-foreground rounded px-2 py-1 text-sm placeholder:text-muted-foreground focus-visible:ring-1 focus-visible:ring-ring outline-none"
            />
            <button
              onClick={() => newWlName.trim() && createWl.mutate(newWlName.trim())}
              disabled={!newWlName.trim() || createWl.isPending}
              className="text-xs bg-primary text-primary-foreground rounded px-3 py-1 hover:bg-primary/90 disabled:opacity-50"
            >
              存当前选择
            </button>
          </div>
          {watchlists?.map((wl) => (
            <div key={wl.id} className="flex items-center justify-between text-sm bg-card border rounded px-2 py-1">
              <span className="truncate">
                {wl.name} <span className="text-muted-foreground/70">({wl.item_count})</span>
              </span>
              <div className="flex gap-2 shrink-0">
                <button onClick={() => loadWl.mutate(wl.id)} className="text-xs text-primary hover:underline">载入</button>
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
  const ret = item.metrics?.total_return;
  return (
    <div
      className={`border rounded-lg p-3 cursor-pointer transition ${
        active ? "border-primary bg-primary/10" : "bg-card hover:border-primary/50"
      }`}
      onClick={onClick}
    >
      <div className="flex items-start justify-between">
        <div className="min-w-0">
          <div className="text-xs font-mono text-muted-foreground truncate">{item.strategy}</div>
          <div className="text-xs text-muted-foreground/70 mt-0.5">
            {item.symbols.length} 标的 · {item.start.slice(0, 10)} ~ {item.end.slice(0, 10)}
          </div>
          {ret !== undefined && (
            <div className={`text-sm font-bold mt-1 ${ret >= 0 ? "text-success" : "text-destructive"}`}>
              {(ret * 100).toFixed(2)}%
            </div>
          )}
        </div>
        <div className="flex flex-col items-end gap-1 shrink-0">
          <span className="text-xs text-muted-foreground">
            {new Date(item.created_at).toLocaleDateString("zh-CN", { month: "short", day: "numeric" })}
          </span>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
            className="text-xs text-muted-foreground hover:text-destructive"
          >
            删除
          </button>
        </div>
      </div>
      {loading && <div className="text-xs text-primary mt-1">加载中...</div>}
    </div>
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
    <div className="bg-card rounded-lg shadow p-4">
      <div className="text-muted-foreground text-sm">{label}</div>
      <div className={`text-xl font-bold mt-1 ${color}`}>{value}</div>
    </div>
  );
}
