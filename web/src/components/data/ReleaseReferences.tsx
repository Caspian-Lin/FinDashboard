import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Link2, ShieldAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { StatusBadge } from "@/components/ui/status-badge";
import { useT } from "@/i18n";
import type { LocalizedText } from "@/i18n";
import { catalogApi, type ReleaseReference } from "@/lib/data-catalog";

/** 引用上限与后端 `limit` 上限一致(1–100);没有 offset,用扩大 limit 分页。 */
const REFERENCE_PAGE = 20;
const REFERENCE_MAX = 100;

const KIND_LABELS: Record<string, LocalizedText> = {
  research_run: { zh: "研究运行", en: "Research run" },
  strategy_spec: { zh: "策略版本", en: "Strategy version" },
  research_topic: { zh: "研究课题", en: "Research topic" },
};

const KIND_ORDER: Record<string, number> = {
  research_run: 0,
  strategy_spec: 1,
  research_topic: 2,
};

/**
 * 发布引用列表(课题 / 运行 / 策略)。
 *
 * 后端 `GET /releases/{id}/references` 只有 `limit`(≤100)没有 offset:
 * 这里按 20 → 50 → 100 放宽 limit 实现「限权分页」,并在溢出时显式提示
 * 「已到接口上限」,不假装是全量。
 */
export function ReleaseReferencesPanel({ releaseId }: { releaseId: string }) {
  const { tl, lang } = useT();
  const [limit, setLimit] = React.useState(REFERENCE_PAGE);
  const query = useQuery({
    queryKey: ["data-catalog", "references", releaseId, limit],
    queryFn: () => catalogApi.references(releaseId, limit),
    staleTime: 30_000,
  });

  if (query.isLoading) return <LoadingState rows={3} />;
  if (query.isError) {
    return (
      <ErrorState
        message={query.error instanceof Error ? query.error.message : undefined}
        onRetry={() => query.refetch()}
      />
    );
  }
  const items = [...(query.data?.items ?? [])].sort(
    (a, b) => (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9),
  );

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">
          {tl({
            zh: "引用该发布的课题 / 运行 / 策略(只读,按创建时间倒序)",
            en: "Topics / runs / strategies referencing this release (read-only, newest first)",
          })}
        </p>
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          <ShieldAlert className="h-3.5 w-3.5" />
          {tl({
            zh: `限权分页:单次最多 ${REFERENCE_MAX} 条`,
            en: `Limited paging: at most ${REFERENCE_MAX} per request`,
          })}
        </span>
      </div>
      {items.length === 0 ? (
        <EmptyState
          icon={<Link2 className="h-6 w-6" />}
          title={tl({ zh: "暂无引用", en: "No references yet" })}
          description={tl({
            zh: "没有任何课题、研究运行或策略版本引用这份发布。",
            en: "No topic, research run, or strategy version references this release.",
          })}
        />
      ) : (
        <ul className="divide-y divide-border rounded-lg border border-border">
          {items.map((item) => (
            <ReferenceRow key={`${item.kind}:${item.ref_id}`} item={item} lang={lang} />
          ))}
        </ul>
      )}
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          {items.length >= limit
            ? tl({
                zh: `已显示 ${items.length} 条(可能还有更多引用)。`,
                en: `${items.length} shown; there may be more references.`,
              })
            : tl({ zh: `共 ${items.length} 条。`, en: `${items.length} total.` })}
        </p>
        {items.length >= limit && limit < REFERENCE_MAX && (
          <Button variant="outline" size="sm" onClick={() => setLimit(REFERENCE_MAX)}>
            {tl({ zh: "加载更多", en: "Load more" })}
          </Button>
        )}
      </div>
    </div>
  );
}

function ReferenceRow({ item, lang }: { item: ReleaseReference; lang: "zh" | "en" }) {
  const { tl } = useT();
  const label = KIND_LABELS[item.kind];
  return (
    <li className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
      <Badge variant="secondary">{label ? tl(label) : item.kind}</Badge>
      {item.deep_link ? (
        <a
          className="inline-flex min-w-0 items-center gap-1 break-all font-mono text-xs text-primary underline underline-offset-2"
          href={item.deep_link}
        >
          {item.ref_id}
          <ExternalLink className="h-3 w-3 shrink-0" />
        </a>
      ) : (
        <span className="break-all font-mono text-xs">{item.ref_id}</span>
      )}
      <StatusBadge status={item.status} />
      <span className="ml-auto text-xs text-muted-foreground">
        {item.created_at ? new Date(item.created_at).toLocaleString(lang === "zh" ? "zh-CN" : "en-US") : "—"}
      </span>
      {item.created_at === null && (
        <span className="sr-only">{tl({ zh: "无创建时间", en: "No created time" })}</span>
      )}
    </li>
  );
}
