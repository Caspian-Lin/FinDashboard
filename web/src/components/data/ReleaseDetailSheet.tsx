import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
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
import { Pager } from "@/components/data/Pager";
import { ReleaseReferencesPanel } from "@/components/data/ReleaseReferences";
import { useT } from "@/i18n";
import { catalogApi } from "@/lib/data-catalog";
import { RELEASE_KIND_DATASET, dataHref } from "@/lib/data-links";
import { formatDateTime, formatNumber } from "@/lib/utils";

const INSTRUMENT_PAGE = 20;

interface ReleaseDetailSheetProps {
  releaseId: string | null;
  onClose: () => void;
}

/** 发布详情抽屉:发布头 + 覆盖 + 逐标的索引 + 引用它的课题/运行(#512)。 */
export function ReleaseDetailSheet({ releaseId, onClose }: ReleaseDetailSheetProps) {
  const { tl, lang } = useT();
  const navigate = useNavigate();
  const [missingOnly, setMissingOnly] = React.useState(false);
  const [offset, setOffset] = React.useState(0);

  React.useEffect(() => {
    setOffset(0);
    setMissingOnly(false);
  }, [releaseId]);

  const release = useQuery({
    queryKey: ["data-catalog", "release", releaseId],
    queryFn: () => catalogApi.release(releaseId as string),
    enabled: Boolean(releaseId),
  });
  const datasetKey = release.data ? RELEASE_KIND_DATASET[release.data.dataset_kind] : undefined;
  const coverage = useQuery({
    queryKey: ["data-catalog", "coverage", datasetKey, releaseId],
    queryFn: () => catalogApi.coverage(datasetKey as string, releaseId as string),
    enabled: Boolean(datasetKey && releaseId),
  });
  const instruments = useQuery({
    queryKey: ["data-catalog", "instruments", releaseId, missingOnly, offset],
    queryFn: () =>
      catalogApi.releaseInstruments(releaseId as string, {
        missing_only: missingOnly || undefined,
        limit: INSTRUMENT_PAGE,
        offset,
      }),
    enabled: Boolean(releaseId),
  });

  return (
    <Sheet open={Boolean(releaseId)} onOpenChange={(open) => !open && onClose()}>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-3xl">
        <SheetHeader className="pr-8 text-left">
          <SheetTitle className="break-all font-mono text-base">
            {releaseId ?? tl({ zh: "发布详情", en: "Release detail" })}
          </SheetTitle>
          <SheetDescription>
            {tl({
              zh: "冻结发布不可变;覆盖与缺口口径来自目录服务(#509)。",
              en: "Frozen releases are immutable; coverage and gaps come from the catalog service (#509).",
            })}
          </SheetDescription>
        </SheetHeader>

        {release.isLoading ? (
          <LoadingState rows={5} className="mt-6" />
        ) : release.isError ? (
          <ErrorState
            className="mt-6"
            message={release.error instanceof Error ? release.error.message : undefined}
            onRetry={() => release.refetch()}
          />
        ) : release.data ? (
          <div className="mt-6 space-y-6">
            <section className="space-y-3" aria-label={tl({ zh: "发布头", en: "Release header" })}>
              <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
                <Field label={tl({ zh: "数据集", en: "Dataset" })} value={release.data.dataset_name} />
                <Field label={tl({ zh: "数据类别", en: "Kind" })} value={release.data.dataset_kind} mono />
                <Field label={tl({ zh: "来源", en: "Source" })} value={release.data.source} />
                <Field label={tl({ zh: "版本", en: "Version" })} value={release.data.version} mono />
                <Field
                  label={tl({ zh: "窗口", en: "Window" })}
                  value={`${release.data.start_date} → ${release.data.end_date}`}
                  mono
                />
                <Field
                  label={tl({ zh: "复权", en: "Adjustment" })}
                  value={release.data.adjustment}
                />
                <Field
                  label={tl({ zh: "标的 / 行数", en: "Symbols / rows" })}
                  value={`${formatNumber(release.data.symbol_count, 0)} / ${formatNumber(release.data.row_count, 0)}`}
                />
                <Field
                  label={tl({ zh: "发布时间", en: "Published" })}
                  value={formatDateTime(release.data.published_at)}
                />
                <Field
                  label={tl({ zh: "Schema", en: "Schema" })}
                  value={release.data.schema_version}
                  mono
                />
                <div className="space-y-1">
                  <dt className="text-xs text-muted-foreground">
                    {tl({ zh: "质量状态", en: "Quality" })}
                  </dt>
                  <dd>
                    <StatusBadge status={release.data.quality_status} />
                  </dd>
                </div>
              </dl>
              <div className="rounded-lg border border-border bg-muted/30 p-3 text-xs">
                <p className="font-mono break-all">
                  {tl({ zh: "checksum", en: "checksum" })}: {release.data.release_checksum}
                </p>
                <p className="mt-1 break-all text-muted-foreground">
                  {tl({ zh: "存储位置", en: "Storage" })}: {release.data.storage_uri}
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    if (!datasetKey) return;
                    // 跳到技术详情后关闭抽屉:否则抽屉长期盖住新 tab,看起来像死按钮。
                    navigate(dataHref({ tab: "tech", dataset: datasetKey, release: release.data?.release_id }));
                    onClose();
                  }}
                  disabled={!datasetKey}
                >
                  {tl({ zh: "技术详情", en: "Tech detail" })}
                </Button>
              </div>
            </section>

            <section className="space-y-3" aria-label={tl({ zh: "覆盖", en: "Coverage" })}>
              <h3 className="text-sm font-semibold">{tl({ zh: "覆盖与缺口", en: "Coverage & gaps" })}</h3>
              {coverage.isLoading ? (
                <LoadingState rows={2} />
              ) : coverage.isError ? (
                <ErrorState
                  message={coverage.error instanceof Error ? coverage.error.message : undefined}
                  onRetry={() => coverage.refetch()}
                />
              ) : coverage.data ? (
                <div className="space-y-2">
                  <CoverageBar
                    coveragePct={coverage.data.coverage_pct}
                    denominator={coverage.data.denominator}
                    expectedSessions={coverage.data.expected_sessions}
                    missingSessions={coverage.data.missing_sessions}
                  />
                  <p className="text-xs text-muted-foreground">
                    {tl({
                      zh: `${coverage.data.symbols_with_gaps} 个标的含缺口 · 缺口交易日 ${coverage.data.missing_sessions} · 分母 ${coverage.data.denominator}`,
                      en: `${coverage.data.symbols_with_gaps} symbols with gaps · ${coverage.data.missing_sessions} missing sessions · denominator ${coverage.data.denominator}`,
                    })}
                  </p>
                  {coverage.data.warnings.length > 0 && (
                    <ul className="list-inside list-disc text-xs text-warning">
                      {coverage.data.warnings.map((warning) => (
                        <li key={warning}>{warning}</li>
                      ))}
                    </ul>
                  )}
                </div>
              ) : null}
            </section>

            <section className="space-y-3" aria-label={tl({ zh: "逐标的索引", en: "Per-instrument index" })}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold">
                  {tl({ zh: "逐标的索引", en: "Per-instrument index" })}
                </h3>
                <div className="flex items-center gap-2">
                  <Checkbox
                    id="release-missing-only"
                    checked={missingOnly}
                    onCheckedChange={(value) => {
                      setMissingOnly(value === true);
                      setOffset(0);
                    }}
                  />
                  <Label htmlFor="release-missing-only" className="text-xs">
                    {tl({ zh: "仅看缺口", en: "Missing only" })}
                  </Label>
                </div>
              </div>
              {instruments.isLoading ? (
                <LoadingState rows={3} />
              ) : instruments.isError ? (
                <ErrorState
                  message={instruments.error instanceof Error ? instruments.error.message : undefined}
                  onRetry={() => instruments.refetch()}
                />
              ) : (instruments.data?.items.length ?? 0) === 0 ? (
                <EmptyState
                  title={tl({ zh: "没有匹配的标的", en: "No matching instruments" })}
                  description={tl({
                    zh: "该发布在当前过滤条件下没有逐标的明细。",
                    en: "No per-instrument rows for this release under the current filter.",
                  })}
                />
              ) : (
                <>
                  <div className="overflow-hidden rounded-lg border border-border">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>{tl({ zh: "标的", en: "Symbol" })}</TableHead>
                          <TableHead>{tl({ zh: "覆盖", en: "Coverage" })}</TableHead>
                          <TableHead>{tl({ zh: "缺/期望", en: "Missing" })}</TableHead>
                          <TableHead>{tl({ zh: "状态", en: "Status" })}</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {instruments.data?.items.map((item) => (
                          <TableRow key={item.code}>
                            <TableCell className="font-mono text-xs">
                              {item.code}
                              <span className="ml-2 text-muted-foreground">{item.name}</span>
                            </TableCell>
                            <TableCell>
                              <CoverageBar coveragePct={item.coverage_pct} denominator={coverage.data?.denominator ?? ""} />
                            </TableCell>
                            <TableCell className="font-mono text-xs">
                              {item.missing_sessions}/{item.expected_sessions}
                            </TableCell>
                            <TableCell>
                              <StatusBadge status={item.ready ? "ready" : "warning"} />
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                  <Pager
                    total={instruments.data?.total ?? 0}
                    limit={INSTRUMENT_PAGE}
                    offset={offset}
                    onChange={setOffset}
                    disabled={instruments.isFetching}
                  />
                </>
              )}
            </section>

            <section className="space-y-3" aria-label={tl({ zh: "引用", en: "References" })}>
              <h3 className="text-sm font-semibold">{tl({ zh: "引用它的课题 / 运行 / 策略", en: "Referenced by" })}</h3>
              <ReleaseReferencesPanel releaseId={releaseId as string} />
            </section>
            <div className="pb-4 text-xs text-muted-foreground">
              <Badge variant="outline" className="mr-2">
                {lang === "zh" ? "只读" : "read-only"}
              </Badge>
              {tl({
                zh: "目录不触发同步或发布;写入口仍在「同步与发布」与旧操作面板。",
                en: "The catalog never triggers sync or publish; write entry points remain in Sync & Publish.",
              })}
            </div>
          </div>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="space-y-1">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={mono ? "break-all font-mono text-xs" : "break-all"}>{value}</dd>
    </div>
  );
}
