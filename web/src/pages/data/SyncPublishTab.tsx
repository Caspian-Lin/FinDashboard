import { Link } from "react-router-dom";
import type { ReactNode } from "react";
import { FolderClock, HardDriveDownload, ListTodo, Settings2, UploadCloud } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { LoadingState, ErrorState } from "@/components/ui/states";
import { BackfillRequestCard } from "@/components/data/BackfillRequestCard";
import { CoverageBar } from "@/components/data/CoverageBar";
import { StatusPill } from "@/components/data/StatusPill";
import { useT } from "@/i18n";
import type { LocalizedText } from "@/i18n";
import { statusHint } from "@/lib/data-labels";
import { dataHref } from "@/lib/data-links";
import { formatDateTime } from "@/lib/utils";
import type { DataTabProps } from "./DataBase";
import { ReleaseBundlePanel } from "./ReleaseBundlePanel";

interface EntryLink {
  href: string;
  label: LocalizedText;
  description: LocalizedText;
  icon: ReactNode;
}

/** 同步与发布状态、操作入口和共享补数。 */
export function SyncPublishTab({
  entries,
  params,
  entriesLoading,
  entriesError,
  entriesErrorMessage,
  reload,
}: DataTabProps) {
  const { tl, lang } = useT();
  const topicId = params.get("topic");

  // 目录未就绪时不能渲染「全部数据集均处于可研究状态」这类健康文案:
  // 首加载(entries=[]+loading)与 500(entries=[])都会走到 pending.length===0。
  if (entriesLoading) return <LoadingState rows={4} />;
  if (entriesError) return <ErrorState message={entriesErrorMessage} onRetry={reload} />;

  const entryLinks: EntryLink[] = [
    {
      href: "/settings",
      label: { zh: "同步计划与预算设置", en: "Sync plan & budget settings" },
      description: {
        zh: "市场/资产范围、来源优先级与调度时间的唯一保存位置(#525 设置总入口)",
        en: "The only place to save market/asset scope, source priority, and schedules (#525)",
      },
      icon: <Settings2 className="h-4 w-4" />,
    },
    {
      href: dataHref({ tab: "datasets", view: "ops", sub: "bars" }),
      label: { zh: "行情拉取与发布向导", en: "Market fetch & release wizard" },
      description: {
        zh: "批量下载、质量修复、冻结发布(旧操作面板原样保留)",
        en: "Bulk download, quality repair, frozen release (legacy panel, unchanged)",
      },
      icon: <HardDriveDownload className="h-4 w-4" />,
    },
    {
      href: dataHref({ tab: "datasets", view: "ops", sub: "metrics" }),
      label: { zh: "研究数据集同步", en: "Research dataset sync" },
      description: {
        zh: "每日指标 / 财务指标 / 三表 / 分红 / 可转债的批次与发布",
        en: "Batches and releases for metrics, financials, statements, dividends, convertibles",
      },
      icon: <UploadCloud className="h-4 w-4" />,
    },
    {
      href: "/jobs?kind=dataset_sync",
      label: { zh: "任务中心 · 数据集同步", en: "Jobs · dataset sync" },
      description: { zh: "同步批次进度、失败原因与重试", en: "Batch progress, failure reasons, retries" },
      icon: <ListTodo className="h-4 w-4" />,
    },
  ];

  const pending = entries.filter((entry) => entry.status !== "usable");
  const lastUpdated = entries
    .map((entry) => entry.updated_at)
    .filter((value): value is string => Boolean(value))
    .sort()
    .at(-1);

  return (
    <div className="space-y-4">
      <ReleaseBundlePanel entries={entries} onImported={reload} />
      <Alert>
        <FolderClock className="h-4 w-4" />
        <AlertTitle>{tl({ zh: "同步与补数", en: "Sync and backfill" })}</AlertTitle>
        <AlertDescription>
          {tl({
            zh: "查看同步和发布状态，按缺口补数。预检通过后显式提交，补齐的数据可生成新的共享发布。",
            en: "Check sync and publish status, then preview and explicitly submit a request to fill gaps. Completed backfills can create new shared releases.",
          })}
        </AlertDescription>
      </Alert>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle className="text-base">{tl({ zh: "同步状态", en: "Sync status" })}</CardTitle>
            <CardDescription>
              {tl({
                zh: "状态来自目录接口;待发布 / 受阻的数据集应优先处理。",
                en: "Status comes from the catalog API; unpublished or blocked datasets deserve attention first.",
              })}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {pending.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {tl({ zh: "全部数据集均处于可研究状态。", en: "Every dataset is currently usable." })}
              </p>
            ) : (
              <ul className="divide-y divide-border">
                {pending.map((entry) => (
                  <li key={entry.key} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                    <Link
                      className="font-medium text-primary underline underline-offset-2"
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
            <p className="mt-3 text-xs text-muted-foreground">
              {tl({ zh: "最近更新", en: "Last update" })}: {formatDateTime(lastUpdated)}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">{tl({ zh: "既有入口", en: "Existing entry points" })}</CardTitle>
            <CardDescription>
              {tl({ zh: "本阶段不迁移写操作,只保证可达。", en: "Writes are not migrated in this phase; only kept reachable." })}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="space-y-2">
              {entryLinks.map((link) => (
                <li key={link.href}>
                  <Link
                    to={link.href}
                    className="flex items-start gap-2 rounded-md border border-border p-2 text-sm transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
      </div>

      <BackfillRequestCard key={topicId ?? "global"} topicId={topicId} />
    </div>
  );
}
