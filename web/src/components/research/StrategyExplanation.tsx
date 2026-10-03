import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LoadingState, ErrorState } from "@/components/ui/states";
import { runSchedule } from "@/lib/research-semantics";
import { workspaceApi } from "@/lib/research-workspace";

const LABELS: Record<string, string> = {
  reject_reason: "拒单原因",
  filled_at: "成交时间",
  research_order_id: "研究订单ID",
  research_fill_id: "研究成交ID",
  metric: "风险指标",
  before_weight: "退出前权重",
  after_weight: "退出后权重",
  current_quantity: "当前数量",
  target_quantity: "目标数量",
  delta_quantity: "调仓数量",
  position_side: "持仓方向",
  markets: "市场",
  asset_classes: "资产类别",
  explicit_symbols: "指定标的",
  min_listing_days: "最短上市天数",
  min_average_amount: "最小平均成交额",
  min_price: "最低价格",
  max_price: "最高价格",
  min_market_cap: "最小市值(冻结数据口径)",
  max_market_cap: "最大市值(冻结数据口径)",
  exclude_st: "排除ST",
  exclude_suspended: "排除停牌",
  exclude_delisted: "排除退市",
  selection_limit: "候选数量上限",
  ranking: "排序筛选",
  missing_data_policy: "缺失数据处理",
  min_data_completeness: "最小完整率",
  required_data_fields: "必需字段",
  excluded_event_types: "排除事件",
  field: "字段",
  direction: "方向",
  node_id: "节点",
  label: "说明",
  kind: "类型",
  operator: "变换",
  source: "因子来源",
  inputs: "输入节点",
  window: "窗口(Bar)",
  weights: "复合权重",
  lag_bars: "滞后Bar",
  outputs: "输出",
  lower_percentile: "缩尾下分位",
  upper_percentile: "缩尾上分位",
  default_action: "未命中动作",
  conflict_policy: "冲突裁决",
  rules: "规则",
  rule_id: "规则ID",
  feature_id: "特征",
  comparator: "条件",
  action: "动作",
  threshold: "阈值",
  validity_bars: "有效Bar",
  priority: "优先级",
  conflict_group: "冲突组",
  rationale: "规则理由",
  allocation_method: "分配方式",
  max_positions: "持仓上限",
  max_target_weight: "目标权重上限",
  target_gross_exposure: "目标总敞口",
  target_net_exposure: "目标净敞口",
  cash_buffer: "现金缓冲",
  turnover_constraint: "换手约束",
  liquidity_participation_limit: "流动性参与率",
  rebalance_threshold: "再平衡带",
  max_weight_per_asset: "单标的硬上限",
  max_weight_per_sleeve: "分组硬上限",
  min_cash_buffer: "最低现金",
  max_leverage: "杠杆上限",
  max_risk_contribution: "风险贡献上限(1表示关闭)",
  rule_type: "退出类型",
  enabled: "启用",
  cooldown_days: "冷却天数",
  target_gross_exposure_after_trigger: "触发后敞口",
  timing: "成交时点",
  commission_rate: "佣金率",
  minimum_commission: "最低佣金(元)",
  sell_tax_rate: "卖出税率",
  slippage_bps: "滑点(bps)",
  max_volume_participation: "成交量参与率",
  enforce_lot_size: "手数约束",
  enforce_price_limits: "涨跌停限制",
  reject_same_bar_fill: "拒绝同Bar成交",
  kind_type: "类型",
  dates: "自定义日期",
  decision_schedule: "决策日历",
  rebalance_frequency: "旧调仓频率",
  params: "冻结参数",
  code_commit: "实现commit",
  series_id: "序列ID",
  content_checksum: "内容checksum",
  integrity: "内容核验",
  symbol: "标的",
  value: "因子值",
  score: "分数",
  weight: "目标权重",
  reason: "原因",
  status: "状态",
  quantity: "数量",
  filled_quantity: "成交数量",
  price: "价格",
  side: "方向",
  constraint: "约束",
  passed: "约束满足",
  before_value: "约束前",
  after_value: "约束后",
  limit: "限值",
  included: "候选纳入",
  reasons: "候选原因",
  execution_at: "执行时间",
  available_at: "信息可用时间",
};
const VALUES: Record<string, string> = {
  equity: "股票",
  a_share: "A股",
  etf: "ETF",
  futures: "期货",
  convertible: "可转债",
  highest_priority: "高优先级规则先行",
  gt: "大于",
  gte: "不小于",
  lt: "小于",
  lte: "不大于",
  next_open: "下一交易日开盘",
  next_close: "下一交易日收盘",
  negate: "取负，翻转原始数值方向",
  cross_section_rank: "截面百分位排名",
  weighted_sum: "加权求和",
  identity: "原值",
  winsorize: "缩尾",
  zscore: "Z分数标准化",
  exclude: "排除缺失标的",
  rank_worst: "缺失排到最末",
  rank_top: "分数排名前列比例",
  rank_bottom: "分数排名末列比例",
  equal_weight: "等权",
  buy: "买入",
  sell: "卖出",
  neutral: "中性",
  higher: "原始偏好较高",
  lower: "原始偏好较低",
  bottom: "由低到高",
  top: "由高到低",
  drawdown_de_risk: "回撤触发降风险",
  portfolio_drawdown_derisk: "组合回撤触发降风险",
  price_stop_loss: "价格止损",
  volatility_stop: "波动止损",
  take_profit: "止盈",
  max_holding_days: "最长持有期",
  cooldown: "冷却",
  rejected: "被拒绝",
  filled: "已成交",
  partial: "部分成交",
  open_long: "买入开仓",
  close_long: "卖出平仓",
};
const STAGES: Record<string, string> = {
  universe: "候选纳入与排除",
  features: "因子与特征值",
  signals: "分数与规则命中",
  targets_before_constraints: "约束前目标",
  constraints: "组合与标的约束",
  targets_after_constraints: "约束后目标",
  risk_exits: "风险规则触发",
  targets_after_risk: "风险退出后目标",
  rebalance_plan: "离散调仓计划",
  orders: "研究订单与拒单",
  fills: "实际研究成交",
  ledger: "成交后持仓",
};
export function EvidenceFields({ value }: { value: unknown }) {
  if (value === null || value === undefined)
    return <span className="text-muted-foreground">未记录</span>;
  if (typeof value !== "object")
    return (
      <span className="break-all">
        {typeof value === "boolean"
          ? value
            ? "是"
            : "否"
          : (VALUES[String(value)] ?? String(value))}
      </span>
    );
  if (Array.isArray(value))
    return (
      <div className="space-y-2">
        {value.map((v, i) => (
          <div key={i}>
            <EvidenceFields value={v} />
          </div>
        ))}
      </div>
    );
  return (
    <dl className="grid grid-cols-[minmax(7rem,1fr)_minmax(0,3fr)] gap-x-4 gap-y-2 text-sm">
      {Object.entries(value).map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted-foreground">{LABELS[k] ?? k}</dt>
          <dd>
            <EvidenceFields value={v} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

const object = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const percent = (value: unknown) =>
  typeof value === "number" ? `${Math.round(value * 10000) / 100}%` : "未记录";
function StrategyOverview({ data }: { data: import("@/lib/research-workspace").Explanation }) {
  const universe = object(data.spec.universe);
  const portfolio = object(data.spec.portfolio_policy);
  const signal = object(data.spec.signal_rules);
  const execution = object(data.effective_policies.execution_model ?? data.spec.execution_model);
  const risk = object(data.effective_policies.risk_exit_policy ?? data.spec.risk_exit_policy);
  const rules = Array.isArray(signal.rules) ? signal.rules.map(object) : [];
  const riskRules = Array.isArray(risk.rules)
    ? risk.rules.map(object).filter((r) => r.enabled)
    : [];
  const calendar = runSchedule({
    manifest: { parameters: data.schedule },
    result: { execution_mode: data.provenance.execution_mode },
  });
  return (
    <section className="space-y-3 rounded-md border border-border p-4">
      <h3 className="font-semibold">策略概览</h3>
      <p className="text-sm">
        候选池：
        {Array.isArray(universe.asset_classes)
          ? universe.asset_classes.map((v) => VALUES[String(v)] ?? String(v)).join("、")
          : "未记录"}
        ；上市至少 {String(universe.min_listing_days ?? "未记录")}{" "}
        天。停牌/ST/退市的排除与缺失数据口径见下方完整规则。
      </p>
      {rules.slice(0, 4).map((r, i) => (
        <p className="text-sm" key={i}>
          买卖条件：{String(r.feature_id ?? "未知特征")} ·{" "}
          {VALUES[String(r.comparator)] ?? String(r.comparator ?? "条件未记录")}{" "}
          {["rank_top", "rank_bottom"].includes(String(r.comparator))
            ? percent(r.threshold)
            : String(r.threshold ?? "阈值未记录")}{" "}
          → {VALUES[String(r.action)] ?? String(r.action ?? "动作未记录")}。
        </p>
      ))}
      {!rules.length && (
        <p className="text-sm text-warning">买卖条件缺少可核验规则，不能只按策略名称推断。</p>
      )}
      <p className="text-sm">
        配权：
        {VALUES[String(portfolio.allocation_method)] ??
          String(portfolio.allocation_method ?? "未记录")}
        ；最多 {String(portfolio.max_positions ?? "未记录")} 个持仓，规格目标总敞口{" "}
        {percent(portfolio.target_gross_exposure)}。
      </p>
      <p className="text-sm">
        风险退出：
        {riskRules.length
          ? riskRules
              .map(
                (r) =>
                  `${VALUES[String(r.rule_type)] ?? String(r.rule_type)}（阈值 ${r.threshold == null ? "按完整规则" : ["price_stop_loss", "take_profit", "portfolio_drawdown_derisk"].includes(String(r.rule_type)) ? percent(r.threshold) : String(r.threshold)}）`,
              )
              .join("；")
          : "未记录启用规则"}
        。阈值是触发条件，不能视为最大亏损保证。
      </p>
      <p className="text-sm">
        决策日历：{calendar.frequency === "未记录" ? "历史日历未记录" : calendar.frequency}
        {calendar.count != null
          ? `，${calendar.count} 次，${calendar.first} 至 ${calendar.last}`
          : ""}
        。成交模型：{VALUES[String(execution.timing)] ?? String(execution.timing ?? "未记录")}。
      </p>
      <p className="text-xs text-muted-foreground">
        此概览读取结构化规则与覆盖值；完整条件、优先级、数量约束和实际成交须在下方核对。规则文字理由可能沿用原规格，覆盖后以结构化阈值为准。
      </p>
    </section>
  );
}

export default function StrategyExplanation({
  runId,
  strategyId,
  version,
}: {
  runId?: string;
  strategyId?: string;
  version?: number;
}) {
  const q = useQuery({
    queryKey: ["strategy-explanation", runId, strategyId, version],
    queryFn: () => workspaceApi.explain(runId, strategyId, version),
  });
  const [date, setDate] = useState("");
  const [symbol, setSymbol] = useState("");
  const [selection, setSelection] = useState<{ business_date: string; symbol: string } | null>(
    null,
  );
  const [offset, setOffset] = useState(0);
  const [directoryOffset, setDirectoryOffset] = useState(0);
  const decisions = useQuery({
    queryKey: ["decision-directory", runId, directoryOffset],
    queryFn: () => workspaceApi.decisions(runId!, { limit: "20", offset: String(directoryOffset) }),
    enabled: !!runId,
  });
  const evidence = useQuery({
    queryKey: ["decision-explanation", runId, selection, offset],
    queryFn: () =>
      workspaceApi.decisions(runId!, { ...selection!, limit: "100", offset: String(offset) }),
    enabled: !!runId && !!selection,
  });
  if (q.isLoading) return <LoadingState />;
  if (q.isError) return <ErrorState message={String(q.error)} onRetry={() => q.refetch()} />;
  const data = q.data;
  if (!data) return null;
  const sections = [
    ["universe", "选什么：候选池与排除条件"],
    ["feature_graph", "怎样打分：因子与变换"],
    ["signal_rules", "何时买卖：阈值与冲突裁决"],
    ["portfolio_policy", "买多少：目标配权"],
    ["risk_exit_policy", "何时退出：风险规则"],
    ["execution_model", "何时成交：执行与费用"],
  ];
  return (
    <div className="space-y-6">
      <div className="border-b border-border pb-4">
        <h2 className="text-lg font-semibold">
          策略说明书 · {String(data.spec.name ?? data.spec.strategy_id ?? "历史策略")}
        </h2>
        <p className="mt-2 max-w-prose text-sm text-muted-foreground">
          规则来自指定版本或本次运行的冻结输入。名称仅是标签；以下变换和买卖条件决定实际行为。
        </p>
        <details className="mt-3">
          <summary className="cursor-pointer text-sm">版本与来源</summary>
          <div className="mt-3">
            <EvidenceFields value={data.provenance} />
          </div>
        </details>
      </div>
      <StrategyOverview data={data} />
      {data.gaps.length > 0 && (
        <div role="status" className="rounded-md border border-border bg-warning/10 p-3 text-sm">
          {data.gaps.map((g) => (
            <p key={g}>{g}</p>
          ))}
        </div>
      )}
      <section>
        <h3 className="mb-3 font-semibold">因子内部参数与实际方向</h3>
        <div className="divide-y divide-border">
          {data.factors.map((f) => (
            <article key={f.node_id} className="space-y-2 py-4">
              <h4 className="font-medium">{f.name}</h4>
              <p className="text-sm">{f.formula ?? "历史公式缺少证据，不使用当前目录替代"}</p>
              <p className="text-sm text-muted-foreground">
                单位：{f.unit} · 因子目录偏好：{VALUES[f.raw_direction] ?? f.raw_direction} ·
                实现版本：
                {f.implementation_version ?? "未核验"} · 窗口：{f.window ?? "未核验"}
              </p>
              {Object.entries(f.effective_direction).map(([output, direction]) => (
                <p key={output} className="text-sm">
                  {output}：{direction}。买卖方向仍需结合信号规则。
                </p>
              ))}
              {Object.values(f.effective_direction).some((d) => d.includes("原值越低")) && (
                <p className="text-sm text-warning">
                  本分支偏好较低原值；名称中的“动量”等标签可能与实际方向不一致。
                </p>
              )}
              <details>
                <summary className="cursor-pointer text-sm">冻结参数与版本证据</summary>
                <div className="mt-2">
                  <EvidenceFields value={f.sources.length ? f.sources : f.parameters} />
                </div>
              </details>
            </article>
          ))}
        </div>
      </section>
      {sections.map(([key, title]) => (
        <details key={key} className="border-t border-border pt-3">
          <summary className="cursor-pointer font-medium">{title}</summary>
          <div className="mt-4">
            <EvidenceFields value={data.spec[key]} />
          </div>
        </details>
      ))}
      <details className="border-t border-border pt-3">
        <summary className="cursor-pointer font-medium">本次运行覆盖与生效值</summary>
        <p className="my-3 text-sm text-muted-foreground">
          规格原值与运行覆盖分别保留。覆盖值复用当前执行端的消费契约解析；历史版本是否实际执行须核对当时账本。无法解析的记录在缺口中注明。
        </p>
        <EvidenceFields value={data.effective_policies} />
        <details className="mt-3">
          <summary className="cursor-pointer text-sm">原始覆盖参数</summary>
          <EvidenceFields value={data.overrides} />
        </details>
      </details>
      <section className="border-t border-border pt-4">
        <h3 className="font-semibold">收益机制与失效假设</h3>
        {data.mechanism_hypotheses.map((h) => (
          <div key={h.source} className="mt-3 max-w-prose space-y-2 text-sm">
            <p>{h.claim}</p>
            <p>证据等级：外部机制假设。交易对手：{h.counterparty}</p>
            <p>{h.failure}</p>
            <a className="text-primary underline" href={h.source} target="_blank" rel="noreferrer">
              机制研究来源
            </a>
          </div>
        ))}
        {data.mechanism_hypotheses.length === 0 && (
          <p className="mt-2 text-sm">
            此版本未归档经核验的经济解释，需在课题轮次中记录假设、来源和替代解释。
          </p>
        )}
        {data.warnings.map((w) => (
          <p key={w} className="mt-2 max-w-prose text-sm text-muted-foreground">
            {w}
          </p>
        ))}
      </section>
      {runId && (
        <section className="border-t border-border pt-4">
          <h3 className="font-semibold">某次实际决策</h3>
          <p className="my-2 text-sm text-muted-foreground">
            选择日期和标的，沿因子值、信号、约束、目标到成交核对。只读取该标的的有界证据。空阶段可能未归档，不能根据空白推断是否成交或拒单。
          </p>
          <form
            className="flex flex-wrap items-end gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              setOffset(0);
              setSelection({ business_date: date, symbol });
            }}
          >
            <div>
              <Label htmlFor="decision-date">决策日期</Label>
              <Input
                id="decision-date"
                type="date"
                required
                value={date}
                onChange={(e) => setDate(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="decision-symbol">标的代码</Label>
              <Input
                id="decision-symbol"
                required
                value={symbol}
                placeholder="000001.SZ"
                onChange={(e) => setSymbol(e.target.value)}
              />
            </div>
            <Button type="submit">查看决策</Button>
          </form>
          {decisions.data && (
            <div className="my-3 flex flex-wrap gap-2">
              {decisions.data.items.map((d) => (
                <Button
                  key={d.decision_id}
                  variant="outline"
                  size="sm"
                  onClick={() => setDate(d.business_date ?? "")}
                >
                  {d.business_date ?? d.decision_id}
                </Button>
              ))}
            </div>
          )}
          {decisions.isError && (
            <ErrorState message={String(decisions.error)} onRetry={() => decisions.refetch()} />
          )}
          {decisions.data && (
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={directoryOffset === 0}
                onClick={() => setDirectoryOffset(Math.max(0, directoryOffset - 20))}
              >
                前一组决策日
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={!decisions.data.has_more}
                onClick={() => setDirectoryOffset(directoryOffset + 20)}
              >
                后一组决策日
              </Button>
            </div>
          )}
          {evidence.isLoading && <LoadingState />}
          {evidence.isError && (
            <ErrorState message={String(evidence.error)} onRetry={() => evidence.refetch()} />
          )}
          {evidence.data && (
            <div className="mt-4 space-y-4">
              {evidence.data.items.length === 0 && (
                <p role="status">
                  此日期/标的没有可用证据，请核对日期和代码。不能据此推断成交或拒单原因。
                </p>
              )}
              {evidence.data.items.map((d, i) => (
                <details key={`${d.trace_id}-${offset + i}`} open>
                  <summary className="cursor-pointer font-medium">
                    {STAGES[d.stage ?? ""] ?? d.stage}
                  </summary>
                  <div className="mt-2">
                    <EvidenceFields value={d.item} />
                  </div>
                  <p className="mt-2 break-all font-mono text-xs text-muted-foreground">
                    来源：{d.trace_id} · {d.checksum}
                  </p>
                </details>
              ))}
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - 100))}
                >
                  上一页
                </Button>
                <Button
                  variant="outline"
                  disabled={!evidence.data.has_more}
                  onClick={() => setOffset(offset + 100)}
                >
                  下一页证据
                </Button>
              </div>
            </div>
          )}
        </section>
      )}
      <Link to="/research/topics" className="inline-block text-sm text-primary underline">
        到研究课题记录解释与证据缺口
      </Link>
    </div>
  );
}
