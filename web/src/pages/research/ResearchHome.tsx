import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Database,
  Atom,
  SlidersHorizontal,
  TestTube,
  GitBranch,
  Scale,
  PlayCircle,
  MonitorSmartphone,
  FileText,
  ArrowRight,
  Activity,
  CheckCircle2,
  AlertCircle,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/status-badge";
import { StatCard } from "@/components/ui/stat-card";
import { LoadingState } from "@/components/ui/states";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { researchRunApi } from "@/lib/research";
import { experimentApi } from "@/lib/research";
import { datasetApi } from "@/lib/research";
import { simulationApi } from "@/lib/simulation";
import { timeAgo } from "@/lib/utils";
import { useLanguage, useT, type LocalizedText } from "@/i18n";

const workflowSteps: { to: string; label: LocalizedText; icon: LucideIcon; desc: LocalizedText }[] = [
  { to: "/research/data", label: { zh: "数据与标的", en: "Data & Instruments" }, icon: Database, desc: { zh: "发布研究数据、查看资产覆盖与质量告警", en: "Publish research data, view asset coverage and quality alerts" } },
  { to: "/research/factors", label: { zh: "因子实验室", en: "Factor Lab" }, icon: Atom, desc: { zh: "因子目录、特征快照、信号预览与因子实验", en: "Factor catalog, feature snapshots, signal preview, and factor experiments" } },
  { to: "/research/strategy", label: { zh: "策略 Studio", en: "Strategy Studio" }, icon: SlidersHorizontal, desc: { zh: "无代码结构化策略配置、版本 diff 与发布", en: "No-code structured strategy configuration, version diffs, and publishing" } },
  { to: "/research/experiments", label: { zh: "实验与 OOS", en: "Experiments & OOS" }, icon: TestTube, desc: { zh: "机器验证实验、阈值与稳健性检验", en: "Machine-verified experiments, thresholds, and robustness checks" } },
  { to: "/research/runs", label: { zh: "研究运行", en: "Research Runs" }, icon: GitBranch, desc: { zh: "冻结输入、血缘追踪与运行重放", en: "Frozen inputs, lineage tracking, and run replay" } },
  { to: "/research/portfolio", label: { zh: "组合与风险", en: "Portfolio & Risk" }, icon: Scale, desc: { zh: "目标权重分配、离散交易与资金可行性", en: "Target weight allocation, discrete trades, and capital feasibility" } },
  { to: "/research/simulation", label: { zh: "模拟盘", en: "Simulation" }, icon: PlayCircle, desc: { zh: "纸面撮合、目标仓位决策与绩效报告", en: "Paper matching, target position decisions, and performance reports" } },
  { to: "/research/workbench", label: { zh: "研究工作台", en: "Research Workbench" }, icon: MonitorSmartphone, desc: { zh: "OpenCode Web 研究交互(AI 能力由 OpenCode 承担)", en: "OpenCode Web research interaction (AI capabilities handled by OpenCode)" } },
  { to: "/research/reports", label: { zh: "研究报告", en: "Research Reports" }, icon: FileText, desc: { zh: "聚合展示运行结果、绩效归因与风险", en: "Aggregated run results, performance attribution, and risk" } },
];

export default function ResearchHome() {
  const { tl } = useT();
  const { lang } = useLanguage();
  const { data: runs, isLoading: runsLoading, isError: runsError } = useQuery({
    queryKey: ["research-runs", "home"],
    queryFn: () => researchRunApi.list({ limit: 5 }),
  });
  const { data: experiments, isError: experimentsError } = useQuery({
    queryKey: ["experiments", "home"],
    queryFn: () => experimentApi.list({ limit: 5 }),
  });
  const { data: releases, isError: releasesError } = useQuery({
    queryKey: ["dataset-releases", "home"],
    queryFn: () => datasetApi.releases({ limit: 5 }),
  });
  const { data: simSessions, isError: simSessionsError } = useQuery({
    queryKey: ["sim-sessions", "home"],
    queryFn: () => simulationApi.listSessions({ limit: 5 }),
  });

  const completedRuns = runsError ? null : runs?.filter((r) => r.status === "completed").length ?? 0;
  const runningRuns = runsError ? null : runs?.filter((r) => r.status === "running" || r.status === "queued").length ?? 0;
  const completedExps = experimentsError ? null : experiments?.filter((e) => e.status === "completed").length ?? 0;
  const activeSims = simSessionsError ? null : simSessions?.filter((s) => s.status === "running").length ?? 0;
  const hasQueryError = runsError || experimentsError || releasesError || simSessionsError;

  return (
    <div>
      <PageHeader
        title={tl({ zh: "研究首页", en: "Research Home" })}
        description={tl({ zh: "从数据到模拟盘的完整研究工作流", en: "The full research workflow, from data to simulation" })}
      />

      {hasQueryError && (
        <Alert variant="warning" className="mb-4">
          <AlertTitle>{tl({ zh: "研究首页有数据未加载", en: "Some Research Home data failed to load" })}</AlertTitle>
          <AlertDescription>
            {tl({ zh: "“—”表示接口暂时不可用，不代表数量为 0。请进入对应工作流页面重试；已加载的数据不会被覆盖。", en: "“—” means the API is temporarily unavailable, not zero. Retry from the corresponding workflow page; already loaded data will not be overwritten." })}
          </AlertDescription>
        </Alert>
      )}

      {/* Stats */}
      <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard label={tl({ zh: "已完成研究运行", en: "Completed Research Runs" })} value={completedRuns ?? "—"} icon={CheckCircle2} hint={runningRuns === null ? tl({ zh: "加载失败", en: "Load failed" }) : runningRuns > 0 ? tl({ zh: `${runningRuns} 个进行中`, en: `${runningRuns} in progress` }) : tl({ zh: "全部完成", en: "All completed" })} />
        <StatCard label={tl({ zh: "已完成实验", en: "Completed Experiments" })} value={completedExps ?? "—"} icon={TestTube} hint={experimentsError ? tl({ zh: "加载失败", en: "Load failed" }) : undefined} />
        <StatCard label={tl({ zh: "最近数据发布", en: "Recent Data Releases" })} value={releasesError ? "—" : releases?.length ?? 0} icon={Database} hint={releasesError ? tl({ zh: "加载失败", en: "Load failed" }) : undefined} />
        <StatCard label={tl({ zh: "活动模拟会话", en: "Active Simulation Sessions" })} value={activeSims ?? "—"} icon={PlayCircle} hint={simSessionsError ? tl({ zh: "加载失败", en: "Load failed" }) : undefined} />
      </div>

      {/* Workflow quick access */}
      <h2 className="mb-3 text-sm font-semibold text-foreground">{tl({ zh: "研究工作流", en: "Research Workflow" })}</h2>
      <div className="mb-8 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {workflowSteps.map((step) => (
          <Link
            key={step.to}
            to={step.to}
            className="group rounded-lg border border-border bg-card p-4 transition-all hover:border-primary/50"
          >
            <div className="flex items-start justify-between">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
                  <step.icon className="h-5 w-5" />
                </div>
                <div>
                  <h3 className="text-sm font-semibold text-foreground">{tl(step.label)}</h3>
                  <p className="mt-0.5 text-xs text-muted-foreground">{tl(step.desc)}</p>
                </div>
              </div>
              <ArrowRight className="h-4 w-4 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
            </div>
          </Link>
        ))}
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* Recent runs */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <GitBranch className="h-4 w-4 text-muted-foreground" />
              {tl({ zh: "最近研究运行", en: "Recent Research Runs" })}
            </CardTitle>
          </CardHeader>
          <CardContent>
            {runsLoading ? (
              <LoadingState rows={3} />
            ) : runsError ? (
              <p className="py-4 text-center text-sm text-destructive">{tl({ zh: "研究运行加载失败，请进入「研究运行」重试。", en: "Failed to load research runs. Go to “Research Runs” to retry." })}</p>
            ) : runs && runs.length > 0 ? (
              <div className="space-y-2">
                {runs.slice(0, 5).map((run) => (
                  <Link
                    key={run.run_id}
                    to={`/research/runs?run=${encodeURIComponent(run.run_id)}`}
                    className="flex items-center justify-between rounded-md px-3 py-2 text-sm hover:bg-accent"
                  >
                    <div className="flex items-center gap-2">
                      <StatusBadge status={run.status} />
                      <span className="font-mono text-xs text-muted-foreground">{run.run_id}</span>
                    </div>
                    <span className="text-xs text-muted-foreground">{timeAgo(run.created_at, lang)}</span>
                  </Link>
                ))}
              </div>
            ) : (
              <p className="py-4 text-center text-sm text-muted-foreground">{tl({ zh: "暂无研究运行", en: "No research runs yet" })}</p>
            )}
          </CardContent>
        </Card>

        {/* Data releases */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Database className="h-4 w-4 text-muted-foreground" />
              {tl({ zh: "最近数据发布", en: "Recent Data Releases" })}
            </CardTitle>
          </CardHeader>
          <CardContent>
            {releasesError ? (
              <p className="py-4 text-center text-sm text-destructive">{tl({ zh: "数据发布加载失败，请进入「数据与标的」重试。", en: "Failed to load data releases. Go to “Data & Instruments” to retry." })}</p>
            ) : releases && releases.length > 0 ? (
              <div className="space-y-2">
                {releases.slice(0, 5).map((rel) => (
                  <Link
                    key={rel.release_id}
                    to="/research/data"
                    className="flex items-center justify-between rounded-md px-3 py-2 text-sm hover:bg-accent"
                  >
                    <div className="flex items-center gap-2">
                      <StatusBadge status={rel.quality_status} />
                      <span className="text-xs">{rel.dataset_name} v{rel.version}</span>
                    </div>
                    <span className="text-xs text-muted-foreground">{tl({ zh: `${rel.symbol_count} 标的`, en: `${rel.symbol_count} symbols` })}</span>
                  </Link>
                ))}
              </div>
            ) : (
              <p className="py-4 text-center text-sm text-muted-foreground">{tl({ zh: "暂无数据发布", en: "No data releases yet" })}</p>
            )}
          </CardContent>
        </Card>

        {/* Simulation */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Activity className="h-4 w-4 text-muted-foreground" />
              {tl({ zh: "模拟盘会话", en: "Simulation Sessions" })}
            </CardTitle>
          </CardHeader>
          <CardContent>
            {simSessionsError ? (
              <p className="py-4 text-center text-sm text-destructive">{tl({ zh: "模拟会话加载失败，请进入「模拟盘」重试。", en: "Failed to load simulation sessions. Go to “Simulation” to retry." })}</p>
            ) : simSessions && simSessions.length > 0 ? (
              <div className="space-y-2">
                {simSessions.slice(0, 5).map((sess) => (
                  <Link
                    key={sess.session_id}
                    to="/research/simulation"
                    className="flex items-center justify-between rounded-md px-3 py-2 text-sm hover:bg-accent"
                  >
                    <div className="flex items-center gap-2">
                      <StatusBadge status={sess.status} />
                      <span className="font-mono text-xs text-muted-foreground">{sess.session_id}</span>
                    </div>
                    <span className="text-xs text-muted-foreground">{sess.strategy_id}</span>
                  </Link>
                ))}
              </div>
            ) : (
              <div className="flex flex-col items-center py-4 text-center">
                <AlertCircle className="mb-2 h-5 w-5 text-muted-foreground/50" />
                <p className="text-sm text-muted-foreground">{tl({ zh: "暂无模拟会话", en: "No simulation sessions yet" })}</p>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
