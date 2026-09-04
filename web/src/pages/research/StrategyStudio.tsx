import * as React from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ShieldCheck,
  Send,
  FilePlus,
  Undo2,
  CheckCircle2,
  Lock,
  Info,
  Code2,
  Eye,
} from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/states";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Input, Textarea } from "@/components/ui/input";
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Link } from "react-router-dom";
import { strategySpecApi, datasetApi, factorLabApi } from "@/lib/research";
import { MasterList, MasterListItem } from "@/components/ui/master-list";
import type {
  DatasetReleaseSummary,
  FactorCatalogEntry,
} from "@/lib/research";
import { cn, formatDateTime } from "@/lib/utils";
import {
  HintLabel,
  ResearchHint,
} from "@/components/research/ResearchHint";
import { RESEARCH_HINTS } from "@/lib/research-hints";
import { useT, type LocalizedText } from "@/i18n";

/* Section metadata */
const SECTIONS = [
  {
    key: "universe",
    label: { zh: "标的域", en: "Universe" },
    hint: {
      zh: "定义策略在哪些标的上运行：市场、资产类别、流动性筛选和排名规则",
      en: "Defines which instruments the strategy runs on: market, asset classes, liquidity filters and ranking rules",
    },
  },
  {
    key: "feature_graph",
    label: { zh: "特征图", en: "Feature Graph" },
    hint: {
      zh: "定义因子的计算逻辑：从原始数据出发，通过算子（均线、动量、排名等）计算特征值",
      en: "Defines factor computation: derive feature values from raw data via operators (moving average, momentum, rank, etc.)",
    },
  },
  {
    key: "signal_rules",
    label: { zh: "信号规则", en: "Signal Rules" },
    hint: {
      zh: "将特征值转化为买卖信号：如'动量排名前 20% → 买入'。多条规则可并行，冲突按优先级裁决",
      en: "Converts feature values into buy/sell signals, e.g. 'top 20% momentum → buy'. Multiple rules can run in parallel; conflicts are resolved by priority",
    },
  },
  {
    key: "portfolio_policy",
    label: { zh: "组合策略", en: "Portfolio Policy" },
    hint: {
      zh: "将信号转化为目标权重：分配方法（等权/反波动率）、持仓数量上限、单标的权重上限等",
      en: "Converts signals into target weights: allocation method (equal weight / inverse volatility), max positions, per-instrument weight cap, etc.",
    },
  },
  {
    key: "risk_exit_policy",
    label: { zh: "风险退出", en: "Risk Exits" },
    hint: {
      zh: "止损/止盈规则：价格止损、波动率止损、最大回撤降仓、最大持有天数等",
      en: "Stop-loss / take-profit rules: price stop, volatility stop, drawdown de-risking, max holding days, etc.",
    },
  },
  {
    key: "execution_model",
    label: { zh: "执行模型", en: "Execution Model" },
    hint: {
      zh: "模拟交易的执行假设：佣金费率、印花税、滑点、成交时间（次日开盘/收盘）",
      en: "Execution assumptions for simulated trading: commission, stamp tax, slippage, fill timing (next open/close)",
    },
  },
  {
    key: "validation_plan",
    label: { zh: "验证计划", en: "Validation Plan" },
    hint: {
      zh: "样本外验证的时间窗口划分：训练集、验证集、测试集(OOS)的日期范围",
      en: "Time window split for out-of-sample validation: date ranges of the train, validation and test (OOS) sets",
    },
  },
] as const;

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

/* ============================================================ */
/* 数据与信号链路只读视图 — 数据集发布 → 特征节点 → 信号规则 → 组合      */
/* ============================================================ */

function specSection(
  spec: Record<string, unknown>,
  key: string,
): Record<string, unknown> | undefined {
  const value = spec[key];
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function SpecChainPanel({
  spec,
  releases,
}: {
  spec: Record<string, unknown>;
  releases: DatasetReleaseSummary[];
}) {
  const { tl } = useT();
  const catalogQuery = useQuery({
    queryKey: ["factor-catalog"],
    queryFn: () => factorLabApi.catalog(),
  });
  const factorByName = React.useMemo(() => {
    const map = new Map<string, FactorCatalogEntry>();
    for (const entry of catalogQuery.data ?? []) map.set(entry.name, entry);
    return map;
  }, [catalogQuery.data]);
  const releaseById = React.useMemo(() => {
    const map = new Map<string, DatasetReleaseSummary>();
    for (const rel of releases) map.set(rel.release_id, rel);
    return map;
  }, [releases]);

  const graph = specSection(spec, "feature_graph");
  const nodes = Array.isArray(graph?.nodes) ? (graph!.nodes as Record<string, unknown>[]) : [];
  const rulesSection = specSection(spec, "signal_rules");
  const rules = Array.isArray(rulesSection?.rules)
    ? (rulesSection!.rules as Record<string, unknown>[])
    : [];
  const portfolio = specSection(spec, "portfolio_policy");
  const plan = specSection(spec, "validation_plan");
  const releaseIds = Array.isArray(plan?.dataset_release_ids)
    ? (plan!.dataset_release_ids as unknown[]).filter(
        (id): id is string => typeof id === "string",
      )
    : [];

  const ruleText = (rule: Record<string, unknown>) => {
    const bits = [String(rule.feature_id ?? "?"), String(rule.comparator ?? "?")];
    if (rule.threshold !== undefined && rule.threshold !== null) bits.push(String(rule.threshold));
    if (rule.reference_feature_id) bits.push(String(rule.reference_feature_id));
    bits.push(`→ ${String(rule.action ?? "?")}`);
    return bits.join(" ");
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm">
          {tl({ zh: "数据与信号链路", en: "Data & signal chain" })}
          <span className="ml-2 text-xs font-normal text-muted-foreground">
            {tl({
              zh: "本规格如何从已发布数据计算出信号(只读)",
              en: "How this spec derives signals from published data (read-only)",
            })}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-xs">
        <div>
          <p className="font-medium text-foreground">
            1. {tl({ zh: "数据集发布", en: "Dataset releases" })}
            <span className="ml-1 text-muted-foreground">({releaseIds.length})</span>
          </p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {releaseIds.length === 0 && (
              <span className="text-muted-foreground">{tl({ zh: "未绑定", en: "None bound" })}</span>
            )}
            {releaseIds.map((id) => {
              const rel = releaseById.get(id);
              return (
                <Link
                  key={id}
                  to={`/research/data?release=${encodeURIComponent(id)}`}
                  className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 transition-colors hover:bg-accent"
                >
                  <span className="font-mono text-[11px] text-foreground">{id}</span>
                  {rel && (
                    <span className="text-[10px] text-muted-foreground">
                      {rel.dataset_name}
                      {rel.dataset_kind ? ` · ${rel.dataset_kind}` : ""} · v{rel.version}
                    </span>
                  )}
                </Link>
              );
            })}
          </div>
        </div>

        <div>
          <p className="font-medium text-foreground">
            2. {tl({ zh: "特征节点", en: "Feature nodes" })}
            <span className="ml-1 text-muted-foreground">({nodes.length})</span>
          </p>
          <div className="mt-1.5 space-y-1">
            {nodes.map((node, i) => {
              const nodeId = String(node.node_id ?? `node-${i}`);
              const source = typeof node.source === "string" ? node.source : null;
              const inputs = Array.isArray(node.inputs) ? (node.inputs as unknown[]).map(String) : [];
              const factor = source ? factorByName.get(source) : undefined;
              return (
                <div key={nodeId} className="flex flex-wrap items-center gap-1.5">
                  <span className="font-medium text-foreground">{String(node.label ?? nodeId)}</span>
                  <Badge variant="info" className="font-mono text-[10px]">
                    {String(node.operator ?? "?")}
                  </Badge>
                  {typeof node.window === "number" && (
                    <span className="text-muted-foreground">{node.window}d</span>
                  )}
                  {source && (
                    <span className="text-muted-foreground">
                      ← <span className="font-mono">{source}</span>
                      {factor ? (
                        <span className="ml-1 text-[10px]">
                          {tl({ zh: "[目录因子]", en: "[catalog factor]" })}
                          {(factor.source_datasets ?? []).length > 0 &&
                            ` · ${factor.source_datasets!.join("/")}`}
                        </span>
                      ) : (
                        source !== "close" && (
                          <span className="ml-1 text-[10px] text-muted-foreground/70">
                            {tl({ zh: "[价格特征]", en: "[price feature]" })}
                          </span>
                        )
                      )}
                    </span>
                  )}
                  {inputs.length > 0 && (
                    <span className="text-muted-foreground">
                      ← <span className="font-mono">{inputs.join(" + ")}</span>
                    </span>
                  )}
                </div>
              );
            })}
            {nodes.length === 0 && (
              <span className="text-muted-foreground">{tl({ zh: "无特征节点", en: "No feature nodes" })}</span>
            )}
          </div>
        </div>

        <div>
          <p className="font-medium text-foreground">
            3. {tl({ zh: "信号规则", en: "Signal rules" })}
            <span className="ml-1 text-muted-foreground">({rules.length})</span>
          </p>
          <div className="mt-1.5 space-y-1 font-mono text-[11px] text-muted-foreground">
            {rules.map((rule, i) => (
              <p key={String(rule.rule_id ?? i)}>{ruleText(rule)}</p>
            ))}
            {rules.length === 0 && (
              <span className="font-sans text-muted-foreground">
                {tl({ zh: "无信号规则", en: "No signal rules" })}
              </span>
            )}
          </div>
        </div>

        <div>
          <p className="font-medium text-foreground">4. {tl({ zh: "组合策略", en: "Portfolio policy" })}</p>
          <p className="mt-1 text-muted-foreground">
            {portfolio
              ? [
                  String(portfolio.allocation_method ?? ""),
                  portfolio.max_positions ? tl({ zh: `最多 ${String(portfolio.max_positions)} 只`, en: `max ${String(portfolio.max_positions)}` }) : "",
                  portfolio.max_target_weight ? `${tl({ zh: "单标的上限", en: "per-instrument cap" })} ${String(portfolio.max_target_weight)}` : "",
                ]
                  .filter(Boolean)
                  .join(" · ") || tl({ zh: "默认配置", en: "Defaults" })
              : tl({ zh: "未声明", en: "Not specified" })}
          </p>
        </div>
      </CardContent>
    </Card>
  );
}

export default function StrategyStudio() {
  const { tl } = useT();
  const qc = useQueryClient();
  const [selectedKind, setSelectedKind] = React.useState<string | null>(null);
  const [showSetup, setShowSetup] = React.useState(false);
  const [spec, setSpec] = React.useState<Record<string, unknown> | null>(null);
  const [editMode, setEditMode] = React.useState(false);
  const [specText, setSpecText] = React.useState("");
  const [showPublishDialog, setShowPublishDialog] = React.useState(false);
  const [showRollbackDialog, setShowRollbackDialog] = React.useState(false);
  const [templateError, setTemplateError] = React.useState<string | null>(null);

  const { data: registry, isLoading: registryLoading } = useQuery({
    queryKey: ["spec-registry"],
    queryFn: strategySpecApi.registry,
  });

  const releasesQuery = useQuery({
    queryKey: ["dataset-releases", "studio"],
    queryFn: () => datasetApi.releases({ limit: 50 }),
  });

  const strategiesQuery = useQuery({
    queryKey: ["spec-list"],
    queryFn: () => strategySpecApi.list(50),
  });
  const strategies = strategiesQuery.data;

  const historyQuery = useQuery({
    queryKey: ["spec-history", selectedKind],
    queryFn: () => strategySpecApi.history(selectedKind!),
    enabled: !!selectedKind,
  });
  const history = historyQuery.data;
  const releases = releasesQuery.data;

  const validateMutation = useMutation({
    mutationFn: (s: Record<string, unknown>) => strategySpecApi.validate(s),
  });

  const draftMutation = useMutation({
    mutationFn: (s: Record<string, unknown>) =>
      strategySpecApi.createDraft(s, history?.[0]?.version),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["spec-list"] }),
  });

  const publishMutation = useMutation({
    mutationFn: ({
      strategyId,
      version,
      expectedVersion,
    }: {
      strategyId: string;
      version: number;
      expectedVersion: number;
    }) => strategySpecApi.publish(strategyId, version, expectedVersion),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["spec-list"] });
      qc.invalidateQueries({ queryKey: ["spec-history"] });
    },
  });

  const rollbackMutation = useMutation({
    mutationFn: ({
      strategyId,
      targetVersion,
      expectedVersion,
    }: {
      strategyId: string;
      targetVersion: number;
      expectedVersion: number;
    }) => strategySpecApi.rollback(strategyId, targetVersion, expectedVersion),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["spec-list"] });
      qc.invalidateQueries({ queryKey: ["spec-history"] });
    },
  });

  React.useEffect(() => {
    if (editMode && spec) {
      setSpecText(JSON.stringify(spec, null, 2));
    }
  }, [editMode, spec]);

  const handleValidate = () => {
    const s = editMode ? safeParse(specText) : spec;
    if (s) validateMutation.mutate(s);
  };

  const handleSaveDraft = () => {
    const s = editMode ? safeParse(specText) : spec;
    if (s) draftMutation.mutate(s);
  };

  const handleEditModeToggle = () => {
    if (editMode && spec) {
      const parsed = safeParse(specText);
      if (parsed) {
        setSpec(parsed);
        setEditMode(false);
      }
    } else {
      setEditMode(true);
    }
  };

  const validation = validateMutation.data;
  const parseError = React.useMemo(() => {
    if (!editMode || !specText) return null;
    return safeParseError(specText);
  }, [editMode, specText]);

  return (
    <div>
      <PageHeader
        title={tl({ zh: "策略 Studio", en: "Strategy Studio" })}
        description={tl({
          zh: "无代码结构化策略配置 — 白名单组件、即时校验、版本管理",
          en: "No-code structured strategy configuration — whitelisted components, instant validation, version management",
        })}
      />

      <Alert variant="info" className="mb-4">
        <Info className="h-4 w-4" />
        <AlertTitle>{tl({ zh: "策略 Studio vs 回测预设", en: "Strategy Studio vs backtest presets" })}</AlertTitle>
        <AlertDescription>
          {tl({ zh: "回测页内的策略 ", en: "Backtest " })}
          <strong>{tl({ zh: "预设", en: "presets" })}</strong>
          {tl({
            zh: "用于快速回测探索（选个内置策略 + 改参数 + 跑结果）。",
            en: " (inside the Backtest page) are for quick backtest exploration (pick a built-in strategy + tweak parameters + run results).",
          })}
          {tl({ zh: "策略 ", en: " Strategy " })}
          <strong>Studio</strong>
          {tl({
            zh: "（本页）用于创建正式的研究规格 —— 完整定义特征图、信号规则、组合策略、风险退出和验证计划，",
            en: " (this page) is for creating formal research specs — fully defining the feature graph, signal rules, portfolio policy, risk exits and validation plan. ",
          })}
          {tl({
            zh: "保存后可供研究运行引用。不支持 Python 编辑。",
            en: "Saved specs can be referenced by research runs. Python editing is not supported.",
          })}
        </AlertDescription>
      </Alert>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
        {/* Left: Strategy list */}
        <div className="space-y-4 lg:col-span-1">
          <MasterList
            title={
              <HintLabel hint={RESEARCH_HINTS.strategy.studio} className="text-sm font-semibold">
                {tl({ zh: "策略类型", en: "Strategy Type" })}
              </HintLabel>
            }
          >
            {registryLoading ? (
              <div className="space-y-2 p-2">
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-full" />
              </div>
            ) : (
              registry?.strategies.map((s) => {
                const kind = s.kind as string;
                const name = (s.name ?? kind) as string;
                const desc = (s.description ?? "") as string;
                return (
                  <MasterListItem
                    key={kind}
                    selected={selectedKind === kind && !showSetup}
                    onClick={() => {
                      setSelectedKind(kind);
                      setTemplateError(null);
                      setShowSetup(true);
                    }}
                  >
                    <div className="text-sm font-medium">{name}</div>
                    <div className="min-w-0 truncate text-xs text-muted-foreground/60">{kind}</div>
                    <div className="mt-1 line-clamp-2 text-xs text-muted-foreground/50">{desc}</div>
                  </MasterListItem>
                );
              })
            )}
          </MasterList>

          {strategies && strategies.length > 0 && (
            <MasterList
              title={tl({ zh: "已保存策略", en: "Saved Strategies" })}
              count={strategies.length}
            >
              {strategies.map((s) => (
                <MasterListItem
                  key={`${s.strategy_id}@${s.version}`}
                  selected={selectedKind === s.strategy_id && !showSetup}
                  onClick={() => {
                    setSelectedKind(s.strategy_id);
                    setSpec(s.spec);
                    setShowSetup(false);
                  }}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="min-w-0 truncate text-sm font-medium">{s.strategy_id}</span>
                    <span className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground/60">
                      v{s.version}
                      {s.published && <Lock className="h-3 w-3 text-success" />}
                    </span>
                  </div>
                </MasterListItem>
              ))}
            </MasterList>
          )}
        </div>

        {/* Center+Right: Editor */}
        <div className="lg:col-span-3">
          {templateError && (
            <Alert variant="destructive" className="mb-4">
              <AlertTitle>{tl({ zh: "模板加载失败", en: "Failed to Load Template" })}</AlertTitle>
              <AlertDescription>
                {templateError}
                {tl({
                  zh: "。策略规格没有被创建或启动；请检查数据发布版本后重试。",
                  en: ". The strategy spec was not created or started; check the dataset release versions and retry.",
                })}
              </AlertDescription>
            </Alert>
          )}
          {validateMutation.isError && (
            <Alert variant="destructive" className="mb-4">
              <AlertTitle>{tl({ zh: "规格校验失败", en: "Spec Validation Failed" })}</AlertTitle>
              <AlertDescription>
                {validateMutation.error instanceof Error
                  ? validateMutation.error.message
                  : tl({
                      zh: "服务端无法完成策略规格校验，请检查数据发布和特征依赖。",
                      en: "The server could not validate the strategy spec; check dataset releases and feature dependencies.",
                    })}
              </AlertDescription>
            </Alert>
          )}
          {draftMutation.isError && (
            <Alert variant="destructive" className="mb-4">
              <AlertTitle>{tl({ zh: "草稿保存失败", en: "Failed to Save Draft" })}</AlertTitle>
              <AlertDescription>
                {draftMutation.error instanceof Error
                  ? draftMutation.error.message
                  : tl({
                      zh: "策略规格没有保存，请检查校验结果后重试。",
                      en: "The strategy spec was not saved; check the validation result and retry.",
                    })}
              </AlertDescription>
            </Alert>
          )}
          {draftMutation.isSuccess && (
            <Alert variant="success" className="mb-4">
              <AlertTitle>{tl({ zh: "策略草稿已保存", en: "Strategy Draft Saved" })}</AlertTitle>
              <AlertDescription>
                {tl({
                  zh: "已生成版本记录。保存不会自动发布，也不会启动研究运行；请先完成校验，再按审批流程发布。",
                  en: "A version record has been created. Saving neither publishes nor starts a research run; complete validation first, then publish via the approval process.",
                })}
              </AlertDescription>
            </Alert>
          )}
          {publishMutation.isError && (
            <Alert variant="destructive" className="mb-4">
              <AlertTitle>{tl({ zh: "策略发布失败", en: "Failed to Publish Strategy" })}</AlertTitle>
              <AlertDescription>
                {publishMutation.error instanceof Error
                  ? publishMutation.error.message
                  : tl({
                      zh: "策略版本没有发布，请检查当前版本和校验状态。",
                      en: "The strategy version was not published; check the current version and validation status.",
                    })}
              </AlertDescription>
            </Alert>
          )}
          {publishMutation.isSuccess && (
            <Alert variant="success" className="mb-4">
              <AlertTitle>{tl({ zh: "策略版本已发布", en: "Strategy Version Published" })}</AlertTitle>
              <AlertDescription>
                {tl({
                  zh: "已发布当前策略版本。发布不会自动启动研究运行，请前往研究运行页面显式排队。",
                  en: "The current strategy version has been published. Publishing does not automatically start a research run; queue one explicitly on the research runs page.",
                })}
              </AlertDescription>
            </Alert>
          )}
          {strategiesQuery.isError && (
            <Alert variant="warning" className="mb-4">
              <AlertTitle>{tl({ zh: "已保存策略列表暂时不可用", en: "Saved Strategy List Temporarily Unavailable" })}</AlertTitle>
              <AlertDescription>
                {errorMessage(strategiesQuery.error, tl({ zh: "无法读取策略版本历史", en: "Could not read strategy version history" }))}
                {tl({
                  zh: "；新模板仍可编辑，但保存后的版本可能需要刷新或后端恢复后才能显示。",
                  en: "; New templates remain editable, but saved versions may only appear after a refresh or once the backend recovers.",
                })}
              </AlertDescription>
            </Alert>
          )}
          {releasesQuery.isError && (
            <Alert variant="destructive" className="mb-4">
              <AlertTitle>{tl({ zh: "数据发布列表加载失败", en: "Failed to Load Dataset Releases" })}</AlertTitle>
              <AlertDescription>
                {errorMessage(releasesQuery.error, tl({ zh: "无法读取数据发布版本", en: "Could not read dataset release versions" }))}
                {tl({
                  zh: "；模板必须绑定可用数据发布后才能校验或保存。",
                  en: "; Templates must bind an available dataset release before they can be validated or saved.",
                })}
              </AlertDescription>
            </Alert>
          )}
          {historyQuery.isError && selectedKind && (
            <Alert variant="warning" className="mb-4">
              <AlertTitle>{tl({ zh: "版本历史加载失败", en: "Failed to Load Version History" })}</AlertTitle>
              <AlertDescription>
                {errorMessage(historyQuery.error, tl({ zh: "无法读取当前策略的版本历史", en: "Could not read version history for this strategy" }))}
                {tl({ zh: "；请刷新后再发布或回滚。", en: "; Refresh before publishing or rolling back." })}
              </AlertDescription>
            </Alert>
          )}
          {!spec ? (
            <Card>
              <CardContent className="py-16">
                <EmptyState
                  icon={<Code2 className="h-10 w-10" />}
                  title={tl({ zh: "选择策略类型开始", en: "Select a Strategy Type to Start" })}
                  description={tl({
                    zh: "从左侧选择一个策略类型，系统将自动加载模板供你编辑。不需要手写 JSON。",
                    en: "Pick a strategy type on the left and a template will be loaded for you to edit. No hand-written JSON required.",
                  })}
                />
              </CardContent>
            </Card>
          ) : (
            <div className="space-y-4">
              {/* Top bar */}
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <h2 className="text-lg font-bold">
                    {String(spec.strategy_id ?? selectedKind ?? "")}
                  </h2>
                  <Badge variant="secondary">
                    {String(spec.schema_version ?? "v1").startsWith("v")
                      ? String(spec.schema_version ?? "v1")
                      : `v${String(spec.schema_version)}`}
                  </Badge>
                  {editMode ? (
                    <Badge variant="warning">{tl({ zh: "JSON 编辑模式", en: "JSON Edit Mode" })}</Badge>
                  ) : (
                    <Badge variant="info">{tl({ zh: "结构化视图", en: "Structured View" })}</Badge>
                  )}
                </div>
                <div className="flex gap-2">
                  <Button size="sm" variant="outline" onClick={handleEditModeToggle}>
                    {editMode ? <Eye className="mr-1.5 h-4 w-4" /> : <Code2 className="mr-1.5 h-4 w-4" />}
                    {editMode
                      ? tl({ zh: "切换到结构化", en: "Switch to Structured" })
                      : tl({ zh: "切换到 JSON", en: "Switch to JSON" })}
                  </Button>
                  <Button size="sm" variant="outline" onClick={handleValidate} disabled={!!parseError}>
                    <ShieldCheck className="mr-1.5 h-4 w-4" />
                    {tl({ zh: "校验", en: "Validate" })}
                  </Button>
                  <Button size="sm" variant="outline" onClick={handleSaveDraft} disabled={!!parseError}>
                    <FilePlus className="mr-1.5 h-4 w-4" />
                    {tl({ zh: "保存草稿", en: "Save Draft" })}
                  </Button>
                </div>
              </div>

              {parseError && (
                <Alert variant="destructive">
                  <AlertDescription className="font-mono text-xs">{parseError}</AlertDescription>
                </Alert>
              )}

              {/* 数据与信号链路只读视图 */}
              <SpecChainPanel spec={spec} releases={releasesQuery.data ?? []} />

              {/* Editor body */}
              {editMode ? (
                <Card>
                  <CardContent className="p-4">
                    <Textarea
                      value={specText}
                      onChange={(e) => setSpecText(e.target.value)}
                      className="min-h-[600px] resize-y font-mono text-xs"
                      spellCheck={false}
                      aria-label={tl({ zh: "策略规格 JSON", en: "Strategy spec JSON" })}
                    />
                  </CardContent>
                </Card>
              ) : (
                <StructuredEditor spec={spec} onChange={setSpec} />
              )}

              {/* Validation result */}
              {validation && (
                <Card>
                  <CardHeader>
                    <CardTitle className="flex items-center gap-2 text-sm">
                      <ShieldCheck className="h-4 w-4" />
                      <HintLabel hint={RESEARCH_HINTS.strategy.validate}>{tl({ zh: "校验结果", en: "Validation Result" })}</HintLabel>
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="space-y-3">
                      <div className="flex items-center gap-2">
                        {validation.can_execute ? (
                          <Badge variant="success">
                            <CheckCircle2 className="mr-1 h-3 w-3" />
                            {tl({ zh: "可执行", en: "Executable" })}
                          </Badge>
                        ) : (
                          <Badge variant="warning">{tl({ zh: "执行权关闭", en: "Execution Disabled" })}</Badge>
                        )}
                        <code className="text-xs text-muted-foreground">{validation.checksum}</code>
                      </div>
                      {validation.errors && validation.errors.length > 0 && (
                        <div className="space-y-1">
                          {validation.errors.map((err, i) => (
                            <div
                              key={i}
                              className="rounded bg-destructive/10 px-2 py-1 text-xs text-destructive"
                            >
                              {err}
                            </div>
                          ))}
                        </div>
                      )}
                      {!validation.can_execute &&
                        (!validation.errors || validation.errors.length === 0) && (
                          <Alert variant="info">
                            <AlertDescription>
                              {tl({
                                zh: "规格解析已完成，但执行权默认关闭。保存/发布不会自动启动回测或模拟；后续必须由受控 worker/CLI 消费 queued 研究运行。",
                                en: "Spec parsing completed, but execution is disabled by default. Saving/publishing will not start a backtest or simulation; a queued research run must later be consumed by a controlled worker/CLI.",
                              })}
                            </AlertDescription>
                          </Alert>
                        )}
                      {validation.feature_order.length > 0 && (
                        <div>
                          <div className="mb-1 text-xs text-muted-foreground">{tl({ zh: "特征顺序", en: "Feature Order" })}</div>
                          <div className="flex flex-wrap gap-1">
                            {validation.feature_order.map((f) => (
                              <Badge key={f} variant="secondary" className="font-mono text-[10px]">
                                {f}
                              </Badge>
                            ))}
                          </div>
                        </div>
                      )}
                      {validation.required_datasets.length > 0 && (
                        <div>
                          <div className="mb-1 text-xs text-muted-foreground">{tl({ zh: "依赖数据集", en: "Required Datasets" })}</div>
                          <div className="flex flex-wrap gap-1">
                            {validation.required_datasets.map((d) => (
                              <Badge key={d} variant="info" className="text-[10px]">
                                {d}
                              </Badge>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  </CardContent>
                </Card>
              )}

              {/* Version history */}
              {selectedKind && history && history.length > 0 && (
                <Card>
                  <CardHeader>
                    <div className="flex items-center justify-between">
                      <CardTitle className="text-sm">{tl({ zh: "版本历史", en: "Version History" })}</CardTitle>
                      <div className="flex gap-1">
                        <Button
                          size="sm"
                          variant="ghost"
                          aria-label={tl({ zh: "发布策略版本", en: "Publish Strategy Version" })}
                          title={tl({ zh: "发布策略版本", en: "Publish Strategy Version" })}
                          onClick={() => setShowPublishDialog(true)}
                        >
                          <Send className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          aria-label={tl({ zh: "回滚策略版本", en: "Roll Back Strategy Version" })}
                          title={tl({ zh: "回滚策略版本", en: "Roll Back Strategy Version" })}
                          onClick={() => setShowRollbackDialog(true)}
                        >
                          <Undo2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </div>
                  </CardHeader>
                  <CardContent className="p-2">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead className="h-8 text-xs">{tl({ zh: "版本", en: "Version" })}</TableHead>
                          <TableHead className="h-8 text-xs">{tl({ zh: "状态", en: "Status" })}</TableHead>
                          <TableHead className="h-8 text-xs">{tl({ zh: "时间", en: "Time" })}</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {history.map((v) => (
                          <TableRow key={v.version}>
                            <TableCell className="py-1.5 font-mono text-xs">v{v.version}</TableCell>
                            <TableCell className="py-1.5">
                              {v.published ? (
                                <Badge variant="success" className="text-[10px]">
                                  {tl({ zh: "已发布", en: "Published" })}
                                </Badge>
                              ) : (
                                <Badge variant="secondary" className="text-[10px]">
                                  {tl({ zh: "草稿", en: "Draft" })}
                                </Badge>
                              )}
                            </TableCell>
                            <TableCell className="py-1.5 text-xs text-muted-foreground">
                              {formatDateTime(v.created_at)}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </CardContent>
                </Card>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Setup dialog */}
      <SetupDialog
        open={showSetup}
        onOpenChange={setShowSetup}
        kind={selectedKind}
        releases={releases ?? []}
        onConfirm={(strategyId, releaseIds) => {
          setShowSetup(false);
          loadTemplate(strategyId, selectedKind!, releaseIds);
        }}
      />

      {/* Publish dialog */}
      <PublishDialog
        open={showPublishDialog}
        onOpenChange={setShowPublishDialog}
        strategyId={selectedKind}
        latestVersion={history?.[0]?.version ?? 0}
        onConfirm={(version, expectedVersion) =>
          publishMutation.mutate(
            { strategyId: selectedKind!, version, expectedVersion },
            { onSuccess: () => setShowPublishDialog(false) },
          )
        }
      />

      {/* Rollback dialog */}
      <RollbackDialog
        open={showRollbackDialog}
        onOpenChange={setShowRollbackDialog}
        history={history ?? []}
        onConfirm={(targetVersion, expectedVersion) =>
          rollbackMutation.mutate(
            { strategyId: selectedKind!, targetVersion, expectedVersion },
            { onSuccess: () => setShowRollbackDialog(false) },
          )
        }
      />
    </div>
  );

  async function loadTemplate(strategyId: string, kind: string, releaseIds: string[]) {
    setTemplateError(null);
    try {
      const tmpl = await strategySpecApi.template(kind, {
        strategy_id: strategyId,
        dataset_release_ids: releaseIds,
      });
      setSpec(tmpl);
    } catch (err) {
      setSpec(null);
      setTemplateError(err instanceof Error ? err.message : tl({ zh: "接口未返回有效模板", en: "The API returned no valid template" }));
    }
  }
}

/* ============================================================ */
/* Structured editor — sectioned view of the spec              */
/* ============================================================ */

function StructuredEditor({
  spec,
  onChange,
}: {
  spec: Record<string, unknown>;
  onChange: (spec: Record<string, unknown>) => void;
}) {
  const { tl } = useT();
  const updateField = (field: string, value: unknown) => {
    onChange({ ...spec, [field]: value });
  };

  const updateSection = (section: string, value: unknown) => {
    onChange({ ...spec, [section]: value });
  };

  return (
    <div className="space-y-3">
      {/* Basic info */}
      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <div>
              <Label htmlFor="spec-name">{tl({ zh: "策略名称", en: "Strategy Name" })}</Label>
              <Input
                id="spec-name"
                value={String(spec.name ?? "")}
                onChange={(e) => updateField("name", e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="spec-kind">{tl({ zh: "策略类型", en: "Strategy Type" })}</Label>
              <Input
                id="spec-kind"
                value={String(spec.strategy_kind ?? "")}
                readOnly
                className="bg-muted/50 font-mono text-sm"
              />
            </div>
          </div>
          <div>
            <Label htmlFor="spec-desc">{tl({ zh: "策略描述", en: "Strategy Description" })}</Label>
            <Textarea
              id="spec-desc"
              value={String(spec.description ?? "")}
              onChange={(e) => updateField("description", e.target.value)}
              className="min-h-[60px]"
            />
          </div>
        </CardContent>
      </Card>

      {/* Sections */}
      <Accordion type="multiple" defaultValue={["universe"]} className="space-y-2">
        {SECTIONS.map((section) => {
          const sectionData = spec[section.key];
          return (
            <AccordionItem
              key={section.key}
              value={section.key}
              className="overflow-hidden rounded-lg border border-border"
            >
              <AccordionTrigger className="px-4 py-3 hover:bg-accent">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-sm">{tl(section.label)}</span>
                  <ResearchHint hint={{ title: section.label, description: section.hint }} />
                  {sectionData != null && (
                    <Badge variant="secondary" className="ml-2 text-[10px]">
                      {tl({ zh: "已配置", en: "Configured" })}
                    </Badge>
                  )}
                </div>
              </AccordionTrigger>
              <AccordionContent className="px-4 pb-4">
                <SectionViewer
                  sectionKey={section.key}
                  label={section.label}
                  hint={section.hint}
                  data={sectionData}
                  onChange={(v) => updateSection(section.key, v)}
                />
              </AccordionContent>
            </AccordionItem>
          );
        })}
      </Accordion>
    </div>
  );
}

function SectionViewer({
  sectionKey,
  label,
  data,
  onChange,
}: {
  sectionKey: string;
  label: LocalizedText;
  hint: LocalizedText;
  data: unknown;
  onChange: (value: unknown) => void;
}) {
  const { tl } = useT();
  const [editing, setEditing] = React.useState(false);
  const [text, setText] = React.useState("");

  React.useEffect(() => {
    if (editing && data) setText(JSON.stringify(data, null, 2));
  }, [editing, data]);

  const summary = React.useMemo(() => getSectionSummary(sectionKey, data), [sectionKey, data]);

  if (!data) {
    return <p className="py-2 text-sm text-muted-foreground">{tl({ zh: "未配置", en: "Not Configured" })}</p>;
  }

  return (
    <div className="space-y-2">
      {/* Human-readable summary */}
      {!editing && (
        <div className="space-y-2">
          {summary.length > 0 ? (
            <div className="space-y-1">
              {summary.map((item, i) => (
                <div key={i} className="flex items-start gap-2 text-xs">
                  <span className="shrink-0 text-muted-foreground">{tl(item.label)}:</span>
                  <span className="text-foreground/90">{tl(item.value)}</span>
                </div>
              ))}
            </div>
          ) : (
            <pre className="max-h-[200px] overflow-auto scrollbar-thin rounded bg-muted/50 p-2 text-xs font-mono">
              {JSON.stringify(data, null, 2)}
            </pre>
          )}
          <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
            <Code2 className="mr-1.5 h-3.5 w-3.5" />
            {tl({ zh: "编辑 JSON", en: "Edit JSON" })}
          </Button>
        </div>
      )}

      {/* JSON editor */}
      {editing && (
        <div className="space-y-2">
          <Textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            className="min-h-[200px] resize-y font-mono text-xs"
            spellCheck={false}
            aria-label={tl({ zh: `${label.zh} JSON 编辑`, en: `${label.en} JSON editor` })}
          />
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                const parsed = safeParse(text);
                if (parsed) {
                  onChange(parsed);
                  setEditing(false);
                }
              }}
            >
              {tl({ zh: "保存", en: "Save" })}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              {tl({ zh: "取消", en: "Cancel" })}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function getSectionSummary(
  key: string,
  data: unknown,
): { label: LocalizedText; value: LocalizedText }[] {
  if (!data || typeof data !== "object") return [];
  const d = data as Record<string, unknown>;

  switch (key) {
    case "universe":
      return [
        { label: { zh: "市场", en: "Markets" }, value: sameText(arrToStr(d.markets)) },
        { label: { zh: "资产类别", en: "Asset Classes" }, value: sameText(arrToStr(d.asset_classes)) },
        { label: { zh: "最大持仓数", en: "Max Positions" }, value: sameText(String(d.selection_limit ?? "—")) },
        { label: { zh: "排除 ST", en: "Exclude ST" }, value: bool(d.exclude_st) },
        { label: { zh: "排除停牌", en: "Exclude Suspended" }, value: bool(d.exclude_suspended) },
        { label: { zh: "最小上市天数", en: "Min Listing Days" }, value: sameText(String(d.min_listing_days ?? "—")) },
      ].filter((x) => x.value.zh !== "—");

    case "feature_graph": {
      const nodes = (d.nodes as unknown[]) ?? [];
      const outputs = (d.outputs as unknown[]) ?? [];
      return [
        { label: { zh: "特征数", en: "Features" }, value: sameText(String(nodes.length)) },
        { label: { zh: "输出特征", en: "Output Features" }, value: sameText(arrToStr(outputs)) },
        ...nodes.slice(0, 5).map((n, i) => ({
          label: { zh: `节点 ${i + 1}`, en: `Node ${i + 1}` },
          value: sameText(`${String((n as Record<string, unknown>)?.label ?? "?")} (${String(
            (n as Record<string, unknown>)?.operator ?? "?",
          )})`),
        })),
      ];
    }

    case "signal_rules": {
      const rules = (d.rules as unknown[]) ?? [];
      return [
        { label: { zh: "规则数", en: "Rules" }, value: sameText(String(rules.length)) },
        { label: { zh: "冲突策略", en: "Conflict Policy" }, value: sameText(String(d.conflict_policy ?? "—")) },
        ...rules.slice(0, 5).map((r, i) => ({
          label: { zh: `规则 ${i + 1}`, en: `Rule ${i + 1}` },
          value: sameText(`${String((r as Record<string, unknown>)?.feature_id ?? "?")} ${String(
            (r as Record<string, unknown>)?.comparator ?? "?",
          )} → ${String((r as Record<string, unknown>)?.action ?? "?")}`),
        })),
      ];
    }

    case "portfolio_policy":
      return [
        { label: { zh: "分配方法", en: "Allocation Method" }, value: sameText(String(d.allocation_method ?? "—")) },
        { label: { zh: "最大持仓数", en: "Max Positions" }, value: sameText(String(d.max_positions ?? "—")) },
        { label: { zh: "单标的权重上限", en: "Max Weight per Instrument" }, value: sameText(pct(d.max_target_weight)) },
        { label: { zh: "目标总暴露", en: "Target Gross Exposure" }, value: sameText(pct(d.target_gross_exposure)) },
        { label: { zh: "现金缓冲", en: "Cash Buffer" }, value: sameText(pct(d.cash_buffer)) },
      ];

    case "risk_exit_policy": {
      const rules = (d.rules as unknown[]) ?? [];
      const enabled = rules.filter(
        (r) => (r as Record<string, unknown>)?.enabled === true,
      );
      return [
        {
          label: { zh: "规则数", en: "Rules" },
          value: { zh: `${enabled.length}/${rules.length} 启用`, en: `${enabled.length}/${rules.length} enabled` },
        },
        ...enabled.slice(0, 3).map((r) => ({
          label: sameText(String((r as Record<string, unknown>)?.rule_type ?? "?")),
          value: {
            zh: `阈值 ${String((r as Record<string, unknown>)?.threshold ?? "?")}`,
            en: `Threshold ${String((r as Record<string, unknown>)?.threshold ?? "?")}`,
          },
        })),
      ];
    }

    case "execution_model":
      return [
        { label: { zh: "成交时间", en: "Fill Timing" }, value: sameText(String(d.timing ?? "—")) },
        { label: { zh: "佣金率", en: "Commission Rate" }, value: sameText(String(d.commission_rate ?? "—")) },
        { label: { zh: "印花税", en: "Stamp Tax" }, value: sameText(String(d.sell_tax_rate ?? "—")) },
        { label: { zh: "滑点(bps)", en: "Slippage (bps)" }, value: sameText(String(d.slippage_bps ?? "—")) },
      ];

    case "validation_plan": {
      const releases = (d.dataset_release_ids as unknown[]) ?? [];
      return [
        { label: { zh: "数据发布", en: "Dataset Releases" }, value: { zh: `${String(releases.length)} 个`, en: String(releases.length) } },
        { label: { zh: "模式", en: "Mode" }, value: sameText(String(d.mode ?? "—")) },
        { label: { zh: "训练期", en: "Train Period" }, value: sameText(`${d.train_start ?? "?"} ~ ${d.train_end ?? "?"}`) },
        { label: { zh: "验证期", en: "Validation Period" }, value: sameText(`${d.validation_start ?? "?"} ~ ${d.validation_end ?? "?"}`) },
        { label: { zh: "测试期(OOS)", en: "Test Period (OOS)" }, value: sameText(`${d.test_start ?? "?"} ~ ${d.test_end ?? "?"}`) },
        { label: { zh: "基准", en: "Benchmark" }, value: sameText(String(d.benchmark_symbol ?? "—")) },
      ];
    }

    default:
      return [];
  }
}

/* ============================================================ */
/* Setup Dialog — collect strategy_id + dataset_release_ids     */
/* ============================================================ */

function SetupDialog({
  open,
  onOpenChange,
  kind,
  releases,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  kind: string | null;
  releases: {
    release_id: string;
    dataset_name: string;
    version: string;
    source?: string;
    start_date?: string;
    end_date?: string;
    symbol_count?: number;
    coverage_pct?: number;
    quality_status?: string;
    capabilities?: { key: string; status: string; ready_count?: number }[];
  }[];
  onConfirm: (strategyId: string, releaseIds: string[]) => void;
}) {
  const { tl } = useT();
  const [strategyId, setStrategyId] = React.useState("");
  const [selectedReleases, setSelectedReleases] = React.useState<Set<string>>(new Set());

  React.useEffect(() => {
    if (open && kind) {
      setStrategyId(kind);
      setSelectedReleases(new Set());
    }
  }, [open, kind]);

  const toggleRelease = (id: string) => {
    setSelectedReleases((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{tl({ zh: "创建策略规格", en: "Create Strategy Spec" })}</DialogTitle>
          <DialogDescription>
            {tl({
              zh: "选择数据发布版本。模板将自动填充默认配置，你可以在编辑器中修改。建议优先选择质量为「通过」且包含策略所需能力的数据发布。",
              en: 'Select dataset release versions. The template will be pre-filled with defaults, which you can edit in the editor. Prefer releases whose quality is "passed" and that include the capabilities the strategy needs.',
            })}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div>
            <Label htmlFor="setup-id">{tl({ zh: "策略 ID", en: "Strategy ID" })}</Label>
            <Input
              id="setup-id"
              value={strategyId}
              onChange={(e) => setStrategyId(e.target.value)}
              className="font-mono"
              placeholder={tl({ zh: "如 ma_cross", en: "e.g. ma_cross" })}
            />
          </div>
          <div>
            <Label>{tl({ zh: "数据发布版本", en: "Dataset Release Versions" })}</Label>
            {releases.length === 0 ? (
              <p className="rounded bg-warning/10 p-2 text-xs text-warning">
                {tl({
                  zh: "暂无数据发布。请先在「数据与标的」页面拉取行情数据并发布研究数据集。",
                  en: 'No dataset releases yet. Fetch market data and publish a research dataset on the "Data & Instruments" page first.',
                })}
              </p>
            ) : (
              <div className="max-h-[200px] space-y-1 overflow-y-auto scrollbar-thin">
                {releases.map((r) => (
                  <label
                    key={r.release_id}
                    className="flex cursor-pointer items-start gap-2 rounded-md px-2 py-2 text-sm hover:bg-accent"
                  >
                    <input
                      type="checkbox"
                      checked={selectedReleases.has(r.release_id)}
                      onChange={() => toggleRelease(r.release_id)}
                      className="rounded"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-1">
                        <span className="font-mono text-xs">{r.dataset_name}</span>
                        <Badge variant="secondary" className="text-[10px]">
                          v{r.version}
                        </Badge>
                        {r.quality_status && (
                          <Badge
                            variant={r.quality_status === "passed" ? "success" : "warning"}
                            className="text-[10px]"
                          >
                            {r.quality_status === "passed"
                              ? tl({ zh: "质量通过", en: "Quality Passed" })
                              : tl({ zh: "质量告警", en: "Quality Warning" })}
                          </Badge>
                        )}
                        {r.quality_status === "passed" && (
                          <Badge variant="info" className="text-[10px]">{tl({ zh: "推荐", en: "Recommended" })}</Badge>
                        )}
                      </span>
                      <span className="mt-0.5 block truncate font-mono text-[10px] text-muted-foreground">
                        {r.release_id}
                      </span>
                      <span className="mt-0.5 block text-[10px] text-muted-foreground">
                        {r.start_date ?? "—"} ~ {r.end_date ?? "—"} · {r.symbol_count ?? "—"}
                        {tl({ zh: " 标的 · 覆盖 ", en: " symbols · coverage " })}
                        {r.coverage_pct ?? "—"}
                      </span>
                      {r.capabilities && r.capabilities.length > 0 && (
                        <span className="mt-0.5 block truncate text-[10px] text-muted-foreground">
                          {tl({ zh: "能力：", en: "Capabilities: " })}
                          {r.capabilities.filter((c) => c.status === "ready").map((c) => c.key).join(tl({ zh: "、", en: ", " })) || tl({ zh: "暂无可用能力", en: "No ready capabilities" })}
                        </span>
                      )}
                    </span>
                  </label>
                ))}
              </div>
            )}
          </div>
        </div>
        <DialogFooter>
          <Button
            disabled={!strategyId || selectedReleases.size === 0}
            onClick={() => onConfirm(strategyId, Array.from(selectedReleases))}
          >
            <FilePlus className="mr-2 h-4 w-4" />
            {tl({ zh: "加载模板", en: "Load Template" })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ============================================================ */
/* Publish & Rollback dialogs (same as before)                 */
/* ============================================================ */

function PublishDialog({
  open,
  onOpenChange,
  strategyId,
  latestVersion,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  strategyId: string | null;
  latestVersion: number;
  onConfirm: (version: number, expectedVersion: number) => void;
}) {
  const { tl } = useT();
  const [version, setVersion] = React.useState(latestVersion);
  React.useEffect(() => setVersion(latestVersion), [latestVersion]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{tl({ zh: "发布策略版本", en: "Publish Strategy Version" })}</DialogTitle>
          <DialogDescription>
            {tl({
              zh: `发布 ${strategyId} 的指定版本。发布不会自动启动运行。`,
              en: `Publish a chosen version of ${strategyId}. Publishing does not automatically start a run.`,
            })}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <Label htmlFor="pub-version">{tl({ zh: "发布版本号", en: "Version to Publish" })}</Label>
            <Input
              id="pub-version"
              type="number"
              value={version}
              onChange={(e) => setVersion(Number(e.target.value))}
            />
          </div>
        </div>
        <DialogFooter>
          <Button onClick={() => onConfirm(version, latestVersion)}>
            <Send className="mr-2 h-4 w-4" />
            {tl({ zh: "确认发布", en: "Confirm Publish" })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RollbackDialog({
  open,
  onOpenChange,
  history,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  history: { version: number; published: boolean; created_at: string }[];
  onConfirm: (targetVersion: number, expectedVersion: number) => void;
}) {
  const { tl } = useT();
  const [target, setTarget] = React.useState(history[0]?.version ?? 0);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{tl({ zh: "回滚策略版本", en: "Roll Back Strategy Version" })}</DialogTitle>
          <DialogDescription>{tl({ zh: "选择要回滚到的目标版本", en: "Select the target version to roll back to" })}</DialogDescription>
        </DialogHeader>
        <div className="max-h-[200px] space-y-1 overflow-y-auto scrollbar-thin">
          {history.map((v) => (
            <button
              key={v.version}
              onClick={() => setTarget(v.version)}
              className={cn(
                "flex w-full items-center justify-between rounded-md px-3 py-2 text-sm transition-colors",
                target === v.version ? "bg-primary/10 text-primary" : "hover:bg-accent",
              )}
            >
              <span className="font-mono">v{v.version}</span>
              {v.published && (
                <Badge variant="success" className="text-[10px]">
                  {tl({ zh: "已发布", en: "Published" })}
                </Badge>
              )}
            </button>
          ))}
        </div>
        <DialogFooter>
          <Button onClick={() => onConfirm(target, history[0]?.version ?? 0)}>
            <Undo2 className="mr-2 h-4 w-4" />
            {tl({ zh: `回滚到 v${target}`, en: `Roll back to v${target}` })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ============================================================ */
/* Helpers                                                      */
/* ============================================================ */

function safeParse(text: string): Record<string, unknown> | null {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function safeParseError(text: string): string | null {
  try {
    JSON.parse(text);
    return null;
  } catch (e) {
    return (e as Error).message;
  }
}

function arrToStr(v: unknown): string {
  if (Array.isArray(v)) return v.join(", ");
  return String(v ?? "—");
}

function pct(v: unknown): string {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function bool(v: unknown): LocalizedText {
  return v === true ? { zh: "是", en: "Yes" } : v === false ? { zh: "否", en: "No" } : { zh: "—", en: "—" };
}

/** 后端数据(枚举值/数字等)原样展示,两种语言同文 */
function sameText(s: string): LocalizedText {
  return { zh: s, en: s };
}
