import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { LoadingState, ErrorState, EmptyState } from "@/components/ui/states";
import { useT } from "@/i18n";
import { workspaceApi } from "@/lib/research-workspace";

export default function TopicOverview() {
  const { tl } = useT();
  const query = useQuery({ queryKey: ["topics", "overview"], queryFn: () => workspaceApi.topics() });

  return (
    <section aria-labelledby="recent-topics" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="recent-topics" className="font-semibold">{tl({ zh: "继续研究课题", en: "Continue a research topic" })}</h2>
        <Button variant="outline" className="min-h-11 sm:min-h-9" asChild>
          <Link to="/research/topics">{tl({ zh: "打开课题", en: "Open topics" })}</Link>
        </Button>
      </div>
      {query.isPending && <LoadingState />}
      {query.isError && <ErrorState onRetry={() => void query.refetch()} />}
      {query.data?.items.length === 0 && (
        <EmptyState
          title={tl({ zh: "还没有研究课题", en: "No research topics yet" })}
          description={tl({
            zh: "在课题页记录问题、需要补齐的证据和下一步。",
            en: "Open Topics to record a research question, gaps in evidence, and the next step.",
          })}
        />
      )}
      {!!query.data?.items.length && (
        <ul className="divide-y rounded-lg border bg-card">
          {query.data.items.slice(0, 5).map((topic) => (
            <li key={topic.topic_id} className="space-y-1 px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <Link
                  className="flex min-h-11 items-center break-words text-sm font-semibold hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring sm:min-h-8"
                  to={`/research/topics?topic=${encodeURIComponent(topic.topic_id)}`}
                >
                  {topic.title}
                </Link>
                <StatusBadge status={topic.status} />
              </div>
              <p className="max-w-prose break-words text-sm text-muted-foreground">{topic.question}</p>
              {topic.next_step && (
                <p className="max-w-prose break-words text-sm">
                  <span className="font-medium">{tl({ zh: "下一步", en: "Next step" })} · </span>{topic.next_step}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
