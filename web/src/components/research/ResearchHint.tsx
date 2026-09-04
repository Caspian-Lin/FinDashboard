import * as React from "react";
import { Link, useLocation } from "react-router-dom";
import { ArrowRight, CheckCircle2, CircleHelp } from "lucide-react";
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
      zh: "将通过验证的策略冻结为可复现的研究运行",
      en: "Freeze the validated strategy into a reproducible research run",
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

/**
 * 页顶「研究流程」帮助弹窗:收纳全流程导航与「下一步」引导,
 * 取代此前每页内联的 8 步流程条与页底 NextStepCTA(与侧边栏三重重复)。
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
          {tl({ zh: "研究流程", en: "Workflow" })}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80">
        <p className="text-sm font-medium text-foreground">
          {tl({ zh: "研究流程", en: "Research workflow" })}
        </p>
        <div className="mt-2 space-y-0.5">
          {WORKFLOW_STEPS.map((step) => {
            const isCurrent = currentStep?.step === step.step;
            const isPast = currentStep ? step.step < currentStep.step : false;
            return (
              <Link
                key={step.path}
                to={step.path}
                className={cn(
                  "flex items-center gap-2 rounded-md px-2 py-1.5 text-xs font-medium transition-colors",
                  isCurrent && "bg-primary/10 text-primary",
                  isPast && "text-success",
                  !isCurrent && !isPast && "text-muted-foreground hover:text-foreground",
                )}
              >
                {isPast ? (
                  <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
                ) : (
                  <span className="w-3.5 shrink-0 text-center tabular-nums opacity-60">{step.step}</span>
                )}
                {tl(step.label)}
              </Link>
            );
          })}
        </div>
        {next && (
          <div className="mt-3 border-t border-border pt-3">
            <Link
              to={next.path}
              className="flex items-center justify-between gap-2 rounded-md px-2 py-1.5 transition-colors hover:bg-accent"
            >
              <span>
                <span className="block text-xs font-medium text-foreground">
                  {tl({ zh: "下一步：", en: "Next: " })}
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
