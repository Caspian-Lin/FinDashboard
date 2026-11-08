import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { CoverageBar } from "@/components/data/CoverageBar";
import { StatusPill } from "@/components/data/StatusPill";
import { useT } from "@/i18n";
import { catalogApi } from "@/lib/data-catalog";
import { statusHint } from "@/lib/data-labels";
import { dataHref } from "@/lib/data-links";
import { formatDateTime, timeAgo } from "@/lib/utils";

interface DatasetDetailSheetProps {
  datasetKey: string | null;
  onClose: () => void;
  onOpenRelease: (releaseId: string) => void;
}

/**
 * 数据集详情面板:发布头、覆盖、批次、字段、单位/复权/PIT(#512)。
 *
 * 所有数值来自 `GET /datasets/{dataset}` 与 `GET .../coverage`,抽屉只做展示
 * 与跳转;发布详情另行打开 `ReleaseDetailSheet`。
 */
export function DatasetDetailSheet({ datasetKey, onClose, onOpenRelease }: DatasetDetailSheetProps) {
  const { tl, lang } = useT();
  const navigate = useNavigate();
  const detail = useQuery({
    queryKey: ["data-catalog", "dataset", datasetKey],
    queryFn: () => catalogApi.dataset(datasetKey as string),
    enabled: Boolean(datasetKey),
  });
  const coverage = useQuery({
    queryKey: ["data-catalog", "coverage", datasetKey, null],
    queryFn: () => catalogApi.coverage(datasetKey as string),
    enabled: Boolean(datasetKey),
  });

  const entry = detail.data?.entry;

  return (
    <Sheet open={Boolean(datasetKey)} onOpenChange={(open) => !open && onClose()}>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-3xl">
        <SheetHeader className="pr-8 text-left">
          <SheetTitle>{entry ? (lang === "zh" ? entry.title : entry.title_en) : datasetKey}</SheetTitle>
          <SheetDescription className="break-all font-mono text-xs">{datasetKey}</SheetDescription>
        </SheetHeader>

        {detail.isLoading ? (
          <LoadingState rows={5} className="mt-6" />
        ) : detail.isError ? (
          <ErrorState
            className="mt-6"
            message={detail.error instanceof Error ? detail.error.message : undefined}
            onRetry={() => detail.refetch()}
          />
        ) : detail.data && entry ? (
          <div className="mt-6 space-y-6">
            <section className="space-y-2" aria-label={tl({ zh: "状态", en: "Status" })}>
              <div className="flex flex-wrap items-center gap-2">
                <StatusPill status={entry.status} />
                <Badge variant="outline">{entry.storage}</Badge>
                {entry.publishable && <Badge variant="secondary">{tl({ zh: "可发布", en: "Publishable" })}</Badge>}
                {entry.source && <Badge variant="secondary">{entry.source}</Badge>}
              </div>
              <p className="text-xs text-muted-foreground">{statusHint(entry.status, lang)}</p>
              <p className="text-xs text-muted-foreground">
                {tl({ zh: "最近更新", en: "Last update" })}: {formatDateTime(entry.updated_at)}
                {entry.updated_at ? ` (${timeAgo(entry.updated_at, lang)})` : ""}
              </p>
              {entry.warnings.length > 0 && (
                <ul className="list-inside list-disc text-xs text-warning">
                  {entry.warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              )}
            </section>

            <section className="space-y-2" aria-label={tl({ zh: "覆盖", en: "Coverage" })}>
              <h3 className="text-sm font-semibold">{tl({ zh: "覆盖与缺口", en: "Coverage & gaps" })}</h3>
              {coverage.isLoading ? (
                <LoadingState rows={2} />
              ) : coverage.isError ? (
                <ErrorState
                  message={coverage.error instanceof Error ? coverage.error.message : undefined}
                  onRetry={() => coverage.refetch()}
                />
              ) : coverage.data ? (
                <>
                  <CoverageBar
                    coveragePct={coverage.data.coverage_pct}
                    denominator={coverage.data.denominator}
                    expectedSessions={coverage.data.expected_sessions}
                    missingSessions={coverage.data.missing_sessions}
                  />
                  <p className="text-xs text-muted-foreground">
                    {tl({
                      zh: `分母:${coverage.data.denominator} · 含缺口标的 ${coverage.data.symbols_with_gaps}`,
                      en: `Denominator: ${coverage.data.denominator} · symbols with gaps ${coverage.data.symbols_with_gaps}`,
                    })}
                  </p>
                  {coverage.data.missing_preview.length > 0 && (
                    <p className="font-mono text-xs text-muted-foreground">
                      {tl({ zh: "缺口预览", en: "Gap preview" })}:{" "}
                      {coverage.data.missing_preview.map((item) => item.code).join(" · ")}
                    </p>
                  )}
                  {coverage.data.warnings.length > 0 && (
                    <ul className="list-inside list-disc text-xs text-warning">
                      {coverage.data.warnings.map((warning) => (
                        <li key={warning}>{warning}</li>
                      ))}
                    </ul>
                  )}
                </>
              ) : null}
            </section>

            <section className="space-y-2" aria-label={tl({ zh: "口径", en: "Semantics" })}>
              <h3 className="text-sm font-semibold">{tl({ zh: "单位 / 复权 / PIT / 日历", en: "Units / adjustment / PIT / calendar" })}</h3>
              <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
                <div>
                  <dt className="text-xs text-muted-foreground">{tl({ zh: "复权口径", en: "Adjustment" })}</dt>
                  <dd className="font-mono text-xs">{detail.data.adjustment ?? tl({ zh: "非价格数据(固定 none)", en: "Non-price (fixed none)" })}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">{tl({ zh: "交易日历市场", en: "Calendar market" })}</dt>
                  <dd className="font-mono text-xs">{detail.data.calendar_market}</dd>
                </div>
                <div className="sm:col-span-2">
                  <dt className="text-xs text-muted-foreground">PIT</dt>
                  <dd className="text-xs">{detail.data.pit_rule}</dd>
                </div>
                <div className="sm:col-span-2">
                  <dt className="text-xs text-muted-foreground">{tl({ zh: "原始单位", en: "Raw units" })}</dt>
                  <dd>
                    <ul className="list-inside list-disc text-xs text-muted-foreground">
                      {detail.data.unit_notes.map((note) => (
                        <li key={note}>{note}</li>
                      ))}
                    </ul>
                  </dd>
                </div>
              </dl>
            </section>

            <section className="space-y-2" aria-label={tl({ zh: "字段", en: "Fields" })}>
              <h3 className="text-sm font-semibold">
                {tl({ zh: `字段 (${detail.data.fields.length})`, en: `Fields (${detail.data.fields.length})` })}
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {detail.data.fields.map((field) => (
                  <Badge key={field} variant="outline" className="font-mono text-[11px]">
                    {field}
                  </Badge>
                ))}
              </div>
            </section>

            <section className="space-y-2" aria-label={tl({ zh: "发布", en: "Releases" })}>
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-sm font-semibold">{tl({ zh: "历史发布", en: "Releases" })}</h3>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => navigate(dataHref({ tab: "tech", dataset: datasetKey }))}
                >
                  {tl({ zh: "技术详情", en: "Tech detail" })}
                </Button>
              </div>
              {detail.data.releases.length === 0 ? (
                <EmptyState
                  title={tl({ zh: "尚无发布", en: "No releases yet" })}
                  description={tl({
                    zh: "该数据集没有冻结发布;同步后可在「同步与发布」发起发布。",
                    en: "This dataset has no frozen release yet. Sync first, then publish from Sync & Publish.",
                  })}
                />
              ) : (
                <ul className="divide-y divide-border rounded-lg border border-border">
                  {detail.data.releases.map((release) => (
                    <li key={release.release_id} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
                      <span className="font-mono text-xs">{release.release_id}</span>
                      <Badge variant="secondary">{release.version}</Badge>
                      <StatusBadge status={release.quality_status} />
                      <span className="text-xs text-muted-foreground">
                        {release.start_date} → {release.end_date} · {release.symbol_count} 标的
                      </span>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="ml-auto"
                        onClick={() => onOpenRelease(release.release_id)}
                      >
                        {tl({ zh: "查看发布", en: "Open release" })}
                      </Button>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="space-y-2" aria-label={tl({ zh: "批次", en: "Batches" })}>
              <h3 className="text-sm font-semibold">{tl({ zh: "最近同步批次", en: "Recent sync batches" })}</h3>
              {detail.data.batches.length === 0 ? (
                <p className="text-xs text-muted-foreground">{tl({ zh: "无同步批次记录。", en: "No sync batches." })}</p>
              ) : (
                <div className="overflow-hidden rounded-lg border border-border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>{tl({ zh: "批次", en: "Batch" })}</TableHead>
                        <TableHead>{tl({ zh: "状态", en: "Status" })}</TableHead>
                        <TableHead>{tl({ zh: "接收/接受", en: "Received/accepted" })}</TableHead>
                        <TableHead>{tl({ zh: "完成时间", en: "Completed" })}</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {detail.data.batches.map((batch) => (
                        <TableRow key={batch.id}>
                          <TableCell className="font-mono text-xs">{batch.id}</TableCell>
                          <TableCell>
                            <StatusBadge status={batch.status} />
                          </TableCell>
                          <TableCell className="font-mono text-xs">
                            {batch.received_rows}/{batch.accepted_rows}
                          </TableCell>
                          <TableCell className="text-xs text-muted-foreground">
                            {formatDateTime(batch.completed_at)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}
              {detail.data.failures.length > 0 && (
                <div className="space-y-1">
                  <h4 className="text-xs font-semibold text-warning">
                    {tl({ zh: "失败 / 质量拒绝批次", en: "Failed / quality-rejected batches" })}
                  </h4>
                  <ul className="list-inside list-disc text-xs text-warning">
                    {detail.data.failures.map((batch) => (
                      <li key={`failure-${batch.id}`}>
                        #{batch.id} {batch.status}: {batch.error_summary ?? batch.quality_status ?? "—"}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </section>
          </div>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}
