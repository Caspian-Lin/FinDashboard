import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Database, ListTodo, Wrench } from "lucide-react";
import TopicOverview from "@/components/research/TopicOverview";
import { Button } from "@/components/ui/button";
import { PageContainer } from "@/components/ui/page-container";
import { PageHeader } from "@/components/ui/page-header";
import { StatusBadge } from "@/components/ui/status-badge";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { useLanguage, useT } from "@/i18n";
import { datasetApi, researchRunApi } from "@/lib/research";
import { timeAgo } from "@/lib/utils";

const shortcuts = [
  { to: "/data", label: { zh: "数据基座", en: "Data Base" }, icon: Database },
  { to: "/jobs", label: { zh: "任务与审计", en: "Tasks & audit" }, icon: ListTodo },
  { to: "/research/tools", label: { zh: "高级工具", en: "Advanced tools" }, icon: Wrench },
];

export default function ResearchHome({ workspace = false }: { workspace?: boolean }) {
  const { tl } = useT();
  const { lang } = useLanguage();
  const runs = useQuery({
    queryKey: ["research-runs", "home"],
    queryFn: () => researchRunApi.list({ limit: 5 }),
  });
  const releases = useQuery({
    queryKey: ["dataset-releases", "home"],
    queryFn: () => datasetApi.releases({ limit: 5 }),
  });

  return (
    <PageContainer>
      <PageHeader title={workspace
        ? tl({ zh: "工作总览", en: "Workspace overview" })
        : tl({ zh: "研究总览", en: "Research overview" })}
      />
      <TopicOverview />
      <nav aria-label={tl({ zh: "常用入口", en: "Frequently used tools" })} className="flex flex-wrap gap-2">
        {shortcuts.map(({ to, label, icon: Icon }) => (
          <Button key={to} variant="outline" className="min-h-11 px-3 sm:min-h-9" asChild>
            <Link to={to}><Icon className="mr-2 size-4" aria-hidden="true" />{tl(label)}</Link>
          </Button>
        ))}
      </nav>
      <div className="grid gap-5 lg:grid-cols-2">
        <section aria-labelledby="recent-runs" className="min-w-0">
          <h2 id="recent-runs" className="mb-3 font-semibold">{tl({ zh: "最近研究运行", en: "Recent research runs" })}</h2>
          {runs.isPending && <LoadingState />}
          {runs.isError && <ErrorState onRetry={() => void runs.refetch()} />}
          {runs.data?.length === 0 && <EmptyState title={tl({ zh: "暂无研究运行", en: "No research runs yet" })} />}
          {!!runs.data?.length && (
            <ul className="divide-y rounded-lg border bg-card">
              {runs.data.map((run) => (
                <li key={run.run_id}>
                  <Link
                    to={`/research/runs?run=${encodeURIComponent(run.run_id)}`}
                    className="flex min-h-11 flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2 text-sm hover:bg-muted focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
                  >
                    <StatusBadge status={run.status} />
                    <span className="min-w-0 flex-1 break-all font-mono text-xs">{run.run_id}</span>
                    <span className="text-xs text-muted-foreground">{timeAgo(run.created_at, lang)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section aria-labelledby="recent-releases" className="min-w-0">
          <h2 id="recent-releases" className="mb-3 font-semibold">{tl({ zh: "最近数据发布", en: "Recent data releases" })}</h2>
          {releases.isPending && <LoadingState />}
          {releases.isError && <ErrorState onRetry={() => void releases.refetch()} />}
          {releases.data?.length === 0 && <EmptyState title={tl({ zh: "暂无数据发布", en: "No data releases yet" })} />}
          {!!releases.data?.length && (
            <ul className="divide-y rounded-lg border bg-card">
              {releases.data.map((release) => (
                <li key={release.release_id}>
                  <Link
                    to={`/data?tab=datasets&release=${encodeURIComponent(release.release_id)}`}
                    className="flex min-h-11 items-center gap-2 px-3 py-2 text-sm hover:bg-muted focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
                  >
                    <StatusBadge status={release.quality_status} />
                    <span className="min-w-0 flex-1 break-words">{release.dataset_name} v{release.version}</span>
                    <ArrowRight className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </PageContainer>
  );
}
