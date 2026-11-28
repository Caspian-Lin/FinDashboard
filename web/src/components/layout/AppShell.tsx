import * as React from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Menu, Moon, Sun, ChevronLeft, ChevronRight, AlertTriangle } from "lucide-react";
import { api } from "@/lib/api";
import { RESEARCH_EDITION } from "@/lib/edition";
import { useWebSocket } from "@/lib/ws";
import { useTheme } from "@/components/ui/theme-provider";
import { StatusDot } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTrigger, SheetTitle, SheetDescription } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import { useT } from "@/i18n";
import {
  isCurrentPath,
  livePages,
  navigationContext,
  primaryNavigation,
  toolsNav,
} from "./nav-config";
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
  const { pathname } = useLocation();
  const context = navigationContext(pathname, RESEARCH_EDITION);
  const links = primaryNavigation(RESEARCH_EDITION);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Link
        to="/"
        aria-label={tl({ zh: "FinDashboard 工作总览", en: "FinDashboard workspace overview" })}
        onClick={onNavigate}
        className={cn(
          "flex h-14 shrink-0 items-center gap-2.5 border-b px-4 font-semibold focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring",
          collapsed && "justify-center px-2",
        )}
      >
        <span className="flex size-4 shrink-0 items-end gap-0.5 text-primary" aria-hidden="true">
          <span className="h-1.5 w-1 bg-current" />
          <span className="h-2.5 w-1 bg-current" />
          <span className="h-4 w-1 bg-current" />
        </span>
        {!collapsed && <span className="text-base">FinDashboard</span>}
      </Link>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        <nav className="space-y-1" aria-label={tl({ zh: "主导航", en: "Main navigation" })}>
          {links.map((item) => {
            const active = isCurrentPath(pathname, item.to) || item.restricted && context?.restricted;
            return (
              <React.Fragment key={item.to}>
                <NavLink
                  to={item.to}
                  end
                  onClick={onNavigate}
                  aria-label={tl(item.label)}
                  title={collapsed ? tl(item.label) : undefined}
                  className={cn(
                    "flex min-h-11 items-center gap-2.5 rounded-md px-3 text-sm font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring md:min-h-9",
                    active ? "bg-accent font-semibold text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
                    collapsed && "justify-center px-2",
                  )}
                >
                  <item.icon className={cn("size-4 shrink-0", active && "text-primary")} aria-hidden="true" />
                  {!collapsed && <span>{tl(item.label)}</span>}
                </NavLink>
                {item.restricted && context?.restricted && (
                  <div className={cn("space-y-1", !collapsed && "pl-4")}>
                    {livePages.map((page) => (
                      <NavLink
                        key={page.to}
                        to={page.to}
                        end
                        onClick={onNavigate}
                        aria-label={tl(page.label)}
                        title={collapsed ? tl(page.label) : undefined}
                        className={({ isActive }) => cn(
                          "flex min-h-11 items-center gap-2 rounded-md px-3 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring md:min-h-9",
                          isActive ? "bg-accent font-semibold text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
                          collapsed && "justify-center px-2",
                        )}
                      >
                        <page.icon className="size-4 shrink-0" aria-hidden="true" />
                        {!collapsed && <span>{tl(page.label)}</span>}
                      </NavLink>
                    ))}
                  </div>
                )}
              </React.Fragment>
            );
          })}
        </nav>
        <NavLink
          to={toolsNav.to}
          end
          onClick={onNavigate}
          aria-label={tl(toolsNav.label)}
          title={collapsed ? tl(toolsNav.label) : undefined}
          className={({ isActive }) => cn(
            "mt-4 flex min-h-11 items-center gap-2.5 rounded-md px-3 text-sm font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring md:min-h-9",
            isActive ? "bg-accent font-semibold text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
            collapsed && "justify-center px-2",
          )}
        >
          <toolsNav.icon className="size-4 shrink-0" aria-hidden="true" />
          {!collapsed && <span>{tl(toolsNav.label)}</span>}
        </NavLink>
      </div>
    </div>
  );
}

function SystemStatus({ collapsed = false }: { collapsed?: boolean }) {
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

  if (collapsed) {
    // 折叠态(w-16)放不下文字标签:只渲染状态点,title 承载语义,防止逐字竖排换行挤出框。
    return (
      <div className="flex flex-col items-center gap-2 border-t border-border py-2">
        <span className="flex items-center" title={kernelLabel}>
          <StatusDot status={kernelOk ? "online" : "offline"} />
          <span className="sr-only">{kernelLabel}</span>
        </span>
        <span className="flex items-center" title="WS">
          <StatusDot status={wsConnected ? "online" : "idle"} />
          <span className="sr-only">WS</span>
        </span>
        {ksLevel !== "off" && (
          <span className="flex items-center text-warning" title={`KS: ${ksLevel}`}>
            <AlertTriangle className="h-3 w-3" />
            <span className="sr-only">KS: {ksLevel}</span>
          </span>
        )}
      </div>
    );
  }

  return (
    <div className="flex items-center gap-4 whitespace-nowrap border-t border-border px-4 py-2">
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

  const currentPage = navigationContext(location.pathname, RESEARCH_EDITION);

  return (
    <div className="flex h-dvh overflow-hidden bg-background">
      <a
        href="#main-content"
        className="sr-only z-50 rounded-md bg-primary px-3 py-2 text-primary-foreground focus:not-sr-only focus:fixed focus:left-3 focus:top-3"
      >
        {tl({ zh: "跳到正文", en: "Skip to main content" })}
      </a>
      {/* Desktop Sidebar */}
      <aside
        className={cn(
          "hidden md:flex shrink-0 flex-col border-r border-border bg-card transition-[width] duration-200",
          collapsed ? "w-16" : "w-60",
        )}
      >
        <SidebarContent collapsed={collapsed} />
        {!RESEARCH_EDITION && <SystemStatus collapsed={collapsed} />}
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
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {/* Top Bar */}
        <header data-finboard-workbench-header className="flex h-14 shrink-0 items-center justify-between border-b border-border bg-card px-4">
          <div className="flex items-center gap-3">
            {/* Mobile menu */}
            <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
              <SheetTrigger asChild>
                <Button variant="ghost" size="icon" className="md:hidden" aria-label={tl({ zh: "打开菜单", en: "Open menu" })}>
                  <Menu className="h-5 w-5" />
                </Button>
              </SheetTrigger>
              <SheetContent side="left" className="w-[min(20rem,90vw)] p-0">
                <SheetTitle className="sr-only">{tl({ zh: "导航菜单", en: "Navigation menu" })}</SheetTitle>
                <SheetDescription className="sr-only">{tl({ zh: "选择工作区页面", en: "Choose a workspace page" })}</SheetDescription>
                <div className="flex h-full flex-col bg-card">
                  <SidebarContent collapsed={false} onNavigate={() => setMobileOpen(false)} />
                  {!RESEARCH_EDITION && <SystemStatus />}
                </div>
              </SheetContent>
            </Sheet>

            <div className="flex items-center gap-2">
              {/* 顶栏只承载全局导航上下文,不渲染页面主标题;唯一 h1 由页面 PageHeader 提供 */}
              <span className="max-w-[36vw] truncate text-sm font-semibold text-foreground" aria-hidden="true">
                {currentPage ? tl(currentPage.label) : ""}
              </span>
              {currentPage?.restricted && (
                <span className="hidden items-center gap-1 rounded bg-warning/10 px-1.5 py-0.5 text-[10px] font-medium text-warning sm:inline-flex">
                  <AlertTriangle className="h-2.5 w-2.5" />
                  {tl({ zh: "受控区域", en: "Restricted area" })}
                </span>
              )}
            </div>
          </div>

          <div data-finboard-workbench-toolbar className="flex items-center gap-1">
            {isResearchWorkflowPath(location.pathname) && (
              <WorkflowHelpPopover next={workflowNextForPath(location.pathname)} />
            )}
            <ThemeToggle />
          </div>
        </header>

        {/* Content */}
        <main id="main-content" tabIndex={-1} className="flex-1 overflow-y-auto scrollbar-thin p-4 md:p-6">
          {children}
        </main>
      </div>
    </div>
  );
}
