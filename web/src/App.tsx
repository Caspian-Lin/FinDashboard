import * as React from "react";
import { Routes, Route, Navigate, useLocation } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { ErrorBoundary } from "@/components/layout/ErrorBoundary";
import { Skeleton } from "@/components/ui/skeleton";
import { legacyResearchDataTarget } from "@/lib/data-links";
import { LegacyWorkbenchRedirect } from "@/pages/research/LegacyWorkbenchRedirect";

const ResearchTopics = React.lazy(() => import("@/pages/research/ResearchTopics"));
const StrategyExplanationPage = React.lazy(() => import("@/pages/research/StrategyExplanationPage"));

/* Shared data domain (lazy) */
const DataBase = React.lazy(() => import("@/pages/data/DataBase"));

/* Research domain (lazy) */
const ResearchHome = React.lazy(() => import("@/pages/research/ResearchHome"));
const FactorLab = React.lazy(() => import("@/pages/research/FactorLab"));
const StrategyStudio = React.lazy(() => import("@/pages/research/StrategyStudio"));
const Experiments = React.lazy(() => import("@/pages/research/Experiments"));
const ResearchRuns = React.lazy(() => import("@/pages/research/ResearchRuns"));
const PortfolioRisk = React.lazy(() => import("@/pages/research/PortfolioRisk"));
const Simulation = React.lazy(() => import("@/pages/research/Simulation"));
const AgentConnect = React.lazy(() => import("@/pages/research/AgentConnect"));
const ResearchDocs = React.lazy(() => import("@/pages/research/ResearchDocs"));
const Reports = React.lazy(() => import("@/pages/research/Reports"));

/* Live trading domain (lazy) */
const Dashboard = React.lazy(() => import("@/pages/Dashboard"));
const Positions = React.lazy(() => import("@/pages/Positions"));
const Orders = React.lazy(() => import("@/pages/Orders"));
const Fills = React.lazy(() => import("@/pages/Fills"));
const Control = React.lazy(() => import("@/pages/Control"));

/* Tools (lazy) */
const Backtest = React.lazy(() => import("@/pages/Backtest"));
const Settings = React.lazy(() => import("@/pages/Settings"));
const Jobs = React.lazy(() => import("@/pages/Jobs"));
const NotFound = React.lazy(() => import("@/pages/NotFound"));

function PageLoader() {
  return (
    <div className="space-y-4 p-1">
      <Skeleton className="h-8 w-64" />
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-24 w-full" />
        ))}
      </div>
      <Skeleton className="h-64 w-full" />
    </div>
  );
}

/**
 * 旧 `/research/data` → `/data` 参数保全跳转(issue #512)。
 *
 * `tab` 按 `legacyResearchDataTarget` 映射到新数据集 tab,`release` 等参数原样
 * 保留;不 404、不丢深链。
 */
export function LegacyResearchDataRedirect() {
  const { search } = useLocation();
  return <Navigate to={legacyResearchDataTarget(search)} replace />;
}

export default function App() {
  return (
    <AppShell>
      <ErrorBoundary>
        <React.Suspense fallback={<PageLoader />}>
          <Routes>
            {/* Shared data base(issue #512;一级入口,导航在「研究」组首位) */}
            <Route path="/data" element={<DataBase />} />
            {/* 旧路由永久跳转,保全 tab/release 等查询参数 */}
            <Route path="/research/data" element={<LegacyResearchDataRedirect />} />

            {/* Research */}
            <Route path="/research/topics" element={<ResearchTopics />} />
            <Route path="/research/strategy-explanation" element={<StrategyExplanationPage />} />
            <Route path="/research" element={<ResearchHome />} />
            <Route path="/research/factors" element={<FactorLab />} />
            <Route path="/research/strategy" element={<StrategyStudio />} />
            <Route path="/research/experiments" element={<Experiments />} />
            <Route path="/research/runs" element={<ResearchRuns />} />
            <Route path="/research/portfolio" element={<PortfolioRisk />} />
            <Route path="/research/simulation" element={<Simulation />} />
            <Route path="/research/agent-connect" element={<AgentConnect />} />
            {/* 旧内置工作台永久跳转到外部 Agent 接入页,保全 ?topic= 等参数 */}
            <Route path="/research/workbench" element={<LegacyWorkbenchRedirect />} />
            <Route path="/research/docs" element={<ResearchDocs />} />
            <Route path="/research/reports" element={<Reports />} />

            {/* Live Trading */}
            <Route path="/" element={<Dashboard />} />
            <Route path="/positions" element={<Positions />} />
            <Route path="/orders" element={<Orders />} />
            <Route path="/fills" element={<Fills />} />
            <Route path="/control" element={<Control />} />

            {/* Tools */}
            <Route path="/backtest" element={<Backtest />} />
            {/* 策略预设已并入回测页的预设库面板 */}
            <Route path="/strategies" element={<Navigate to="/backtest" replace />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/jobs" element={<Jobs />} />

            {/* 404 */}
            <Route path="*" element={<NotFound />} />
          </Routes>
        </React.Suspense>
      </ErrorBoundary>
    </AppShell>
  );
}
