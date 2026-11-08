import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

interface CoverageBarProps {
  /** 服务端覆盖率(0–1);null = 未计算,前端不得回退自算。 */
  coveragePct: number | null;
  /** 服务端分母口径原文(禁止前端改写)。 */
  denominator: string;
  expectedSessions?: number;
  missingSessions?: number;
  className?: string;
}

/**
 * 覆盖率条(issue #512):数值与分母全部来自目录接口。
 *
 * `%` 只做展示换算(× 100)并保留一位小数;分母原文进 tooltip,便于核对
 * 「Σ expected_sessions」与「received_rows」两种口径,不做前端聚合。
 */
export function CoverageBar({
  coveragePct,
  denominator,
  expectedSessions,
  missingSessions,
  className,
}: CoverageBarProps) {
  const { tl, lang } = useT();
  const known = coveragePct !== null && Number.isFinite(coveragePct);
  const pct = known ? Math.max(0, Math.min(1, coveragePct)) * 100 : null;
  const tone =
    pct === null
      ? "bg-muted-foreground/40"
      : pct >= 99.5
        ? "bg-success"
        : pct >= 95
          ? "bg-warning"
          : "bg-destructive";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          className={cn(
            "inline-flex min-w-32 flex-col gap-1 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-ring",
            className,
          )}
          aria-label={
            known
              ? tl({
                  zh: `覆盖率 ${pct?.toFixed(1)}%,分母 ${denominator}`,
                  en: `Coverage ${pct?.toFixed(1)}%, denominator ${denominator}`,
                })
              : tl({ zh: "覆盖率未计算", en: "Coverage not computed" })
          }
        >
          <span className="flex items-center justify-between gap-2 font-mono text-xs">
            <span>{known ? `${pct?.toFixed(1)}%` : "—"}</span>
            {typeof missingSessions === "number" && typeof expectedSessions === "number" && (
              <span className="text-muted-foreground">
                {tl({ zh: "缺", en: "miss" })} {missingSessions}/{expectedSessions}
              </span>
            )}
          </span>
          <span className="h-1.5 w-full overflow-hidden rounded-full bg-secondary" aria-hidden="true">
            <span
              className={cn("block h-full rounded-full", tone)}
              style={{ width: `${known ? Math.max(2, pct ?? 0) : 0}%` }}
            />
          </span>
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <p className="font-medium">
          {known
            ? tl({ zh: "覆盖率", en: "Coverage" }) + `: ${pct?.toFixed(1)}%`
            : tl({ zh: "覆盖率未计算", en: "Coverage not computed" })}
        </p>
        <p className="mt-1">
          {tl({ zh: "分母口径", en: "Denominator" })}: {denominator || "—"}
        </p>
        {typeof missingSessions === "number" && typeof expectedSessions === "number" && (
          <p className="mt-1">
            {lang === "zh"
              ? `缺口 ${missingSessions} / 期望 ${expectedSessions} 交易日`
              : `Gaps ${missingSessions} / expected ${expectedSessions} sessions`}
          </p>
        )}
        <p className="mt-1 text-muted-foreground">
          {tl({ zh: "数值由服务端计算,前端不自算。", en: "Computed server-side; the UI never derives it." })}
        </p>
      </TooltipContent>
    </Tooltip>
  );
}
