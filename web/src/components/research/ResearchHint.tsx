import * as React from "react";
import { Link, useLocation } from "react-router-dom";
import { ArrowRight, CircleHelp } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverTrigger, PopoverContent } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import { useT, type LocalizedText } from "@/i18n";
import InfoHint from "@/components/InfoHint";
import type { InfoHintDefinition } from "@/lib/infoHints";

/* 研究/工具页统一的帮助提示:ResearchHint 现在是 InfoHint 的薄封装,
   图标、悬停+点击钉住、弹出样式全局只有 InfoHint 一套实现。 */

export type ResearchHintContent = InfoHintDefinition;

export function ResearchHint({ hint, className }: { hint: ResearchHintContent; className?: string }) {
  return <InfoHint content={hint} className={className} />;
}

export function HintLabel({
  children,
  hint,
  className,
}: {
  children: React.ReactNode;
  hint: ResearchHintContent;
  className?: string;
}) {
  return (
    <span className={cn("inline-flex items-center gap-1", className)}>
      {children}
      <ResearchHint hint={hint} />
    </span>
  );
}

/* Research workflow steps (rendered inside the page-header help popover) */

const WORKFLOW_STEPS: { path: string; label: LocalizedText; step: number }[] = [
  { path: "/research/data", label: { zh: "数据", en: "Data" }, step: 1 },
  { path: "/research/factors", label: { zh: "因子", en: "Factors" }, step: 2 },
  { path: "/research/strategy", label: { zh: "策略", en: "Strategy" }, step: 3 },
  { path: "/research/experiments", label: { zh: "实验", en: "Experiments" }, step: 4 },
  { path: "/research/runs", label: { zh: "运行", en: "Runs" }, step: 5 },
  { path: "/research/portfolio", label: { zh: "组合", en: "Portfolio" }, step: 6 },
  { path: "/research/simulation", label: { zh: "模拟", en: "Simulation" }, step: 7 },
  { path: "/research/reports", label: { zh: "报告", en: "Reports" }, step: 8 },
];

export interface WorkflowNextStep {
  path: string;
  label: LocalizedText | string;
  description?: LocalizedText | string;
}

/* 探索工具(issue #484):回测只做快速探索、不入研究晋级链;工作台与研究记录
   同为辅助入口,故与 8 步主流程分开展示,不参与步骤序号与当前步高亮。 */
const EXPLORE_TOOLS: { path: string; label: LocalizedText; description: LocalizedText }[] = [
  {
    path: "/backtest",
    label: { zh: "回测", en: "Backtest" },
    description: {
      zh: "快速探索:单次运行看结果,不入晋级链",
      en: "Quick exploration: one run for results, outside the promotion chain",
    },
  },
  {
    path: "/research/workbench",
    label: { zh: "研究工作台", en: "Research Workbench" },
    description: { zh: "OpenCode 研究交互入口", en: "OpenCode research interaction entry" },
  },
  {
    path: "/research/docs",
    label: { zh: "研究记录", en: "Research Notes" },
    description: {
      zh: "仓库 docs/research/ 的只读展示(经 PR 维护)",
      en: "Read-only mirror of the repo's docs/research/ (maintained via PRs)",
    },
  },
];

/** 各研究页在流程中的「下一步」引导(原先每页页底 NextStepCTA 的文案)。 */
export const WORKFLOW_NEXT: Record<string, WorkflowNextStep> = {
  data: {
    path: "/research/factors",
    label: { zh: "因子实验室", en: "Factor Lab" },
    description: {
      zh: "基于已拉取的数据探索因子、创建因子实验",
      en: "Explore factors and create factor experiments from the fetched data",
    },
  },
  factors: {
    path: "/research/strategy",
    label: { zh: "策略 Studio", en: "Strategy Studio" },
    description: { zh: "将因子组合为完整的交易策略", en: "Combine factors into a complete trading strategy" },
  },
  strategy: {
    path: "/research/experiments",
    label: { zh: "实验与 OOS", en: "Experiments & OOS" },
    description: {
      zh: "用样本外数据验证策略是否真的有效，排除过拟合",
      en: "Validate whether the strategy truly works on out-of-sample data and rule out overfitting",
    },
  },
  experiments: {
    path: "/research/runs",
    label: { zh: "研究运行", en: "Research runs" },
    description: {
      zh: "对指定策略版本进行可复现回测，核对验证结果",
      en: "Backtest a specified strategy version reproducibly and inspect validation evidence",
    },
  },
  runs: {
    path: "/research/portfolio",
    label: { zh: "组合与风险", en: "Portfolio & Risk" },
    description: {
      zh: "将冻结的策略转化为目标权重和离散交易计划",
      en: "Turn the frozen strategy into target weights and discrete trade plans",
    },
  },
  portfolio: {
    path: "/research/simulation",
    label: { zh: "模拟盘", en: "Paper Trading" },
    description: {
      zh: "用纸面撮合验证策略在真实交易环境下的表现",
      en: "Validate strategy performance in a realistic trading environment via paper matching.",
    },
  },
  simulation: {
    path: "/research/reports",
    label: { zh: "研究报告", en: "Research Reports" },
    description: {
      zh: "查看模拟交易的完整绩效报告和归因分析",
      en: "View the full performance reports and attribution analysis of the simulated trading",
    },
  },
};

const WORKFLOW_NEXT_BY_PATH: [prefix: string, next: WorkflowNextStep][] = [
  ["/research/data", WORKFLOW_NEXT.data],
  ["/research/factors", WORKFLOW_NEXT.factors],
  ["/research/strategy", WORKFLOW_NEXT.strategy],
  ["/research/experiments", WORKFLOW_NEXT.experiments],
  ["/research/runs", WORKFLOW_NEXT.runs],
  ["/research/portfolio", WORKFLOW_NEXT.portfolio],
  ["/research/simulation", WORKFLOW_NEXT.simulation],
];

/** 按路径前缀推导当前页在流程中的「下一步」(顶栏 workflow help 用;/backtest 无下一步)。 */
export function workflowNextForPath(pathname: string): WorkflowNextStep | undefined {
  return WORKFLOW_NEXT_BY_PATH.find(([prefix]) => pathname.startsWith(prefix))?.[1];
}

/** 顶栏 workflow help 可见性:研究分组页面 + 回测页。 */
export function isResearchWorkflowPath(pathname: string): boolean {
  return pathname.startsWith("/research/") || pathname.startsWith("/backtest");
}

/**
 * 「研究流程」帮助弹窗:全局只挂一份在顶栏(研究分组可见),
 * 收纳全流程导航与「下一步」引导,取代此前每页页头的独立按钮。
 */
export function WorkflowHelpPopover({ next }: { next?: WorkflowNextStep }) {
  const { tl } = useT();
  const { pathname } = useLocation();
  const currentStep = WORKFLOW_STEPS.find((s) => pathname.startsWith(s.path));

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" className="gap-1.5">
          <CircleHelp className="h-4 w-4" />
          {tl({ zh: "研究入口", en: "Research tools" })}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80">
        <p className="text-sm font-medium text-foreground">
          {tl({ zh: "研究入口", en: "Research tools" })}
        </p>
        <p className="mt-2 text-xs text-muted-foreground">研究可暂停、分支与反复验证；页面位置不表示阶段已通过。</p>
        <Link to="/research/topics" className="mt-2 block text-sm text-primary underline">研究课题与证据时间线</Link>
        <div className="mt-2 space-y-0.5">
          {WORKFLOW_STEPS.map((step) => {
            const isCurrent = currentStep?.step === step.step;
            return (
              <Link
                key={step.path}
                to={step.path}
                className={cn(
                  "flex items-center gap-2 rounded-md px-2 py-1.5 text-xs font-medium transition-colors",
                  isCurrent && "bg-primary/10 text-primary",
                  !isCurrent && "text-muted-foreground hover:text-foreground",
                )}
              >
                {tl(step.label)}
              </Link>
            );
          })}
        </div>
        <div className="mt-3 border-t border-border pt-3">
          <p className="px-2 text-xs font-medium text-foreground">
            {tl({ zh: "探索工具", en: "Exploration tools" })}
          </p>
          <p className="mt-0.5 px-2 text-xs text-muted-foreground">
            {tl({
              zh: "按问题选择工具：回测用于快速探索，工作台用于交互，正式资料用于查结论。",
              en: "Choose tools for the question: explore with backtests, interact in the workbench, and consult accepted findings in research docs.",
            })}
          </p>
          <div className="mt-1 space-y-0.5">
            {EXPLORE_TOOLS.map((tool) => (
              <Link
                key={tool.path}
                to={tool.path}
                className="block rounded-md px-2 py-1.5 transition-colors hover:bg-accent"
              >
                <span className="block text-xs font-medium text-muted-foreground">
                  {tl(tool.label)}
                </span>
                <span className="mt-0.5 block text-xs text-muted-foreground/70">
                  {tl(tool.description)}
                </span>
              </Link>
            ))}
          </div>
        </div>
        {next && (
          <div className="mt-3 border-t border-border pt-3">
            <Link
              to={next.path}
              className="flex items-center justify-between gap-2 rounded-md px-2 py-1.5 transition-colors hover:bg-accent"
            >
              <span>
                <span className="block text-xs font-medium text-foreground">
                  {tl({ zh: "相关工具：", en: "Related tool: " })}
                  {typeof next.label === "string" ? next.label : tl(next.label)}
                </span>
                {next.description && (
                  <span className="mt-0.5 block text-xs text-muted-foreground">
                    {typeof next.description === "string" ? next.description : tl(next.description)}
                  </span>
                )}
              </span>
              <ArrowRight className="h-4 w-4 shrink-0 text-primary" />
            </Link>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
