import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Database, Filter, RotateCcw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { CoverageBar } from "@/components/data/CoverageBar";
import { DatasetDetailSheet } from "@/components/data/DatasetDetailSheet";
import { Pager } from "@/components/data/Pager";
import { StatusPill } from "@/components/data/StatusPill";
import { useT } from "@/i18n";
import type { LocalizedText } from "@/i18n";
import { CATALOG_DATASET_LABELS, CATALOG_STATUSES, catalogApi } from "@/lib/data-catalog";
import { CATALOG_STATUS_META } from "@/lib/data-labels";
import { formatDateTime, timeAgo } from "@/lib/utils";
import type { DataTabProps } from "./DataBase";

const PAGE_SIZE = 20;
const ALL = "all";

const ASSET_CLASSES: { value: string; label: LocalizedText }[] = [
  { value: "equity", label: { zh: "股票", en: "Equity" } },
  { value: "fixed_income", label: { zh: "固收", en: "Fixed income" } },
  { value: "convertible", label: { zh: "可转债", en: "Convertible" } },
  { value: "commodity", label: { zh: "商品", en: "Commodity" } },
  { value: "derivative", label: { zh: "衍生品", en: "Derivative" } },
];

const ResearchDataOps = React.lazy(() => import("@/components/data/ResearchDataOps"));

/**
 * 数据集 tab:目录列表 + 过滤 + 详情抽屉,并保留旧「分类操作」子视图。
 *
 * 目录视图的过滤全部走服务端参数(禁止前端对 10 万行做本地过滤);旧操作
 * 面板(`ResearchDataOps`)原样挂载,行情拉取 / 财务 / 三表 / 分红 / 转债 /
 * 标的与发布向导保持可达。
 */
export function DatasetsTab({ params, setParams, entries, onOpenRelease }: DataTabProps) {
  const { tl } = useT();
  const [detailKey, setDetailKey] = React.useState<string | null>(null);
  const [keyword, setKeyword] = React.useState(() => params.get("q") ?? "");
  // 来源是非枚举输入:必须受控,否则 reset() 后保留旧显示值,失焦时又把旧值写回 URL。
  const [sourceInput, setSourceInput] = React.useState(() => params.get("source") ?? "");

  const view = params.get("view") === "ops" ? "ops" : "catalog";
  const dataset = params.get("dataset");
  const status = params.get("status");
  const source = params.get("source");
  const assetClass = params.get("asset_class");
  const start = params.get("start");
  const end = params.get("end");
  const q = params.get("q");
  const offset = Math.max(0, Number.parseInt(params.get("offset") ?? "", 10) || 0);

  React.useEffect(() => {
    setKeyword(q ?? "");
  }, [q]);

  React.useEffect(() => {
    setSourceInput(source ?? "");
  }, [source]);

  const query = useQuery({
    queryKey: ["data-catalog", "entries", { dataset, status, source, assetClass, start, end, q, offset }],
    queryFn: () =>
      catalogApi.list({
        dataset: dataset ?? undefined,
        status: status ?? undefined,
        source: source ?? undefined,
        asset_class: assetClass ?? undefined,
        start: start ?? undefined,
        end: end ?? undefined,
        q: q ?? undefined,
        limit: PAGE_SIZE,
        offset,
      }),
    enabled: view === "catalog",
  });

  const setView = (next: string) => {
    if (next === "ops") setParams({ view: "ops", sub: params.get("sub") ?? "bars", offset: null });
    else setParams({ view: null, sub: null });
  };

  const setFilter = (patch: Record<string, string | null>) => {
    setParams({ ...patch, offset: null });
  };

  const reset = () => {
    setKeyword("");
    setSourceInput("");
    setParams({ dataset: null, status: null, source: null, asset_class: null, start: null, end: null, q: null, offset: null });
  };

  const hasFilters = Boolean(dataset || status || source || assetClass || start || end || q);
  const items = query.data?.items ?? [];

  return (
    <div className="space-y-4">
      <Tabs value={view} onValueChange={setView}>
        <TabsList className="h-auto max-w-full justify-start overflow-x-auto">
          <TabsTrigger value="catalog">{tl({ zh: "数据目录", en: "Catalog" })}</TabsTrigger>
          <TabsTrigger value="ops">{tl({ zh: "分类操作(旧版入口)", en: "Operations (legacy)" })}</TabsTrigger>
        </TabsList>
      </Tabs>

      {view === "ops" ? (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            {tl({
              zh: "旧「数据与标的」操作面板:行情拉取、每日指标、财务与三表、分红、转债、标的生命周期与发布向导。内部逻辑未改写,只是换了挂载位置。",
              en: "The legacy data operations panel: market fetch, daily metrics, financials, dividends, convertibles, instrument lifecycle, and the release wizard. Logic is unchanged; only its mount point moved.",
            })}
          </p>
          <React.Suspense fallback={<LoadingState rows={5} />}>
            <ResearchDataOps
              embedded
              sub={params.get("sub") ?? undefined}
              onSubChange={(value) => setParams({ sub: value === "bars" ? null : value })}
              onOpenRelease={onOpenRelease}
            />
          </React.Suspense>
        </div>
      ) : (
        <>
          <section
            className="space-y-3 rounded-lg border border-border bg-card p-4"
            aria-label={tl({ zh: "数据集过滤", en: "Dataset filters" })}
          >
            <div className="flex items-center gap-1 text-sm font-medium">
              <Filter className="h-4 w-4" aria-hidden="true" />
              {tl({ zh: "筛选", en: "Filters" })}
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <div className="space-y-1.5">
                <Label htmlFor="data-dataset-filter">{tl({ zh: "数据集", en: "Dataset" })}</Label>
                <Select
                  value={dataset ?? ALL}
                  onValueChange={(value) => setFilter({ dataset: value === ALL ? null : value })}
                >
                  <SelectTrigger id="data-dataset-filter" aria-label={tl({ zh: "按数据集筛选", en: "Filter by dataset" })}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ALL}>{tl({ zh: "全部数据集", en: "All datasets" })}</SelectItem>
                    {entries.map((entry) => (
                      <SelectItem key={entry.key} value={entry.key}>
                        {tl(CATALOG_DATASET_LABELS[entry.key] ?? { zh: entry.title, en: entry.title_en })}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="data-status-filter">{tl({ zh: "状态", en: "Status" })}</Label>
                <Select
                  value={status ?? ALL}
                  onValueChange={(value) => setFilter({ status: value === ALL ? null : value })}
                >
                  <SelectTrigger id="data-status-filter" aria-label={tl({ zh: "按状态筛选", en: "Filter by status" })}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ALL}>{tl({ zh: "全部状态", en: "All statuses" })}</SelectItem>
                    {CATALOG_STATUSES.map((value) => (
                      <SelectItem key={value} value={value}>
                        {tl(CATALOG_STATUS_META[value].label)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="data-asset-filter">{tl({ zh: "资产类别", en: "Asset class" })}</Label>
                <Select
                  value={assetClass ?? ALL}
                  onValueChange={(value) => setFilter({ asset_class: value === ALL ? null : value })}
                >
                  <SelectTrigger id="data-asset-filter" aria-label={tl({ zh: "按资产类别筛选", en: "Filter by asset class" })}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ALL}>{tl({ zh: "全部资产", en: "All assets" })}</SelectItem>
                    {ASSET_CLASSES.map((item) => (
                      <SelectItem key={item.value} value={item.value}>
                        {tl(item.label)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="data-source-filter">{tl({ zh: "来源", en: "Source" })}</Label>
                <Input
                  id="data-source-filter"
                  value={sourceInput}
                  placeholder="tushare / akshare"
                  onChange={(event) => setSourceInput(event.target.value)}
                  onBlur={() => {
                    const value = sourceInput.trim();
                    if (value !== (source ?? "")) setFilter({ source: value || null });
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      const value = sourceInput.trim();
                      setFilter({ source: value || null });
                    }
                  }}
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="data-start-filter">{tl({ zh: "窗口起", en: "Window from" })}</Label>
                <Input
                  id="data-start-filter"
                  type="date"
                  value={start ?? ""}
                  onChange={(event) => setFilter({ start: event.target.value || null })}
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="data-end-filter">{tl({ zh: "窗口止", en: "Window to" })}</Label>
                <Input
                  id="data-end-filter"
                  type="date"
                  value={end ?? ""}
                  onChange={(event) => setFilter({ end: event.target.value || null })}
                />
              </div>

              <div className="space-y-1.5 sm:col-span-2">
                <Label htmlFor="data-keyword-filter">{tl({ zh: "关键词", en: "Keyword" })}</Label>
                <div className="flex gap-2">
                  <Input
                    id="data-keyword-filter"
                    value={keyword}
                    placeholder={tl({ zh: "数据集 key / 标题", en: "Dataset key / title" })}
                    onChange={(event) => setKeyword(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") setFilter({ q: keyword.trim() || null });
                    }}
                  />
                  <Button variant="outline" onClick={() => setFilter({ q: keyword.trim() || null })}>
                    {tl({ zh: "搜索", en: "Search" })}
                  </Button>
                </div>
              </div>
            </div>
            <div className="flex items-center justify-between gap-2">
              <p className="text-xs text-muted-foreground">
                {tl({
                  zh: "状态与覆盖率均来自服务端目录接口;过滤在服务端执行。",
                  en: "Status and coverage come from the catalog API; filtering runs server-side.",
                })}
              </p>
              {hasFilters && (
                <Button variant="ghost" size="sm" onClick={reset}>
                  <RotateCcw className="h-4 w-4" />
                  {tl({ zh: "重置筛选", en: "Reset filters" })}
                </Button>
              )}
            </div>
          </section>

          {query.isLoading ? (
            <LoadingState rows={6} />
          ) : query.isError ? (
            <ErrorState
              message={query.error instanceof Error ? query.error.message : undefined}
              onRetry={() => query.refetch()}
            />
          ) : items.length === 0 ? (
            <EmptyState
              icon={<Database className="h-8 w-8" />}
              title={tl({ zh: "没有匹配的数据集", en: "No matching datasets" })}
              description={
                hasFilters
                  ? tl({ zh: "当前筛选条件下没有数据集;清除筛选后查看全部。", en: "No datasets under the current filters; clear them to see all." })
                  : tl({ zh: "目录尚未登记数据集。", en: "The catalog has no datasets yet." })
              }
              action={
                hasFilters ? (
                  <Button variant="outline" size="sm" onClick={reset}>
                    {tl({ zh: "清除筛选", en: "Clear filters" })}
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <>
              <div className="overflow-hidden rounded-lg border border-border bg-card">
                {/* min-w:窄视口下表格不挤压,由 Table 内建 overflow-auto 横向滚动。 */}
                <Table className="min-w-[60rem]">
                  <TableHeader>
                    <TableRow>
                      <TableHead>{tl({ zh: "数据集", en: "Dataset" })}</TableHead>
                      <TableHead>{tl({ zh: "状态", en: "Status" })}</TableHead>
                      <TableHead>{tl({ zh: "覆盖率", en: "Coverage" })}</TableHead>
                      <TableHead>{tl({ zh: "标的 / 行数", en: "Symbols / rows" })}</TableHead>
                      <TableHead>{tl({ zh: "本地窗口", en: "Local window" })}</TableHead>
                      <TableHead>{tl({ zh: "最近更新", en: "Updated" })}</TableHead>
                      <TableHead>{tl({ zh: "来源", en: "Source" })}</TableHead>
                      <TableHead className="text-right">{tl({ zh: "操作", en: "Actions" })}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {items.map((entry) => (
                      <TableRow key={entry.key}>
                        <TableCell>
                          <div className="space-y-0.5">
                            <p className="font-medium">{entry.title}</p>
                            <p className="font-mono text-xs text-muted-foreground">{entry.key}</p>
                          </div>
                        </TableCell>
                        <TableCell>
                          <StatusPill status={entry.status} />
                        </TableCell>
                        <TableCell>
                          <CoverageBar
                            coveragePct={entry.coverage_pct}
                            denominator={entry.denominator}
                            className="w-36"
                          />
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {entry.symbol_count ?? "—"} / {entry.row_count ?? "—"}
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {entry.local_start && entry.local_end ? `${entry.local_start} → ${entry.local_end}` : "—"}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {entry.updated_at ? `${formatDateTime(entry.updated_at)}` : "—"}
                          {entry.updated_at && <span className="ml-1">({timeAgo(entry.updated_at)})</span>}
                        </TableCell>
                        <TableCell>
                          <Badge variant="secondary">{entry.release?.source ?? entry.source ?? "—"}</Badge>
                        </TableCell>
                        <TableCell className="text-right">
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => setDetailKey(entry.key)}
                            aria-label={tl({ zh: `查看 ${entry.title} 详情`, en: `Open ${entry.title_en} detail` })}
                          >
                            {tl({ zh: "详情", en: "Detail" })}
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
              <Pager
                total={query.data?.total ?? 0}
                limit={PAGE_SIZE}
                offset={offset}
                onChange={(next) => setParams({ offset: next > 0 ? next : null })}
                disabled={query.isFetching}
              />
            </>
          )}
        </>
      )}

      <DatasetDetailSheet
        datasetKey={detailKey}
        onClose={() => setDetailKey(null)}
        onOpenRelease={onOpenRelease}
      />
    </div>
  );
}
