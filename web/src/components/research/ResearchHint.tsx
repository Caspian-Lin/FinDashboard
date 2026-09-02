import * as React from "react";
import { Link } from "react-router-dom";
import { HelpCircle, ArrowRight, CheckCircle2 } from "lucide-react";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useT, type LocalizedText } from "@/i18n";

/* Reusable InfoHint badge that shows help text on hover/focus */

interface HintData {
  title: LocalizedText;
  description: LocalizedText;
  detail?: LocalizedText;
}

export function ResearchHint({ hint, className }: { hint: HintData; className?: string }) {
  const { tl } = useT();
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex h-4 w-4 items-center justify-center rounded-full text-muted-foreground/60 transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring -m-2.5 p-2.5",
            className,
          )}
          aria-label={tl(hint.title)}
        >
          <HelpCircle className="h-3.5 w-3.5" />
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-sm">
        <div className="space-y-1">
          <p className="font-medium text-foreground">{tl(hint.title)}</p>
          <p className="text-xs text-muted-foreground">{tl(hint.description)}</p>
          {hint.detail && <p className="text-xs italic text-muted-foreground/70">{tl(hint.detail)}</p>}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}

export function HintLabel({
  children,
  hint,
  className,
}: {
  children: React.ReactNode;
  hint: HintData;
  className?: string;
}) {
  return (
    <span className={cn("inline-flex items-center gap-1", className)}>
      {children}
      <ResearchHint hint={hint} />
    </span>
  );
}

/* Research workflow steps indicator */

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

export function WorkflowIndicator({ currentPath }: { currentPath: string }) {
  const { tl } = useT();
  const currentStep = WORKFLOW_STEPS.find((s) => currentPath.startsWith(s.path));

  if (!currentStep) return null;

  return (
    <div className="mb-4 flex items-center gap-1 overflow-x-auto scrollbar-thin rounded-lg border border-border bg-card p-2">
      {WORKFLOW_STEPS.map((step, i) => {
        const isCurrent = step.step === currentStep.step;
        const isPast = step.step < currentStep.step;
        return (
          <React.Fragment key={step.path}>
            {i > 0 && <ArrowRight className="h-3 w-3 shrink-0 text-muted-foreground/40" />}
            <Link
              to={step.path}
              className={cn(
                "flex shrink-0 items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                isCurrent && "bg-primary/10 text-primary",
                isPast && "text-success",
                !isCurrent && !isPast && "text-muted-foreground hover:text-foreground",
              )}
            >
              {isPast && <CheckCircle2 className="h-3 w-3" />}
              <span className="tabular-nums opacity-60">{step.step}</span>
              {tl(step.label)}
            </Link>
          </React.Fragment>
        );
      })}
    </div>
  );
}

/* Next step CTA */

export function NextStepCTA({
  nextPath,
  nextLabel,
  description,
}: {
  nextPath: string;
  nextLabel: LocalizedText | string;
  description?: LocalizedText | string;
}) {
  const { tl } = useT();
  return (
    <Link
      to={nextPath}
      className="mt-6 flex items-center justify-between rounded-lg border border-primary/30 bg-primary/5 px-4 py-3 transition-colors hover:border-primary/50 hover:bg-primary/10"
    >
      <div>
        <p className="text-sm font-medium text-foreground">{tl({ zh: "下一步：", en: "Next: " })}{typeof nextLabel === "string" ? nextLabel : tl(nextLabel)}</p>
        {description && <p className="text-xs text-muted-foreground">{typeof description === "string" ? description : tl(description)}</p>}
      </div>
      <ArrowRight className="h-4 w-4 text-primary" />
    </Link>
  );
}
