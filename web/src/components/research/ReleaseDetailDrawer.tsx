import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { datasetApi, type DatasetReleaseDetail } from "@/lib/research";
import { formatDateTime, formatNumber, formatPercent } from "@/lib/utils";
import { useT } from "@/i18n";

/**
 * 数据发布详情抽屉:概览(冻结清单/质量/能力)+ 数据预览(只读尾部行采样)。
 * 由数据页发布列表行点击或 ?release= 深链打开。
 */
export function ReleaseDetailDrawer({
  releaseId,
  onClose,
}: {
  releaseId: string | null;
  onClose: () => void;
}) {
  const { tl } = useT();
  const detailQuery = useQuery({
    queryKey: ["dataset-release-detail", releaseId],
    queryFn: () => datasetApi.releaseDetail(releaseId as string),
    enabled: releaseId !== null,
  });

  return (
    <Sheet open={releaseId !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-3xl">
        <SheetHeader>
          <SheetTitle className="font-mono text-sm">{releaseId ?? ""}</SheetTitle>
          <SheetDescription>
            {tl({
              zh: "冻结发布的清单、质量与只读数据预览;数据以发布时点为准。",
              en: "Frozen release inventory, quality and read-only data preview; data is as of publication.",
            })}
          </SheetDescription>
        </SheetHeader>
        <div className="pb-8">
          {detailQuery.isLoading ? (
            <LoadingState rows={6} />
          ) : detailQuery.isError ? (
            <ErrorState
              message={
                detailQuery.error instanceof Error
                  ? detailQuery.error.message
                  : tl({ zh: "发布详情加载失败", en: "Failed to load release details" })
              }
              onRetry={() => detailQuery.refetch()}
            />
          ) : detailQuery.data ? (
            <ReleaseDetailTabs detail={detailQuery.data} />
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function ReleaseDetailTabs({ detail }: { detail: DatasetReleaseDetail }) {
  const { tl } = useT();
  return (
    <Tabs defaultValue="overview">
      <TabsList>
        <TabsTrigger value="overview">{tl({ zh: "概览", en: "Overview" })}</TabsTrigger>
        <TabsTrigger value="preview">{tl({ zh: "数据预览", en: "Data preview" })}</TabsTrigger>
      </TabsList>
      <TabsContent value="overview" className="mt-4">
        <ReleaseOverview detail={detail} />
      </TabsContent>
      <TabsContent value="preview" className="mt-4">
        <ReleasePreview detail={detail} />
      </TabsContent>
    </Tabs>
  );
}

/**
 * 质量报告的值形态混杂:嵌套对象(coverage / source_by_instrument 等)直接
 * String() 会渲染成 "[object Object]";统一转紧凑 JSON,超长文本(逐标的
 * warnings 串等)截断保持网格可读,全量数据走导出/接口。
 */
function formatQualityValue(value: unknown): string {
  const text =
    value !== null && typeof value === "object"
      ? JSON.stringify(value)
      : String(value);
  return text.length > 200 ? `${text.slice(0, 200)}…` : text;
}

function ReleaseOverview({ detail }: { detail: DatasetReleaseDetail }) {
  const { tl } = useT();
  const qualityReport = detail.quality_report ?? {};
  const qualityEntries = Object.entries(qualityReport)
    .filter(([, value]) => value !== null && value !== undefined)
    .slice(0, 24);

  // issue #349:覆盖率口径含「已查询但无 bar」的区间(停牌/节假日语义),
  // 数据源静默漏数时仍可能为 100%。把质量报告里交易日历审计口径的缺口合计
  // (missing_sessions / suspended_sessions / anomaly_count)与覆盖率并排
  // 展示;质量报告无该块时显示占位,不强造数据。
  const coverageBlock =
    typeof qualityReport["coverage"] === "object" && qualityReport["coverage"] !== null
      ? (qualityReport["coverage"] as Record<string, unknown>)
      : null;
  const asCount = (value: unknown): number | null =>
    typeof value === "number" && Number.isFinite(value) ? value : null;
  const missingSessions = asCount(coverageBlock?.["missing_sessions"]);
  const suspendedSessions = asCount(coverageBlock?.["suspended_sessions"]);
  const anomalyCount = asCount(coverageBlock?.["anomaly_count"]);
  const gapSummary =
    missingSessions === null && suspendedSessions === null && anomalyCount === null
      ? null
      : [
          missingSessions !== null
            ? tl({ zh: `缺 ${missingSessions} 日`, en: `${missingSessions} missing` })
            : null,
          suspendedSessions !== null
            ? tl({ zh: `停牌 ${suspendedSessions}`, en: `${suspendedSessions} suspended` })
            : null,
          anomalyCount !== null
            ? tl({ zh: `异常 ${anomalyCount}`, en: `${anomalyCount} anomalies` })
            : null,
        ]
          .filter((item): item is string => item !== null)
          .join(" · ");

  return (
    <div className="space-y-5 text-xs">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <div>
          <p className="font-medium text-muted-foreground">{tl({ zh: "数据集", en: "Dataset" })}</p>
          <p className="mt-0.5 text-foreground">{detail.dataset_name}</p>
        </div>
        <div>
          <p className="font-medium text-muted-foreground">{tl({ zh: "类型 / 版本", en: "Kind / Version" })}</p>
          <p className="mt-0.5 font-mono text-foreground">
            {detail.dataset_kind ?? "bars"} · v{detail.version}
          </p>
        </div>
        <div>
          <p className="font-medium text-muted-foreground">{tl({ zh: "来源", en: "Source" })}</p>
          <p className="mt-0.5 font-mono text-foreground">{detail.source}</p>
        </div>
        <div>
          <p className="font-medium text-muted-foreground">{tl({ zh: "数据范围", en: "Date range" })}</p>
          <p className="mt-0.5 tabular-nums text-foreground">
            {detail.start_date} ~ {detail.end_date}
          </p>
        </div>
        <div>
          <p className="font-medium text-muted-foreground">{tl({ zh: "周期 / 复权", en: "Period / Adjust" })}</p>
          <p className="mt-0.5 font-mono text-foreground">
            {detail.period} / {detail.adjustment}
          </p>
        </div>
        <div>
          <p className="font-medium text-muted-foreground">{tl({ zh: "质量", en: "Quality" })}</p>
          <p className="mt-0.5">
            <StatusBadge status={detail.quality_status} />
          </p>
        </div>
        <div>
          <p className="font-medium text-muted-foreground">{tl({ zh: "标的数", en: "Symbols" })}</p>
          <p className="mt-0.5 tabular-nums text-foreground">{formatNumber(detail.symbol_count, 0)}</p>
        </div>
        <div>
          <p className="font-medium text-muted-foreground">{tl({ zh: "总行数", en: "Rows" })}</p>
          <p className="mt-0.5 tabular-nums text-foreground">{formatNumber(detail.row_count, 0)}</p>
        </div>
        <div>
          <p className="font-medium text-muted-foreground">{tl({ zh: "覆盖率", en: "Coverage" })}</p>
          <p className="mt-0.5 tabular-nums text-foreground">{formatPercent(detail.coverage_pct, 1)}</p>
        </div>
        <div>
          <p className="font-medium text-muted-foreground">
            {tl({ zh: "数据缺口(审计)", en: "Data gaps (audit)" })}
          </p>
          <p
            className={`mt-0.5 tabular-nums ${
              (missingSessions ?? 0) > 0 ? "text-warning" : "text-foreground"
            }`}
          >
            {gapSummary ?? "—"}
          </p>
        </div>
        <div>
          <p className="font-medium text-muted-foreground">{tl({ zh: "发布时间", en: "Published" })}</p>
          <p className="mt-0.5 tabular-nums text-foreground">{formatDateTime(detail.published_at)}</p>
        </div>
        <div>
          <p className="font-medium text-muted-foreground">{tl({ zh: "存储", en: "Storage" })}</p>
          <p className="mt-0.5 break-all font-mono text-foreground">{detail.storage_uri || "—"}</p>
        </div>
        <div>
          <p className="font-medium text-muted-foreground">{tl({ zh: "发布 checksum", en: "Release checksum" })}</p>
          <p className="mt-0.5 break-all font-mono text-foreground">{detail.release_checksum || "—"}</p>
        </div>
      </div>

      <p className="text-muted-foreground">
        {tl({
          zh: "口径:覆盖率含「已查询但无 bar」的区间(停牌/节假日);真实数据缺口以质量报告 missing_sessions(交易日历审计)为准。",
          en: "Coverage counts queried sessions without bars (suspensions/holidays); real data gaps follow missing_sessions in the quality report (trading-calendar audit).",
        })}
      </p>

      {(detail.known_limitations ?? []).length > 0 && (
        <div className="rounded-md border border-warning/30 bg-warning/5 p-3">
          <p className="font-medium text-foreground">{tl({ zh: "已知局限", en: "Known limitations" })}</p>
          <ul className="mt-1 list-disc pl-4 text-muted-foreground">
            {(detail.known_limitations ?? []).map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <p className="font-medium text-foreground">
          {tl({ zh: "能力快照", en: "Capabilities" })}
          <span className="ml-1 text-muted-foreground">({(detail.capabilities ?? []).length})</span>
        </p>
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {(detail.capabilities ?? []).map((cap) => (
            <Badge key={cap.key} variant={cap.status === "ready" ? "success" : "warning"} className="text-[10px]">
              {cap.key} · {cap.status} ({cap.ready_count}/{cap.symbol_count})
            </Badge>
          ))}
        </div>
      </div>

      {qualityEntries.length > 0 && (
        <div>
          <p className="font-medium text-foreground">{tl({ zh: "质量报告", en: "Quality report" })}</p>
          <div className="mt-1.5 grid grid-cols-2 gap-x-4 gap-y-1 text-muted-foreground md:grid-cols-3">
            {qualityEntries.map(([key, value]) => (
              <p key={key} className="break-all">
                <span className="font-mono">{key}</span>: {formatQualityValue(value)}
              </p>
            ))}
          </div>
        </div>
      )}

      <div>
        <p className="font-medium text-foreground">
          {tl({ zh: "冻结标的清单", en: "Frozen instruments" })}
          <span className="ml-1 text-muted-foreground">({(detail.instruments ?? []).length})</span>
        </p>
        <div className="mt-1.5 max-h-80 overflow-y-auto rounded-md border border-border scrollbar-thin">
          <Table>
            <TableHeader className="sticky top-0 bg-card">
              <TableRow>
                <TableHead>code</TableHead>
                <TableHead>{tl({ zh: "名称", en: "Name" })}</TableHead>
                <TableHead>{tl({ zh: "类型", en: "Type" })}</TableHead>
                <TableHead className="text-right">{tl({ zh: "行数", en: "Rows" })}</TableHead>
                <TableHead className="text-right">{tl({ zh: "缺日", en: "Missing" })}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(detail.instruments ?? []).map((inst) => (
                <TableRow key={inst.code}>
                  <TableCell className="font-mono text-[11px]">{inst.code}</TableCell>
                  <TableCell className="max-w-[10rem] truncate text-[11px]">{inst.name}</TableCell>
                  <TableCell className="text-[11px] text-muted-foreground">{inst.instrument_type}</TableCell>
                  <TableCell className="text-right tabular-nums text-[11px]">{formatNumber(inst.row_count, 0)}</TableCell>
                  <TableCell className="text-right tabular-nums text-[11px]">
                    {inst.missing_sessions > 0 ? (
                      <span className="text-warning">{inst.missing_sessions}</span>
                    ) : (
                      "0"
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  );
}

function ReleasePreview({ detail }: { detail: DatasetReleaseDetail }) {
  const { tl } = useT();
  const [symbol, setSymbol] = useState<string>("");
  const symbols = useMemo(() => (detail.instruments ?? []).map((item) => item.code), [detail.instruments]);
  const effectiveSymbol = symbol || symbols[0] || "";

  const previewQuery = useQuery({
    queryKey: ["dataset-release-preview", detail.release_id, effectiveSymbol],
    queryFn: () => datasetApi.releasePreview(detail.release_id, effectiveSymbol || undefined, 20),
    enabled: effectiveSymbol !== "",
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-xs font-medium text-muted-foreground" htmlFor="release-preview-symbol">
          {tl({ zh: "标的", en: "Symbol" })}
        </label>
        <select
          id="release-preview-symbol"
          value={effectiveSymbol}
          onChange={(event) => setSymbol(event.target.value)}
          className="max-w-[16rem] rounded-md border border-input bg-card px-2 py-1.5 font-mono text-xs text-foreground"
        >
          {symbols.map((code) => (
            <option key={code} value={code}>
              {code}
            </option>
          ))}
        </select>
      </div>

      {previewQuery.isLoading ? (
        <LoadingState rows={5} />
      ) : previewQuery.isError ? (
        <ErrorState
          message={
            previewQuery.error instanceof Error
              ? previewQuery.error.message
              : tl({ zh: "预览加载失败", en: "Failed to load preview" })
          }
          onRetry={() => previewQuery.refetch()}
        />
      ) : previewQuery.data ? (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
            <span className="font-mono">{previewQuery.data.label}</span>
            <span>
              {tl({
                zh: `尾部 ${previewQuery.data.rows.length} 行 / 共 ${previewQuery.data.total_rows} 行`,
                en: `last ${previewQuery.data.rows.length} of ${previewQuery.data.total_rows} rows`,
              })}
            </span>
          </div>
          {previewQuery.data.rows.length === 0 ? (
            <EmptyState
              title={tl({ zh: "无数据行", en: "No rows" })}
              description={tl({ zh: "该文件存在但没有任何数据行。", en: "The file exists but contains no rows." })}
            />
          ) : (
            <div className="max-h-[26rem] overflow-auto rounded-md border border-border scrollbar-thin">
              <Table>
                <TableHeader className="sticky top-0 bg-card">
                  <TableRow>
                    {previewQuery.data.columns.map((column) => (
                      <TableHead key={column} className="whitespace-nowrap font-mono text-[11px]">
                        {column}
                      </TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {previewQuery.data.rows.map((row, i) => (
                    <TableRow key={i}>
                      {previewQuery.data.columns.map((column) => (
                        <TableCell key={column} className="whitespace-nowrap font-mono text-[11px]">
                          {row[column] === null || row[column] === undefined ? (
                            <span className="text-muted-foreground/50">null</span>
                          ) : (
                            String(row[column])
                          )}
                        </TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
          <div className="flex items-center justify-between">
            <p className="break-all font-mono text-[11px] text-muted-foreground">
              {previewQuery.data.artifact}
            </p>
            <Button variant="outline" size="sm" onClick={() => previewQuery.refetch()} disabled={previewQuery.isFetching}>
              {tl({ zh: "刷新", en: "Refresh" })}
            </Button>
          </div>
        </>
      ) : null}
    </div>
  );
}
