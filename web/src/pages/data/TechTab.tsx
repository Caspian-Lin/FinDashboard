import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ExternalLink, FileCode2, ListTodo, Wrench } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { CoverageBar } from "@/components/data/CoverageBar";
import { StatusPill } from "@/components/data/StatusPill";
import { useT } from "@/i18n";
import { CATALOG_DATASET_LABELS, catalogApi } from "@/lib/data-catalog";
import { dataHref } from "@/lib/data-links";
import { formatDateTime, formatNumber } from "@/lib/utils";
import type { DataTabProps } from "./DataBase";

/** 技术详情(issue #512):质量 / 分区 / 批次 / 单位 / 复权 / PIT / manifest 入口。 */
export function TechTab({ params, setParams, entries, entriesLoading, entriesError, reload, onOpenRelease }: DataTabProps) {
  const { tl } = useT();
  const dataset = params.get("dataset") ?? entries[0]?.key ?? null;
  const selectedEntry = entries.find((entry) => entry.key === dataset);
  const detail = useQuery({
    queryKey: ["data-catalog", "dataset", dataset],
    queryFn: () => catalogApi.dataset(dataset as string),
    enabled: Boolean(dataset),
    staleTime: 15_000,
  });
  const coverage = useQuery({
    queryKey: ["data-catalog", "coverage", dataset, null],
    queryFn: () => catalogApi.coverage(dataset as string),
    enabled: Boolean(dataset),
  });

  if (entriesLoading) return <LoadingState rows={6} />;
  if (entriesError) return <ErrorState onRetry={reload} />;
  if (!dataset) {
    return (
      <EmptyState
        icon={<Wrench className="h-8 w-8" />}
        title={tl({ zh: "没有可展示的数据集", en: "No dataset to inspect" })}
        description={tl({ zh: "目录为空,先确认后端 /api/data/catalog 可达。", en: "The catalog is empty; check /api/data/catalog." })}
      />
    );
  }

  const entry = detail.data?.entry ?? selectedEntry;
  const latestRelease = detail.data?.releases[0];
  const title = entry ? (entry.title) : dataset;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1.5">
          <Label htmlFor="tech-dataset-select">{tl({ zh: "数据集", en: "Dataset" })}</Label>
          <Select value={dataset} onValueChange={(value) => setParams({ dataset: value, release: null })}>
            <SelectTrigger id="tech-dataset-select" className="w-64" aria-label={tl({ zh: "选择数据集", en: "Choose dataset" })}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {entries.map((item) => (
                <SelectItem key={item.key} value={item.key}>
                  {tl(CATALOG_DATASET_LABELS[item.key] ?? { zh: item.title, en: item.title_en })}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button asChild variant="outline" size="sm">
            <Link to={`/jobs?kind=dataset_sync`}>
              <ListTodo className="h-4 w-4" />
              {tl({ zh: "同步任务日志", en: "Sync job logs" })}
            </Link>
          </Button>
          <Button asChild variant="outline" size="sm">
            <Link to={`/jobs?kind=dataset_publish`}>
              <ListTodo className="h-4 w-4" />
              {tl({ zh: "发布任务日志", en: "Publish job logs" })}
            </Link>
          </Button>
        </div>
      </div>

      {detail.isLoading ? (
        <LoadingState rows={6} />
      ) : detail.isError ? (
        <ErrorState
          message={detail.error instanceof Error ? detail.error.message : undefined}
          onRetry={() => detail.refetch()}
        />
      ) : detail.data && entry ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">{tl({ zh: "质量与状态", en: "Quality & status" })}</CardTitle>
              <CardDescription>{title}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <StatusPill status={entry.status} />
                {latestRelease && <StatusBadge status={latestRelease.quality_status} />}
                <Badge variant="outline">{entry.storage}</Badge>
              </div>
              {entry.warnings.length > 0 && (
                <ul className="list-inside list-disc text-xs text-warning">
                  {entry.warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              )}
              <CoverageBar
                coveragePct={coverage.data?.coverage_pct ?? entry.coverage_pct}
                denominator={coverage.data?.denominator ?? entry.denominator}
                expectedSessions={coverage.data?.expected_sessions}
                missingSessions={coverage.data?.missing_sessions}
              />
              <dl className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
                <div>
                  <dt className="text-muted-foreground">{tl({ zh: "标的数", en: "Symbols" })}</dt>
                  <dd className="font-mono">{entry.symbol_count ?? "—"}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">{tl({ zh: "行数", en: "Rows" })}</dt>
                  <dd className="font-mono">{formatNumber(entry.row_count, 0)}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">{tl({ zh: "缓存文件", en: "Cache files" })}</dt>
                  <dd className="font-mono">{entry.cache_files}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">{tl({ zh: "缓存大小", en: "Cache bytes" })}</dt>
                  <dd className="font-mono">{formatNumber(entry.cache_bytes, 0)}</dd>
                </div>
              </dl>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">{tl({ zh: "分区与口径", en: "Partitions & semantics" })}</CardTitle>
              <CardDescription>
                {tl({ zh: "窗口、复权、PIT 与交易日历均由 registry / manifest 决定。", en: "Window, adjustment, PIT, and calendar come from the registry/manifest." })}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <dl className="space-y-2">
                <Row label={tl({ zh: "本地窗口", en: "Local window" })} value={
                  entry.local_start && entry.local_end ? `${entry.local_start} → ${entry.local_end}` : "—"
                } mono />
                <Row label={tl({ zh: "复权", en: "Adjustment" })} value={detail.data.adjustment ?? "none"} mono />
                <Row label={tl({ zh: "日历市场", en: "Calendar market" })} value={detail.data.calendar_market} mono />
                <div>
                  <dt className="text-xs text-muted-foreground">PIT</dt>
                  <dd className="text-xs">{detail.data.pit_rule}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">{tl({ zh: "原始单位", en: "Raw units" })}</dt>
                  <dd>
                    <ul className="list-inside list-disc text-xs text-muted-foreground">
                      {detail.data.unit_notes.map((note) => (
                        <li key={note}>{note}</li>
                      ))}
                    </ul>
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">{tl({ zh: "字段", en: "Fields" })}</dt>
                  <dd className="flex flex-wrap gap-1 pt-1">
                    {detail.data.fields.map((field) => (
                      <Badge key={field} variant="outline" className="font-mono text-[11px]">
                        {field}
                      </Badge>
                    ))}
                  </dd>
                </div>
              </dl>
            </CardContent>
          </Card>

          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle className="text-base">{tl({ zh: "批次", en: "Batches" })}</CardTitle>
              <CardDescription>
                {tl({ zh: "最近 20 个同步批次;失败与质量拒绝单列。", en: "Latest 20 sync batches; failures and quality rejections listed separately." })}
              </CardDescription>
            </CardHeader>
            <CardContent>
              {detail.data.batches.length === 0 ? (
                <p className="text-xs text-muted-foreground">{tl({ zh: "该数据集没有同步批次(如 bars 走缓存目录)。", en: "No sync batches for this dataset (e.g. bars uses the cache directory)." })}</p>
              ) : (
                <div className="overflow-hidden rounded-lg border border-border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>{tl({ zh: "批次", en: "Batch" })}</TableHead>
                        <TableHead>{tl({ zh: "版本", en: "Version" })}</TableHead>
                        <TableHead>{tl({ zh: "状态", en: "Status" })}</TableHead>
                        <TableHead>{tl({ zh: "质量", en: "Quality" })}</TableHead>
                        <TableHead>{tl({ zh: "接收/接受/拒绝", en: "received/accepted/rejected" })}</TableHead>
                        <TableHead>{tl({ zh: "完成", en: "Completed" })}</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {detail.data.batches.map((batch) => (
                        <TableRow key={batch.id}>
                          <TableCell className="font-mono text-xs">{batch.id}</TableCell>
                          <TableCell className="max-w-48 truncate font-mono text-xs">{batch.dataset_version}</TableCell>
                          <TableCell>
                            <StatusBadge status={batch.status} />
                          </TableCell>
                          <TableCell className="text-xs">{batch.quality_status ?? "—"}</TableCell>
                          <TableCell className="font-mono text-xs">
                            {batch.received_rows}/{batch.accepted_rows}/{batch.rejected_rows}
                          </TableCell>
                          <TableCell className="text-xs text-muted-foreground">{formatDateTime(batch.completed_at)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}
              {detail.data.failures.length > 0 && (
                <ul className="mt-2 list-inside list-disc text-xs text-warning">
                  {detail.data.failures.map((batch) => (
                    <li key={`failure-${batch.id}`}>
                      #{batch.id} {batch.status}: {batch.error_summary ?? batch.quality_status ?? "—"}
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>

          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <FileCode2 className="h-4 w-4" aria-hidden="true" />
                {tl({ zh: "Manifest 入口", en: "Manifest entry" })}
              </CardTitle>
              <CardDescription>
                {tl({
                  zh: "只展示发布头的 checksum / 存储位置 / 能力位;manifest 整包按需加载(不随页面请求)。",
                  en: "Only the release header checksum, storage URI, and capabilities are shown; the full manifest loads on demand.",
                })}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {latestRelease ? (
                <>
                  <dl className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
                    <Row label="release_id" value={latestRelease.release_id} mono />
                    <Row label="schema_version" value={latestRelease.schema_version} mono />
                    <Row label="checksum" value={latestRelease.release_checksum} mono />
                    <Row label={tl({ zh: "存储位置", en: "Storage" })} value={latestRelease.storage_uri} mono />
                  </dl>
                  <div className="flex flex-wrap gap-2 pt-1">
                    {latestRelease.capabilities.length === 0 ? (
                      <span className="text-xs text-muted-foreground">
                        {tl({ zh: "该发布未声明能力位。", en: "This release declares no capabilities." })}
                      </span>
                    ) : (
                      latestRelease.capabilities.map((capability, index) => (
                        <Badge key={index} variant="secondary" className="font-mono text-[11px]">
                          {String((capability as { key?: unknown }).key ?? JSON.stringify(capability))}
                        </Badge>
                      ))
                    )}
                  </div>
                  <div className="flex flex-wrap gap-2 pt-2">
                    <Button variant="outline" size="sm" onClick={() => onOpenRelease(latestRelease.release_id)}>
                      {tl({ zh: "发布详情与引用", en: "Release detail & references" })}
                    </Button>
                    <Button asChild variant="ghost" size="sm">
                      <Link to={dataHref({ tab: "datasets", dataset, release: latestRelease.release_id })}>
                        <ExternalLink className="h-4 w-4" />
                        {tl({ zh: "在目录中打开", en: "Open in catalog" })}
                      </Link>
                    </Button>
                  </div>
                </>
              ) : (
                <p className="text-xs text-muted-foreground">
                  {tl({ zh: "该数据集没有冻结发布,因此没有 manifest。", en: "No frozen release, so no manifest." })}
                </p>
              )}
            </CardContent>
          </Card>
        </div>
      ) : null}
    </div>
  );
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={mono ? "break-all font-mono text-xs" : "break-all text-xs"}>{value}</dd>
    </div>
  );
}
