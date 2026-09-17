import * as React from "react";
import { Badge } from "./badge";
import { cn } from "@/lib/utils";

type StatusVariant = "default" | "success" | "warning" | "destructive" | "info" | "secondary";

const statusVariantMap: Record<string, StatusVariant> = {
  queued: "secondary",
  running: "info",
  pending: "secondary",
  active: "info",
  started: "info",
  paused: "warning",
  stopped: "secondary",
  archived: "secondary",
  completed: "success",
  succeeded: "success",
  success: "success",
  retry_waiting: "warning",
  cancel_requested: "warning",
  interrupted: "warning",
  passed: "success",
  ok: "success",
  approved: "success",
  published: "success",
  consumed: "success",
  ready: "success",
  healthy: "success",
  failed: "destructive",
  error: "destructive",
  rejected: "destructive",
  halted: "destructive",
  cancelled: "destructive",
  canceled: "destructive",
  invalid: "destructive",
  rejected_review: "destructive",
  warning: "warning",
  warnings: "warning",
  unknown: "warning",
  blocked: "warning",
  proposed: "warning",
  draft: "secondary",
  drafts: "secondary",
  off: "secondary",
  no_new_orders: "warning",
  reduce_only: "warning",
  cancel_all: "warning",
  fill: "info",
  filled: "success",
  partial: "warning",
  working: "info",
  new: "info",
  live: "success",
  shadow: "info",
};

interface StatusBadgeProps {
  status: string;
  className?: string;
  children?: React.ReactNode;
}

export function StatusBadge({ status, className, children }: StatusBadgeProps) {
  const normalized = status.toLowerCase();
  const variant = statusVariantMap[normalized] ?? "default";
  const label = children ?? status;
  return (
    <Badge variant={variant} className={cn("font-mono uppercase", className)}>
      {label}
    </Badge>
  );
}

interface DotProps {
  status: "online" | "offline" | "warning" | "idle";
  className?: string;
}

export function StatusDot({ status, className }: DotProps) {
  const colorMap = {
    online: "bg-success",
    offline: "bg-destructive",
    warning: "bg-warning",
    idle: "bg-muted-foreground",
  };
  return (
    <span className={cn("relative inline-flex h-2 w-2", className)}>
      {(status === "online" || status === "warning") && (
        <span
          className={cn(
            "absolute inline-flex h-full w-full animate-ping rounded-full opacity-60",
            colorMap[status],
          )}
        />
      )}
      <span className={cn("relative inline-flex h-2 w-2 rounded-full", colorMap[status])} />
    </span>
  );
}
