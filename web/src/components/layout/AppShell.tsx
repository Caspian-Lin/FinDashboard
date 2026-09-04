import * as React from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Menu, Moon, Sun, ChevronLeft, ChevronRight, AlertTriangle } from "lucide-react";
import { api } from "@/lib/api";
import { useWebSocket } from "@/lib/ws";
import { useTheme } from "@/components/ui/theme-provider";
import { StatusDot } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTrigger, SheetTitle } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import { useT } from "@/i18n";
import { navGroups } from "./nav-config";
import {
  WorkflowHelpPopover,
  workflowNextForPath,
  isResearchWorkflowPath,
} from "@/components/research/ResearchHint";

function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  const { t } = useT();
  return (
    <Button variant="ghost" size="icon" onClick={toggleTheme} aria-label={t("shell.toggleTheme")}>
      {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
    </Button>
  );
}

function SidebarContent({ collapsed, onNavigate }: { collapsed: boolean; onNavigate?: () => void }) {
  const { tl } = useT();
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Logo:与顶栏同高(56px),图标/文字在同一对齐线上 */}
      <div className={cn("flex h-14 items-center border-b border-border px-4", collapsed && "justify-center px-2")}>
        <div className="flex items-center gap-2">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary text-sm font-bold text-primary-foreground">
            FD
          </div>
          {!collapsed && (
            <span className="font-bold text-foreground text-lg tracking-tight">FinDashboard</span>
          )}
        </div>
      </div>

      {/* Nav */}
      <nav className="min-h-0 flex-1 overflow-y-auto scrollbar-thin py-3" aria-label={tl({ zh: "主导航", en: "Main navigation" })}>
        {navGroups.map((group) => (
          <div key={group.label.en} className="mb-4">
            {!collapsed && (
              <div className="mb-1 flex items-center gap-2 px-4">
                <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  {tl(group.label)}
                </span>
                {group.restricted && (
                  <AlertTriangle className="h-3 w-3 text-warning" aria-label={tl({ zh: "受控区域", en: "Restricted area" })} />
                )}
              </div>
            )}
            <div className="space-y-0.5 px-2">
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  onClick={onNavigate}
                  className={({ isActive }) =>
                    cn(
                      "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                      collapsed && "justify-center px-2",
                      isActive
                        ? "bg-primary/10 text-primary"
                        : "text-muted-foreground hover:bg-accent hover:text-foreground",
                      group.restricted && !isActive && "text-muted-foreground/70",
                    )
                  }
                  title={collapsed ? tl(item.label) : undefined}
                >
                  <item.icon className="h-4 w-4 shrink-0" />
                  {!collapsed && <span>{tl(item.label)}</span>}
                </NavLink>
              ))}
            </div>
          </div>
        ))}
      </nav>
    </div>
  );
}

function SystemStatus() {
  const { t } = useT();
  const wsConnected = useWebSocket();
  const healthQuery = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 5000,
  });
  const health = healthQuery.data;
  const kernelOk = health?.kernel_ready ?? false;
  const ksLevel = health?.kill_switch_level ?? "off";
  const kernelLabel = healthQuery.isError
    ? t("shell.apiUnavailable")
    : kernelOk
      ? t("shell.kernelReady")
      : t("shell.kernelNotReady");

  return (
    <div className="flex items-center gap-4 px-4 py-2 border-t border-border">
      <div className="flex items-center gap-1.5 text-xs">
        <StatusDot status={kernelOk ? "online" : "offline"} />
        <span className="text-muted-foreground" title={healthQuery.isError ? t("shell.healthCheckFailed") : undefined}>
          {kernelLabel}
        </span>
      </div>
      <div className="flex items-center gap-1.5 text-xs">
        <StatusDot status={wsConnected ? "online" : "idle"} />
        <span className="text-muted-foreground">WS</span>
      </div>
      {ksLevel !== "off" && (
        <div className="flex items-center gap-1 text-xs text-warning font-medium">
          <AlertTriangle className="h-3 w-3" />
          KS: {ksLevel}
        </div>
      )}
    </div>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const { tl } = useT();
  const [collapsed, setCollapsed] = React.useState(false);
  const [mobileOpen, setMobileOpen] = React.useState(false);
  const location = useLocation();

  const pageTitle = React.useMemo(() => {
    for (const group of navGroups) {
      for (const item of group.items) {
        if (item.end ? location.pathname === item.to : location.pathname.startsWith(item.to)) {
          return { title: tl(item.label), group: tl(group.label), restricted: group.restricted };
        }
      }
    }
    return { title: "", group: "", restricted: false };
  }, [location.pathname]);

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      {/* Desktop Sidebar */}
      <aside
        className={cn(
          "hidden md:flex shrink-0 flex-col border-r border-border bg-card transition-[width] duration-200",
          collapsed ? "w-16" : "w-60",
        )}
      >
        <SidebarContent collapsed={collapsed} />
        <SystemStatus />
        <div className="border-t border-border p-2">
          <Button
            variant="ghost"
            size="sm"
            className="w-full justify-center text-muted-foreground"
            onClick={() => setCollapsed((c) => !c)}
            aria-label={collapsed ? tl({ zh: "展开侧栏", en: "Expand sidebar" }) : tl({ zh: "折叠侧栏", en: "Collapse sidebar" })}
          >
            {collapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronLeft className="h-4 w-4" />}
          </Button>
        </div>
      </aside>

      {/* Main Area */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Top Bar */}
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-border bg-card px-4">
          <div className="flex items-center gap-3">
            {/* Mobile menu */}
            <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
              <SheetTrigger asChild>
                <Button variant="ghost" size="icon" className="md:hidden" aria-label={tl({ zh: "打开菜单", en: "Open menu" })}>
                  <Menu className="h-5 w-5" />
                </Button>
              </SheetTrigger>
              <SheetContent side="left" className="w-72 p-0">
                <SheetTitle className="sr-only">{tl({ zh: "导航菜单", en: "Navigation menu" })}</SheetTitle>
                <div className="flex h-full flex-col bg-card">
                  <SidebarContent collapsed={false} onNavigate={() => setMobileOpen(false)} />
                  <SystemStatus />
                </div>
              </SheetContent>
            </Sheet>

            <div className="flex items-center gap-2">
              {pageTitle.group && (
                <span className="hidden text-xs text-muted-foreground sm:inline">{pageTitle.group}</span>
              )}
              {pageTitle.group && <span className="hidden text-xs text-muted-foreground sm:inline">/</span>}
              {/* 顶栏只承载全局导航上下文,不渲染页面主标题;唯一 h1 由页面 PageHeader 提供 */}
              <span className="text-sm font-semibold text-foreground" aria-hidden="true">
                {pageTitle.title}
              </span>
              {pageTitle.restricted && (
                <span className="hidden items-center gap-1 rounded bg-warning/10 px-1.5 py-0.5 text-[10px] font-medium text-warning sm:inline-flex">
                  <AlertTriangle className="h-2.5 w-2.5" />
                  {tl({ zh: "受控区域", en: "Restricted area" })}
                </span>
              )}
            </div>
          </div>

          <div className="flex items-center gap-1">
            {isResearchWorkflowPath(location.pathname) && (
              <WorkflowHelpPopover next={workflowNextForPath(location.pathname)} />
            )}
            <ThemeToggle />
          </div>
        </header>

        {/* Content */}
        <main className="flex-1 overflow-y-auto scrollbar-thin p-4 md:p-6">
          {children}
        </main>
      </div>
    </div>
  );
}
