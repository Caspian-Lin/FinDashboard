import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Database, TriangleAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/status-badge";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { CoverageBar } from "@/components/data/CoverageBar";
import { useT } from "@/i18n";
import type { LocalizedText } from "@/i18n";
import { catalogApi } from "@/lib/data-catalog";
import { dataHref } from "@/lib/data-links";
import { formatDateTime } from "@/lib/utils";

/** 课题缺口原因码 → 文案(#512 稳定码;服务端只回码,不在 API 里拼文案)。 */
export const TOPIC_GAP_LABELS: Record<string, LocalizedText> = {
  release_not_found: { zh: "引用的发布不存在", en: "Referenced release missing" },
  run_not_found: { zh: "引用的研究运行不存在", en: "Referenced run missing" },
  dataset_not_published: { zh: "数据集尚无可用发布", en: "Dataset has no usable release" },
  dataset_kind_unmapped: { zh: "发布类型未注册到目录", en: "Release kind not in catalog" },
  quality_blocked: { zh: "发布质量受阻", en: "Release quality blocked" },
  coverage_gap: { zh: "覆盖缺口", en: "Coverage gap" },
};

const NEXT_STEP_LABELS: Record<string, LocalizedText> = {
  ready: { zh: "数据可研究", en: "Data is usable" },
  fix_gaps: { zh: "有覆盖缺口,建议补数", en: "Coverage gaps; backfill suggested" },
  blocked: { zh: "引用或质量受阻,先修复", en: "Blocked references or quality; fix first" },
  no_refs: { zh: "课题尚未引用数据发布", en: "No data releases referenced" },
};

const RESOLVED_BY_LABELS: Record<string, LocalizedText> = {
  refs: { zh: "按轮次引用推导", en: "Derived from round refs" },
  data_scope: { zh: "按课题显式数据范围", en: "Explicit topic data scope" },
};

interface TopicDataCardProps {
  topicId: string | null | undefined;
  className?: string;
  /** 详情页内嵌时隐藏卡片外框(仍保留标题层级)。 */
  bare?: boolean;
}

/**
 * 课题数据视图(issue #512)。
 *
 * 只消费 `GET /api/data/catalog/topics/{id}/coverage`:发布集合、覆盖、缺口、
 * last_sync 与 next_step 全部由服务端解析,浏览器不聚合多份发布。
 */
export function TopicDataCard({ topicId, className, bare }: TopicDataCardProps) {
  const { tl } = useT();
  const query = useQuery({
    queryKey: ["data-catalog", "topic-coverage", topicId],
    queryFn: () => catalogApi.topicCoverage(topicId as string),
    enabled: Boolean(topicId),
    staleTime: 30_000,
  });

  const title = tl({ zh: "课题数据范围", en: "Topic data scope" });
  const description = tl({
    zh: "按课题轮次的 source_refs 解析发布集合(#514 落地后优先显式 data_scope),复用目录覆盖口径。",
    en: "Resolves the release set from the topic's round source_refs (explicit data_scope wins once #514 lands), reusing catalog coverage.",
  });

  const body = !topicId ? (
    <EmptyState
      icon={<Database className="h-6 w-6" />}
      title={tl({ zh: "未选择课题", en: "No topic selected" })}
      description={tl({
        zh: "从「研究课题」详情进入,或在数据基座概览上方填入课题 ID(RT-...)。",
        en: "Open it from a research topic, or paste a topic ID (RT-...) on the data base overview.",
      })}
    />
  ) : query.isLoading ? (
    <LoadingState rows={3} />
  ) : query.isError ? (
    <ErrorState
      message={query.error instanceof Error ? query.error.message : undefined}
      onRetry={() => query.refetch()}
    />
  ) : query.data ? (
    (() => {
      const data = query.data;
      const empty = data.releases.length === 0 && data.gaps.length === 0;
      return (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Badge variant="outline">
              {tl(RESOLVED_BY_LABELS[data.coverage.resolved_by] ?? {
                zh: data.coverage.resolved_by,
                en: data.coverage.resolved_by,
              })}
            </Badge>
            <span>
              {tl({
                zh: `${data.coverage.release_count} 份发布 · ${data.coverage.dataset_count} 个数据集`,
                en: `${data.coverage.release_count} releases · ${data.coverage.dataset_count} datasets`,
              })}
            </span>
            <span>
              {tl({ zh: "最近同步/发布", en: "Last sync/publish" })}: {formatDateTime(data.last_sync)}
            </span>
            {data.coverage.warnings.length > 0 && (
              <span className="inline-flex items-center gap-1 text-warning">
                <TriangleAlert className="h-3.5 w-3.5" />
                {data.coverage.warnings.join(" / ")}
              </span>
            )}
          </div>

          {empty ? (
            <EmptyState
              icon={<Database className="h-6 w-6" />}
              title={tl({ zh: "本课题还没有引用数据发布", en: "This topic references no releases yet" })}
              description={tl({
                zh: "先做范围预检(补数请求接入位,见 #511),或把已有发布 ID 写进轮次 source_refs。",
                en: "Start with a scope precheck (backfill entry point, #511) or add release IDs to round source_refs.",
              })}
              action={
                <Button asChild variant="outline" size="sm">
                  <Link to={dataHref({ tab: "sync", topic: topicId })}>
                    {tl({ zh: "从本课题发起范围预检", en: "Start a scope precheck for this topic" })}
                  </Link>
                </Button>
              }
            />
          ) : (
            <>
              {data.releases.length > 0 && (
                <ul className="divide-y divide-border rounded-lg border border-border">
                  {data.releases.map((release) => (
                    <li key={release.release_id} className="flex flex-wrap items-center gap-3 px-3 py-2 text-sm">
                      <span className="font-mono text-xs">{release.release_id}</span>
                      <Badge variant="secondary">{release.dataset}</Badge>
                      <StatusBadge status={release.quality_status} />
                      <CoverageBar
                        coveragePct={release.coverage_pct}
                        denominator={release.denominator}
                        expectedSessions={release.expected_sessions}
                        missingSessions={release.missing_sessions}
                        className="w-40"
                      />
                      <span className="text-xs text-muted-foreground">
                        {release.start_date} → {release.end_date}
                      </span>
                      <Link
                        className="ml-auto text-xs text-primary underline underline-offset-2"
                        to={dataHref({ tab: "datasets", dataset: release.dataset, release: release.release_id })}
                      >
                        {tl({ zh: "在目录中查看", en: "Open in catalog" })}
                      </Link>
                    </li>
                  ))}
                </ul>
              )}

              {data.gaps.length > 0 && (
                <div className="rounded-lg border border-warning/30 bg-warning/5 p-3">
                  <h4 className="flex items-center gap-1 text-xs font-semibold text-warning">
                    <TriangleAlert className="h-3.5 w-3.5" />
                    {tl({ zh: `缺口 ${data.gaps.length} 项`, en: `${data.gaps.length} gaps` })}
                  </h4>
                  <ul className="mt-2 space-y-1 text-xs">
                    {data.gaps.map((gap, index) => (
                      <li key={`${gap.release_id}-${gap.reason}-${index}`} className="flex flex-wrap gap-2">
                        <Badge variant="warning">
                          {tl(TOPIC_GAP_LABELS[gap.reason] ?? { zh: gap.reason, en: gap.reason })}
                        </Badge>
                        <span className="font-mono">{gap.release_id || gap.dataset}</span>
                        <span className="text-muted-foreground">{gap.message}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={data.next_step === "ready" ? "success" : "warning"}>
                  {tl(NEXT_STEP_LABELS[data.next_step] ?? { zh: data.next_step, en: data.next_step })}
                </Badge>
                <Button asChild variant="outline" size="sm">
                  <Link
                    to={
                      data.next_step === "ready"
                        ? dataHref({ tab: "datasets" })
                        : dataHref({ tab: "sync", topic: topicId })
                    }
                  >
                    {data.next_step === "ready"
                      ? tl({ zh: "浏览数据集", en: "Browse datasets" })
                      : tl({ zh: "去补数(接入位)", en: "Backfill (entry point)" })}
                  </Link>
                </Button>
              </div>
            </>
          )}
        </div>
      );
    })()
  ) : null;

  if (bare) {
    return (
      <section className={className} aria-label={title}>
        <h3 className="mb-1 text-sm font-semibold">{title}</h3>
        <p className="mb-3 text-xs text-muted-foreground">{description}</p>
        {body}
      </section>
    );
  }

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>{body}</CardContent>
    </Card>
  );
}
