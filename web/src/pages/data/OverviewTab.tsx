import * as React from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Database, FolderClock, Gauge, ListTodo, Settings2, Wrench } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { CoverageBar } from "@/components/data/CoverageBar";
import { StatusPill, statusHint } from "@/components/data/StatusPill";
import { TopicDataCard } from "@/components/data/TopicDataCard";
import { useT } from "@/i18n";
import type { LocalizedText } from "@/i18n";
import type { CatalogEntry } from "@/lib/data-catalog";
import { dataHref } from "@/lib/data-links";
import { formatDateTime, timeAgo } from "@/lib/utils";
import type { DataTabProps } from "./DataBase";

const STATUS_SUMMARY: {
  status: string;
  label: LocalizedText;
  accent: string;
}[] = [
  { status: "usable", label: { zh: "可研究", en: "Usable" }, accent: "text-success" },
  { status: "ingested_not_published", label: { zh: "已同步 · 待发布", en: "Synced · unpublished" }, accent: "text-warning" },
  { status: "blocked", label: { zh: "受阻", en: "Blocked" }, accent: "text-destructive" },
  { status: "other", label: { zh: "未同步 / 过期", en: "Not synced / stale" }, accent: "text-muted-foreground" },
];

/** 概览:状态卡 + 全局缺口摘要 + 最近发布 + 快捷入口 + 课题数据视图。 */
export function OverviewTab({ entries, entriesLoading, entriesError, entriesErrorMessage, reload, params, setParams }: DataTabProps) {
  const { tl, lang } = useT();
  const [topicInput, setTopicInput] = React.useState(() => params.get("topic") ?? "");
  const topicId = params.get("topic");

  React.useEffect(() => {
    setTopicInput(topicId ?? "");
  }, [topicId]);

  const counts = React.useMemo(() => {
    const map: Record<string, number> = {};
    for (const entry of entries) map[entry.status] = (map[entry.status] ?? 0) + 1;
    return {
      usable: map.usable ?? 0,
      ingested_not_published: map.ingested_not_published ?? 0,
      blocked: map.blocked ?? 0,
      other: (map.not_ingested ?? 0) + (map.stale ?? 0) + (map.unknown ?? 0),
    };
  }, [entries]);

  const gaps = React.useMemo(() => {
    const severity: Record<string, number> = {
      blocked: 0,
      ingested_not_published: 1,
      stale: 2,
      not_ingested: 3,
      unknown: 4,
      usable: 5,
    };
    return entries
      .filter(
        (entry) =>
          entry.status !== "usable" ||
          entry.warnings.length > 0 ||
          (entry.coverage_pct !== null && entry.coverage_pct < 0.995),
      )
      .sort((a, b) => {
        const bySeverity = (severity[a.status] ?? 9) - (severity[b.status] ?? 9);
        if (bySeverity !== 0) return bySeverity;
        return (a.coverage_pct ?? 2) - (b.coverage_pct ?? 2);
      })
      .slice(0, 5);
  }, [entries]);

  const recent = React.useMemo(
    () =>
      entries
        .filter((entry): entry is CatalogEntry & { release: NonNullable<CatalogEntry["release"]> } =>
          Boolean(entry.release),
        )
        .sort((a, b) => (a.release.published_at < b.release.published_at ? 1 : -1))
        .slice(0, 5),
    [entries],
  );

  const quickLinks: { href: string; label: LocalizedText; description: LocalizedText; icon: React.ReactNode }[] = [
    {
      href: dataHref({ tab: "datasets" }),
      label: { zh: "数据集目录", en: "Datasets" },
      description: { zh: "按数据集 / 状态 / 来源筛选并下钻详情", en: "Filter and drill into dataset detail" },
      icon: <Database className="h-4 w-4" />,
    },
    {
      href: dataHref({ tab: "sync" }),
      label: { zh: "同步与发布", en: "Sync & Publish" },
      description: { zh: "同步计划、发布入口与补数接入位", en: "Sync plan, publish entries, backfill slot" },
      icon: <FolderClock className="h-4 w-4" />,
    },
    {
      href: dataHref({ tab: "tech" }),
      label: { zh: "技术详情", en: "Tech detail" },
      description: { zh: "质量、分区、批次、单位、复权、PIT 与 manifest", en: "Quality, partitions, batches, units, PIT, manifest" },
      icon: <Wrench className="h-4 w-4" />,
    },
    {
      href: "/jobs?kind=dataset_sync",
      label: { zh: "任务中心", en: "Jobs" },
      description: { zh: "查看数据集同步与发布任务日志", en: "Inspect sync and publish job logs" },
      icon: <ListTodo className="h-4 w-4" />,
    },
    {
      href: "/settings",
      label: { zh: "设置", en: "Settings" },
      description: { zh: "数据同步配置与预算/来源设置", en: "Data sync configuration and budgets" },
      icon: <Settings2 className="h-4 w-4" />,
    },
  ];

  if (entriesLoading) {
    return (
      <div className="space-y-4" aria-busy="true">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
            <Skeleton key={index} className="h-24 w-full" />
          ))}
        </div>
        <Skeleton className="h-64 w-full" />
        <Skeleton className="h-48 w-full" />
      </div>
    );
  }

  if (entriesError) {
    return <ErrorState message={entriesErrorMessage} onRetry={reload} />;
  }

  if (entries.length === 0) {
    return (
      <EmptyState
        icon={<Database className="h-8 w-8" />}
        title={tl({ zh: "目录为空", en: "Empty catalog" })}
        description={tl({
          zh: "尚未登记任何数据集。检查后端 /api/data/catalog 是否可达。",
          en: "No datasets are registered. Check that /api/data/catalog is reachable.",
        })}
        action={
          <Button variant="outline" size="sm" onClick={reload}>
            {tl({ zh: "重试", en: "Retry" })}
          </Button>
        }
      />
    );
  }

  return (
    <div className="space-y-6">
      <section aria-label={tl({ zh: "目录状态", en: "Catalog status" })}>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {STATUS_SUMMARY.map((item) => {
            const count = (counts as Record<string, number>)[item.status] ?? 0;
            const filterStatus =
              item.status === "other" ? null : item.status === "usable" ? null : item.status;
            return (
              <Card key={item.status}>
                <CardContent className="flex items-center justify-between gap-3 pt-5">
                  <div className="min-w-0">
                    <p className="text-xs text-muted-foreground">{tl(item.label)}</p>
                    <p className={`mt-1 text-2xl font-semibold tabular-nums ${item.accent}`}>{count}</p>
                  </div>
                  {filterStatus ? (
                    <Button asChild variant="ghost" size="sm">
                      <Link to={dataHref({ tab: "datasets", status: filterStatus })} aria-label={tl({ zh: `筛选 ${tl(item.label)}`, en: `Filter ${tl(item.label)}` })}>
                        <ArrowRight className="h-4 w-4" />
                      </Link>
                    </Button>
                  ) : (
                    <Gauge className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                  )}
                </CardContent>
              </Card>
            );
          })}
        </div>
      </section>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader className="flex-row items-start justify-between space-y-0">
            <div>
              <CardTitle className="text-base">{tl({ zh: "全局缺口摘要", en: "Global gap summary" })}</CardTitle>
              <CardDescription>
                {tl({
                  zh: "受阻 → 待发布 → 过期 / 未同步,按严重度排序,最多 5 项。",
                  en: "Blocked, unpublished, stale/not-synced — worst first, top 5.",
                })}
              </CardDescription>
            </div>
            <Button asChild variant="ghost" size="sm">
              <Link to={dataHref({ tab: "datasets" })}>{tl({ zh: "全部", en: "All" })}</Link>
            </Button>
          </CardHeader>
          <CardContent>
            {gaps.length === 0 ? (
              <EmptyState
                title={tl({ zh: "没有可见缺口", en: "No visible gaps" })}
                description={tl({
                  zh: "全部数据集都有可用发布且覆盖完整。",
                  en: "Every dataset has a usable release and full coverage.",
                })}
              />
            ) : (
              <ul className="divide-y divide-border">
                {gaps.map((entry) => (
                  <li key={entry.key} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                    <Link
                      className="min-w-0 truncate font-medium text-primary underline underline-offset-2"
                      to={dataHref({ tab: "datasets", dataset: entry.key })}
                    >
                      {lang === "zh" ? entry.title : entry.title_en}
                    </Link>
                    <StatusPill status={entry.status} />
                    <CoverageBar
                      coveragePct={entry.coverage_pct}
                      denominator={entry.denominator}
                      className="ml-auto w-32"
                    />
                    <span className="w-full text-xs text-muted-foreground">
                      {entry.warnings[0] ?? statusHint(entry.status, lang)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">{tl({ zh: "最近发布", en: "Recent releases" })}</CardTitle>
            <CardDescription>
              {tl({ zh: "按发布时间倒序;点击进入发布详情。", en: "Newest first; click to open release detail." })}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {recent.length === 0 ? (
              <EmptyState
                title={tl({ zh: "尚无发布", en: "No releases yet" })}
                description={tl({
                  zh: "先同步数据,再在「同步与发布」发起冻结发布。",
                  en: "Sync data first, then create a frozen release from Sync & Publish.",
                })}
              />
            ) : (
              <ul className="divide-y divide-border">
                {recent.map((entry) => (
                  <li key={entry.key} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                    <span className="font-mono text-xs">{entry.release.release_id}</span>
                    <Badge variant="secondary">{lang === "zh" ? entry.title : entry.title_en}</Badge>
                    <span className="text-xs text-muted-foreground">
                      {formatDateTime(entry.release.published_at)} · {timeAgo(entry.release.published_at, lang)}
                    </span>
                    <Button asChild variant="ghost" size="sm" className="ml-auto">
                      <Link to={dataHref({ tab: "datasets", dataset: entry.key, release: entry.release.release_id })}>
                        {tl({ zh: "查看", en: "Open" })}
                      </Link>
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{tl({ zh: "快捷入口", en: "Quick links" })}</CardTitle>
          <CardDescription>
            {tl({ zh: "现有同步 / 发布 / 任务入口保持可达;本页只做导航。", en: "Existing sync, publish, and job entry points stay reachable; this page only navigates." })}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {quickLinks.map((link) => (
              <li key={link.href}>
                <Link
                  to={link.href}
                  className="flex items-start gap-3 rounded-lg border border-border p-3 text-sm transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <span className="mt-0.5 text-muted-foreground" aria-hidden="true">
                    {link.icon}
                  </span>
                  <span className="min-w-0">
                    <span className="block font-medium">{tl(link.label)}</span>
                    <span className="block text-xs text-muted-foreground">{tl(link.description)}</span>
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>

      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          setParams({ topic: topicInput.trim() || null });
        }}
        aria-label={tl({ zh: "课题数据视图查询", en: "Topic data scope lookup" })}
      >
        <div className="space-y-1.5">
          <Label htmlFor="data-topic-input">{tl({ zh: "课题 ID", en: "Topic ID" })}</Label>
          <Input
            id="data-topic-input"
            value={topicInput}
            onChange={(event) => setTopicInput(event.target.value)}
            placeholder="RT-..."
            className="w-72 max-w-full font-mono"
            aria-describedby="data-topic-hint"
          />
        </div>
        <Button type="submit" variant="outline">
          {tl({ zh: "查看数据范围", en: "Show data scope" })}
        </Button>
        {topicId && (
          <Button type="button" variant="ghost" onClick={() => setParams({ topic: null })}>
            {tl({ zh: "清除", en: "Clear" })}
          </Button>
        )}
        <p id="data-topic-hint" className="w-full text-xs text-muted-foreground">
          {tl({
            zh: "课题详情页也会展示同一张卡片(只读,不写课题)。",
            en: "The research topic page shows the same card (read-only; never writes the topic).",
          })}
        </p>
      </form>

      <TopicDataCard topicId={topicId} />
    </div>
  );
}
