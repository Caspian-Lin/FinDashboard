import { Link } from "react-router-dom";
import type { ReactNode } from "react";
import { CircleDashed, FolderClock, HardDriveDownload, ListTodo, Settings2, UploadCloud } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { CoverageBar } from "@/components/data/CoverageBar";
import { StatusPill, statusHint } from "@/components/data/StatusPill";
import { useT } from "@/i18n";
import type { LocalizedText } from "@/i18n";
import { dataHref } from "@/lib/data-links";
import { formatDateTime } from "@/lib/utils";
import type { DataTabProps } from "./DataBase";

interface EntryLink {
  href: string;
  label: LocalizedText;
  description: LocalizedText;
  icon: ReactNode;
}

/** 同步与发布(第一阶段):只读状态面板 + 既有入口链接 + #511 接入占位。 */
export function SyncPublishTab({ entries, params }: DataTabProps) {
  const { tl, lang } = useT();
  const topicId = params.get("topic");

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
      <Alert>
        <FolderClock className="h-4 w-4" />
        <AlertTitle>{tl({ zh: "第一阶段:只读状态面板", en: "Phase 1: read-only status" })}</AlertTitle>
        <AlertDescription>
          {tl({
            zh: "本页只展示同步/发布现状与既有入口;保存设置与执行仍然分离,补数请求的预览→提交交互在 #511 落地后接入(第二阶段)。",
            en: "This page only shows sync/publish status and existing entry points. Saving settings and running them stay separate; the backfill preview→submit flow lands with #511 (phase 2).",
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

      {/*
        TODO(#511) 第二阶段接入位:
        - 范围预检 POST /api/data/backfills/preview(scope → 候选发布/理由/交集/缺口/受影响策略)
        - 显式提交 POST /api/data/backfills(幂等键 + 配额预占 + publish 开关)
        - 进度/取消/重试读 background_jobs(复用任务中心轮询)
        第一阶段不接任何未实现接口:按钮禁用,只保留 #511 的说明与 topic 预填参数。
      */}
      <Card className="border-dashed">
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-2 text-base">
            <CircleDashed className="h-4 w-4" aria-hidden="true" />
            {tl({ zh: "补数请求 · 接入位", en: "Backfill request · entry point" })}
            <Badge variant="outline">#511</Badge>
          </CardTitle>
          <CardDescription>
            {tl({
              zh: "范围预检 → 候选发布 → 显式提交 → 进度/取消/重试。第一阶段只保留占位,不提交任何请求。",
              en: "Scope precheck → candidate releases → explicit submit → progress/cancel/retry. Phase 1 keeps a placeholder and submits nothing.",
            })}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">
            {tl({
              zh: "接入后:本区按课题范围预填(scope),预览新发布 ID / 版本 / 标的数,用户显式确认后才提交;默认 publish=true 但可按请求关闭。",
              en: "Once wired: prefilled from the topic scope, previewing new release IDs / versions / symbol counts; nothing runs until the user explicitly confirms. publish=true by default but opt-out per request.",
            })}
          </p>
          {topicId ? (
            <p className="rounded-md border border-border bg-muted/30 p-2 font-mono text-xs">
              {tl({ zh: "预填课题", en: "Prefilled topic" })}: {topicId}
              <Link
                className="ml-3 text-primary underline underline-offset-2"
                to={dataHref({ tab: "overview", topic: topicId })}
              >
                {tl({ zh: "查看课题数据范围", en: "View topic data scope" })}
              </Link>
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">
              {tl({
                zh: "从课题详情页进入时会带上 ?topic=RT-... 作为范围预填。",
                en: "Arriving from a topic detail page carries ?topic=RT-... as the scope prefill.",
              })}
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button disabled aria-disabled="true" title={tl({ zh: "第二阶段接入 #511 后可用", en: "Available after #511 lands in phase 2" })}>
              {tl({ zh: "范围预检(待接入)", en: "Scope precheck (pending)" })}
            </Button>
            <Button variant="outline" disabled aria-disabled="true">
              {tl({ zh: "提交补数请求(待接入)", en: "Submit backfill (pending)" })}
            </Button>
            <span className="text-xs text-muted-foreground">
              {tl({ zh: "第二阶段 #511", en: "Phase 2 · #511" })}
            </span>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
