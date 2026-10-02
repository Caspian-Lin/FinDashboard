import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { LoadingState, ErrorState, EmptyState } from "@/components/ui/states";
import { workspaceApi } from "@/lib/research-workspace";
export default function TopicOverview() {
  const q = useQuery({ queryKey: ["topics", "overview"], queryFn: () => workspaceApi.topics() });
  return (
    <section className="mb-6 space-y-4 border-b border-border pb-6">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-lg font-semibold">继续研究问题</h2>
        <Button variant="outline" asChild>
          <Link to="/research/topics">全部课题与记忆</Link>
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">
        从当前问题和证据缺口继续，可反复验证、暂停或分支尝试。工具入口不表示研究已经完成了某个阶段。
      </p>
      {q.isLoading && <LoadingState />}
      {q.isError && <ErrorState message={String(q.error)} onRetry={() => q.refetch()} />}
      {q.data?.items.length === 0 && (
        <EmptyState
          title="为当前研究建立一个课题"
          description="关联已有策略、回测与研究记忆，让每轮的理由、失败和下一步可查。"
          action={
            <Button asChild>
              <Link to="/research/topics">打开研究课题</Link>
            </Button>
          }
        />
      )}
      {q.data?.items.slice(0, 5).map((t) => (
        <article key={t.topic_id} className="space-y-2 border-t border-border pt-4">
          <Link
            className="font-medium text-primary underline"
            to={`/research/topics?topic=${encodeURIComponent(t.topic_id)}`}
          >
            {t.title}
          </Link>
          <p className="max-w-prose text-sm">{t.question}</p>
          <p className="text-xs text-muted-foreground">
            状态：{t.status} · 工作结论：{t.conclusion} · 目标版本：{t.goal.version}
          </p>
          <p className="text-sm">证据缺口：{t.open_questions.join("；") || "尚未记录"}</p>
          <p className="text-sm">下一步：{t.next_step || "尚未记录"}</p>
        </article>
      ))}
    </section>
  );
}
