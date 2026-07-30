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
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { strategySpecApi, datasetApi } from "@/lib/research";
import { cn, formatDateTime } from "@/lib/utils";
import {
  WorkflowIndicator,
  NextStepCTA,
  HintLabel,
  ResearchHint,
} from "@/components/research/ResearchHint";
import { RESEARCH_HINTS } from "@/lib/research-hints";

/* Section metadata */
const SECTIONS = [
  { key: "universe", label: "标的域", hint: "定义策略在哪些标的上运行：市场、资产类别、流动性筛选和排名规则" },
  { key: "feature_graph", label: "特征图", hint: "定义因子的计算逻辑：从原始数据出发，通过算子（均线、动量、排名等）计算特征值" },
  { key: "signal_rules", label: "信号规则", hint: "将特征值转化为买卖信号：如'动量排名前 20% → 买入'。多条规则可并行，冲突按优先级裁决" },
  { key: "portfolio_policy", label: "组合策略", hint: "将信号转化为目标权重：分配方法（等权/反波动率）、持仓数量上限、单标的权重上限等" },
  { key: "risk_exit_policy", label: "风险退出", hint: "止损/止盈规则：价格止损、波动率止损、最大回撤降仓、最大持有天数等" },
  { key: "execution_model", label: "执行模型", hint: "模拟交易的执行假设：佣金费率、印花税、滑点、成交时间（次日开盘/收盘）" },
  { key: "validation_plan", label: "验证计划", hint: "样本外验证的时间窗口划分：训练集、验证集、测试集(OOS)的日期范围" },
] as const;

export default function StrategyStudio() {
  const qc = useQueryClient();
  const [selectedKind, setSelectedKind] = React.useState<string | null>(null);
  const [showSetup, setShowSetup] = React.useState(false);
  const [spec, setSpec] = React.useState<Record<string, unknown> | null>(null);
  const [editMode, setEditMode] = React.useState(false);
  const [specText, setSpecText] = React.useState("");
  const [showPublishDialog, setShowPublishDialog] = React.useState(false);
  const [showRollbackDialog, setShowRollbackDialog] = React.useState(false);

  const { data: registry, isLoading: registryLoading } = useQuery({
    queryKey: ["spec-registry"],
    queryFn: strategySpecApi.registry,
  });

  const { data: releases } = useQuery({
    queryKey: ["dataset-releases", "studio"],
    queryFn: () => datasetApi.releases({ limit: 50 }),
  });

  const { data: strategies } = useQuery({
    queryKey: ["spec-list"],
    queryFn: () => strategySpecApi.list(50),
  });

  const { data: history } = useQuery({
    queryKey: ["spec-history", selectedKind],
    queryFn: () => strategySpecApi.history(selectedKind!),
    enabled: !!selectedKind,
  });

  const validateMutation = useMutation({
    mutationFn: (s: Record<string, unknown>) => strategySpecApi.validate(s),
  });

  const draftMutation = useMutation({
    mutationFn: (s: Record<string, unknown>) =>
      strategySpecApi.createDraft(s, (s.expected_version as number) ?? 0),
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
        title="策略 Studio"
        description="无代码结构化策略配置 — 白名单组件、即时校验、版本管理"
      />
      <WorkflowIndicator currentPath="/research/strategy" />

      <Alert variant="info" className="mb-4">
        <Info className="h-4 w-4" />
        <AlertTitle>策略 Studio vs 策略预设</AlertTitle>
        <AlertDescription>
          策略 <strong>预设</strong>（工具栏）用于快速回测探索（选个内置策略 + 改参数 + 跑结果）。
          策略 <strong>Studio</strong>（本页）用于创建正式的研究规格 —— 完整定义特征图、信号规则、组合策略、风险退出和验证计划，
          保存后可供研究运行引用。不支持 Python 编辑。
        </AlertDescription>
      </Alert>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
        {/* Left: Strategy list */}
        <div className="lg:col-span-1">
          <Card>
            <CardHeader>
              <HintLabel hint={RESEARCH_HINTS.strategy.studio} className="text-sm font-semibold">
                策略类型
              </HintLabel>
            </CardHeader>
            <CardContent className="p-2">
              {registryLoading ? (
                <div className="space-y-2 p-2">
                  <Skeleton className="h-10 w-full" />
                  <Skeleton className="h-10 w-full" />
                </div>
              ) : (
                <ScrollArea className="max-h-[400px]">
                  <div className="space-y-1">
                    {registry?.strategies.map((s) => {
                      const kind = s.kind as string;
                      const name = (s.display_name ?? kind) as string;
                      const desc = (s.description ?? "") as string;
                      return (
                        <button
                          key={kind}
                          onClick={() => {
                            setSelectedKind(kind);
                            setShowSetup(true);
                          }}
                          className={cn(
                            "w-full rounded-md px-3 py-2.5 text-left transition-colors",
                            selectedKind === kind && !showSetup
                              ? "bg-primary/10 text-primary"
                              : "hover:bg-accent text-muted-foreground",
                          )}
                        >
                          <div className="font-medium text-sm">{name}</div>
                          <div className="text-xs text-muted-foreground/60">{kind}</div>
                          <div className="mt-1 line-clamp-2 text-xs text-muted-foreground/50">{desc}</div>
                        </button>
                      );
                    })}
                  </div>
                </ScrollArea>
              )}
            </CardContent>
          </Card>

          {strategies && strategies.length > 0 && (
            <Card className="mt-3">
              <CardHeader>
                <CardTitle className="text-sm">已保存策略</CardTitle>
              </CardHeader>
              <CardContent className="p-2">
                <ScrollArea className="max-h-[200px]">
                  <div className="space-y-1">
                    {strategies.map((s) => (
                      <button
                        key={s.strategy_id}
                        onClick={() => {
                          setSelectedKind(s.strategy_id);
                          setSpec(s.spec);
                          setShowSetup(false);
                        }}
                        className={cn(
                          "flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm transition-colors",
                          selectedKind === s.strategy_id && !showSetup
                            ? "bg-primary/10 text-primary"
                            : "hover:bg-accent text-muted-foreground",
                        )}
                      >
                        <span className="font-medium">{s.strategy_id}</span>
                        <div className="flex items-center gap-1">
                          <span className="text-xs text-muted-foreground/60">v{s.version}</span>
                          {s.published && <Lock className="h-3 w-3 text-success" />}
                        </div>
                      </button>
                    ))}
                  </div>
                </ScrollArea>
              </CardContent>
            </Card>
          )}
        </div>

        {/* Center+Right: Editor */}
        <div className="lg:col-span-3">
          {!spec ? (
            <Card>
              <CardContent className="py-16">
                <EmptyState
                  icon={<Code2 className="h-10 w-10" />}
                  title="选择策略类型开始"
                  description="从左侧选择一个策略类型，系统将自动加载模板供你编辑。不需要手写 JSON。"
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
                  <Badge variant="secondary">v{String(spec.schema_version ?? "v1")}</Badge>
                  {editMode ? (
                    <Badge variant="warning">JSON 编辑模式</Badge>
                  ) : (
                    <Badge variant="info">结构化视图</Badge>
                  )}
                </div>
                <div className="flex gap-2">
                  <Button size="sm" variant="outline" onClick={handleEditModeToggle}>
                    {editMode ? <Eye className="mr-1.5 h-4 w-4" /> : <Code2 className="mr-1.5 h-4 w-4" />}
                    {editMode ? "切换到结构化" : "切换到 JSON"}
                  </Button>
                  <Button size="sm" variant="outline" onClick={handleValidate} disabled={!!parseError}>
                    <ShieldCheck className="mr-1.5 h-4 w-4" />
                    校验
                  </Button>
                  <Button size="sm" variant="outline" onClick={handleSaveDraft} disabled={!!parseError}>
                    <FilePlus className="mr-1.5 h-4 w-4" />
                    保存草稿
                  </Button>
                </div>
              </div>

              {parseError && (
                <Alert variant="destructive">
                  <AlertDescription className="font-mono text-xs">{parseError}</AlertDescription>
                </Alert>
              )}

              {/* Editor body */}
              {editMode ? (
                <Card>
                  <CardContent className="p-4">
                    <Textarea
                      value={specText}
                      onChange={(e) => setSpecText(e.target.value)}
                      className="min-h-[600px] resize-y font-mono text-xs"
                      spellCheck={false}
                      aria-label="策略规格 JSON"
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
                      <HintLabel hint={RESEARCH_HINTS.strategy.validate}>校验结果</HintLabel>
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="space-y-3">
                      <div className="flex items-center gap-2">
                        {validation.can_execute ? (
                          <Badge variant="success">
                            <CheckCircle2 className="mr-1 h-3 w-3" />
                            可执行
                          </Badge>
                        ) : (
                          <Badge variant="destructive">不可执行</Badge>
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
                      {validation.feature_order.length > 0 && (
                        <div>
                          <div className="mb-1 text-xs text-muted-foreground">特征顺序</div>
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
                          <div className="mb-1 text-xs text-muted-foreground">依赖数据集</div>
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
                      <CardTitle className="text-sm">版本历史</CardTitle>
                      <div className="flex gap-1">
                        <Button size="sm" variant="ghost" onClick={() => setShowPublishDialog(true)}>
                          <Send className="h-3.5 w-3.5" />
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setShowRollbackDialog(true)}>
                          <Undo2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </div>
                  </CardHeader>
                  <CardContent className="p-2">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead className="h-8 text-xs">版本</TableHead>
                          <TableHead className="h-8 text-xs">状态</TableHead>
                          <TableHead className="h-8 text-xs">时间</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {history.map((v) => (
                          <TableRow key={v.version}>
                            <TableCell className="py-1.5 font-mono text-xs">v{v.version}</TableCell>
                            <TableCell className="py-1.5">
                              {v.published ? (
                                <Badge variant="success" className="text-[10px]">
                                  已发布
                                </Badge>
                              ) : (
                                <Badge variant="secondary" className="text-[10px]">
                                  草稿
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

      <NextStepCTA
        nextPath="/research/experiments"
        nextLabel="实验与 OOS"
        description="用样本外数据验证策略是否真的有效，排除过拟合"
      />

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
    try {
      const tmpl = await strategySpecApi.template(kind, {
        strategy_id: strategyId,
        dataset_release_ids: releaseIds,
      });
      setSpec(tmpl.spec);
    } catch {
      setSpec(null);
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
              <Label htmlFor="spec-name">策略名称</Label>
              <Input
                id="spec-name"
                value={String(spec.name ?? "")}
                onChange={(e) => updateField("name", e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="spec-kind">策略类型</Label>
              <Input
                id="spec-kind"
                value={String(spec.strategy_kind ?? "")}
                readOnly
                className="bg-muted/50 font-mono text-sm"
              />
            </div>
          </div>
          <div>
            <Label htmlFor="spec-desc">策略描述</Label>
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
                  <span className="font-medium text-sm">{section.label}</span>
                  <ResearchHint hint={{ title: section.label, description: section.hint }} />
                  {sectionData != null && (
                    <Badge variant="secondary" className="ml-2 text-[10px]">
                      已配置
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
  label: string;
  hint: string;
  data: unknown;
  onChange: (value: unknown) => void;
}) {
  const [editing, setEditing] = React.useState(false);
  const [text, setText] = React.useState("");

  React.useEffect(() => {
    if (editing && data) setText(JSON.stringify(data, null, 2));
  }, [editing, data]);

  const summary = React.useMemo(() => getSectionSummary(sectionKey, data), [sectionKey, data]);

  if (!data) {
    return <p className="py-2 text-sm text-muted-foreground">未配置</p>;
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
                  <span className="shrink-0 text-muted-foreground">{item.label}:</span>
                  <span className="text-foreground/90">{item.value}</span>
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
            编辑 JSON
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
            aria-label={`${label} JSON 编辑`}
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
              保存
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              取消
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
): { label: string; value: string }[] {
  if (!data || typeof data !== "object") return [];
  const d = data as Record<string, unknown>;

  switch (key) {
    case "universe":
      return [
        { label: "市场", value: arrToStr(d.markets) },
        { label: "资产类别", value: arrToStr(d.asset_classes) },
        { label: "最大持仓数", value: String(d.selection_limit ?? "—") },
        { label: "排除 ST", value: bool(d.exclude_st) },
        { label: "排除停牌", value: bool(d.exclude_suspended) },
        { label: "最小上市天数", value: String(d.min_listing_days ?? "—") },
      ].filter((x) => x.value !== "—");

    case "feature_graph": {
      const nodes = (d.nodes as unknown[]) ?? [];
      const outputs = (d.outputs as unknown[]) ?? [];
      return [
        { label: "特征数", value: String(nodes.length) },
        { label: "输出特征", value: arrToStr(outputs) },
        ...nodes.slice(0, 5).map((n, i) => ({
          label: `节点 ${i + 1}`,
          value: `${String((n as Record<string, unknown>)?.label ?? "?")} (${String(
            (n as Record<string, unknown>)?.operator ?? "?",
          )})`,
        })),
      ];
    }

    case "signal_rules": {
      const rules = (d.rules as unknown[]) ?? [];
      return [
        { label: "规则数", value: String(rules.length) },
        { label: "冲突策略", value: String(d.conflict_policy ?? "—") },
        ...rules.slice(0, 5).map((r, i) => ({
          label: `规则 ${i + 1}`,
          value: `${String((r as Record<string, unknown>)?.feature_id ?? "?")} ${String(
            (r as Record<string, unknown>)?.comparator ?? "?",
          )} → ${String((r as Record<string, unknown>)?.action ?? "?")}`,
        })),
      ];
    }

    case "portfolio_policy":
      return [
        { label: "分配方法", value: String(d.allocation_method ?? "—") },
        { label: "最大持仓数", value: String(d.max_positions ?? "—") },
        { label: "单标的权重上限", value: pct(d.max_target_weight) },
        { label: "目标总暴露", value: pct(d.target_gross_exposure) },
        { label: "现金缓冲", value: pct(d.cash_buffer) },
      ];

    case "risk_exit_policy": {
      const rules = (d.rules as unknown[]) ?? [];
      const enabled = rules.filter(
        (r) => (r as Record<string, unknown>)?.enabled === true,
      );
      return [
        { label: "规则数", value: `${enabled.length}/${rules.length} 启用` },
        ...enabled.slice(0, 3).map((r) => ({
          label: String((r as Record<string, unknown>)?.rule_type ?? "?"),
          value: `阈值 ${String((r as Record<string, unknown>)?.threshold ?? "?")}`,
        })),
      ];
    }

    case "execution_model":
      return [
        { label: "成交时间", value: String(d.timing ?? "—") },
        { label: "佣金率", value: String(d.commission_rate ?? "—") },
        { label: "印花税", value: String(d.sell_tax_rate ?? "—") },
        { label: "滑点(bps)", value: String(d.slippage_bps ?? "—") },
      ];

    case "validation_plan": {
      const releases = (d.dataset_release_ids as unknown[]) ?? [];
      return [
        { label: "数据发布", value: String(releases.length) + " 个" },
        { label: "模式", value: String(d.mode ?? "—") },
        { label: "训练期", value: `${d.train_start ?? "?"} ~ ${d.train_end ?? "?"}` },
        { label: "验证期", value: `${d.validation_start ?? "?"} ~ ${d.validation_end ?? "?"}` },
        { label: "测试期(OOS)", value: `${d.test_start ?? "?"} ~ ${d.test_end ?? "?"}` },
        { label: "基准", value: String(d.benchmark_symbol ?? "—") },
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
  releases: { release_id: string; dataset_name: string; version: string }[];
  onConfirm: (strategyId: string, releaseIds: string[]) => void;
}) {
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
          <DialogTitle>创建策略规格</DialogTitle>
          <DialogDescription>
            选择数据发布版本。模板将自动填充默认配置，你可以在编辑器中修改。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div>
            <Label htmlFor="setup-id">策略 ID</Label>
            <Input
              id="setup-id"
              value={strategyId}
              onChange={(e) => setStrategyId(e.target.value)}
              className="font-mono"
              placeholder="如 ma_cross"
            />
          </div>
          <div>
            <Label>数据发布版本</Label>
            {releases.length === 0 ? (
              <p className="rounded bg-warning/10 p-2 text-xs text-warning">
                暂无数据发布。请先在「数据与标的」页面拉取行情数据并发布研究数据集。
              </p>
            ) : (
              <div className="max-h-[200px] space-y-1 overflow-y-auto scrollbar-thin">
                {releases.map((r) => (
                  <label
                    key={r.release_id}
                    className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-accent"
                  >
                    <input
                      type="checkbox"
                      checked={selectedReleases.has(r.release_id)}
                      onChange={() => toggleRelease(r.release_id)}
                      className="rounded"
                    />
                    <span className="font-mono text-xs">{r.dataset_name}</span>
                    <Badge variant="secondary" className="text-[10px]">
                      v{r.version}
                    </Badge>
                    <span className="ml-auto truncate font-mono text-[10px] text-muted-foreground">
                      {r.release_id}
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
            加载模板
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
  const [version, setVersion] = React.useState(latestVersion);
  React.useEffect(() => setVersion(latestVersion), [latestVersion]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>发布策略版本</DialogTitle>
          <DialogDescription>
            发布 {strategyId} 的指定版本。发布不会自动启动运行。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <Label htmlFor="pub-version">发布版本号</Label>
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
            确认发布
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
  const [target, setTarget] = React.useState(history[0]?.version ?? 0);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>回滚策略版本</DialogTitle>
          <DialogDescription>选择要回滚到的目标版本</DialogDescription>
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
                  已发布
                </Badge>
              )}
            </button>
          ))}
        </div>
        <DialogFooter>
          <Button onClick={() => onConfirm(target, history[0]?.version ?? 0)}>
            <Undo2 className="mr-2 h-4 w-4" />
            回滚到 v{target}
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

function bool(v: unknown): string {
  return v === true ? "是" : v === false ? "否" : "—";
}
