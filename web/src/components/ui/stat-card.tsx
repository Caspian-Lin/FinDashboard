import * as React from "react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

interface StatCardProps {
  label: string;
  value: React.ReactNode;
  icon?: LucideIcon;
  hint?: string;
  trend?: { value: string; positive: boolean };
  className?: string;
}

export function StatCard({ label, value, icon: Icon, hint, trend, className }: StatCardProps) {
  return (
    <div className={cn("rounded-lg border border-border bg-card p-4 shadow-sm", className)}>
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wider text-muted-foreground">{label}</span>
        {Icon && <Icon className="h-4 w-4 text-muted-foreground" />}
      </div>
      <div className="mt-2 text-2xl font-bold tabular-nums text-foreground">{value}</div>
      <div className="mt-1 flex items-center gap-2">
        {trend && (
          <span
            className={cn(
              "text-xs font-medium",
              trend.positive ? "text-success" : "text-destructive",
            )}
          >
            {trend.value}
          </span>
        )}
        {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      </div>
    </div>
  );
}

interface MetricRowProps {
  metrics: { label: string; value: React.ReactNode; hint?: string }[];
  columns?: 2 | 3 | 4;
  className?: string;
}

export function MetricRow({ metrics, columns = 4, className }: MetricRowProps) {
  const colClass =
    columns === 2 ? "sm:grid-cols-2" : columns === 3 ? "sm:grid-cols-3" : "sm:grid-cols-2 lg:grid-cols-4";
  return (
    <div className={cn("grid grid-cols-1 gap-3", colClass, className)}>
      {metrics.map((m, i) => (
        <div key={i} className="rounded-lg border border-border bg-card p-4">
          <div className="text-xs font-medium uppercase tracking-wider text-muted-foreground">{m.label}</div>
          <div className="mt-1 text-xl font-bold tabular-nums text-foreground">{m.value}</div>
          {m.hint && <div className="mt-0.5 text-xs text-muted-foreground">{m.hint}</div>}
        </div>
      ))}
    </div>
  );
}
