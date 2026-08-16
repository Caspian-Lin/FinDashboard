import * as React from "react";
import { Routes, Route } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { ErrorBoundary } from "@/components/layout/ErrorBoundary";
import { Skeleton } from "@/components/ui/skeleton";

/* Research domain (lazy) */
const ResearchHome = React.lazy(() => import("@/pages/research/ResearchHome"));
const ResearchData = React.lazy(() => import("@/pages/research/ResearchData"));
const FactorLab = React.lazy(() => import("@/pages/research/FactorLab"));
const StrategyStudio = React.lazy(() => import("@/pages/research/StrategyStudio"));
const Experiments = React.lazy(() => import("@/pages/research/Experiments"));
const ResearchRuns = React.lazy(() => import("@/pages/research/ResearchRuns"));
const PortfolioRisk = React.lazy(() => import("@/pages/research/PortfolioRisk"));
const Simulation = React.lazy(() => import("@/pages/research/Simulation"));
const ResearchWorkbench = React.lazy(() => import("@/pages/research/ResearchWorkbench"));
const Reports = React.lazy(() => import("@/pages/research/Reports"));

/* Live trading domain (lazy) */
const Dashboard = React.lazy(() => import("@/pages/Dashboard"));
const Positions = React.lazy(() => import("@/pages/Positions"));
const Orders = React.lazy(() => import("@/pages/Orders"));
const Fills = React.lazy(() => import("@/pages/Fills"));
const Control = React.lazy(() => import("@/pages/Control"));

/* Tools (lazy) */
const Backtest = React.lazy(() => import("@/pages/Backtest"));
const Strategies = React.lazy(() => import("@/pages/Strategies"));
const Settings = React.lazy(() => import("@/pages/Settings"));
const Data = React.lazy(() => import("@/pages/Data"));
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

export default function App() {
  return (
    <AppShell>
      <ErrorBoundary>
        <React.Suspense fallback={<PageLoader />}>
          <Routes>
            {/* Research */}
            <Route path="/research" element={<ResearchHome />} />
            <Route path="/research/data" element={<ResearchData />} />
            <Route path="/research/factors" element={<FactorLab />} />
            <Route path="/research/strategy" element={<StrategyStudio />} />
            <Route path="/research/experiments" element={<Experiments />} />
            <Route path="/research/runs" element={<ResearchRuns />} />
            <Route path="/research/portfolio" element={<PortfolioRisk />} />
            <Route path="/research/simulation" element={<Simulation />} />
            <Route path="/research/workbench" element={<ResearchWorkbench />} />
            <Route path="/research/reports" element={<Reports />} />

            {/* Live Trading */}
            <Route path="/" element={<Dashboard />} />
            <Route path="/positions" element={<Positions />} />
            <Route path="/orders" element={<Orders />} />
            <Route path="/fills" element={<Fills />} />
            <Route path="/control" element={<Control />} />

            {/* Tools */}
            <Route path="/data" element={<Data />} />
            <Route path="/backtest" element={<Backtest />} />
            <Route path="/strategies" element={<Strategies />} />
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
