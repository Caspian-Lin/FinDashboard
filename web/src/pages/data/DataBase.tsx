import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Navigate, useSearchParams } from "react-router-dom";
import { LayoutGrid, ListTree, SlidersHorizontal, Wrench } from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { PageContainer } from "@/components/ui/page-container";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ReleaseDetailSheet } from "@/components/data/ReleaseDetailSheet";
import { useT } from "@/i18n";
import { catalogApi, type CatalogEntry } from "@/lib/data-catalog";
import {
  DATA_TABS,
  parseDataTab,
  legacyResearchDataTarget,
  RELEASE_KIND_DATASET,
  type DataTab,
} from "@/lib/data-links";
import { DatasetsTab } from "./DatasetsTab";
import { OverviewTab } from "./OverviewTab";
import { SyncPublishTab } from "./SyncPublishTab";
import { TechTab } from "./TechTab";
import { useCatalogEntries } from "./useCatalogEntries";

export interface DataTabProps {
  /** 当前 URL 查询参数(只读;写操作走 setParams)。 */
  params: URLSearchParams;
  setParams: (patch: Record<string, string | number | null | undefined>) => void;
  /** 全量目录条目(过滤下拉 / 概览统计共用一次请求)。 */
  entries: CatalogEntry[];
  entriesLoading: boolean;
  entriesError: boolean;
  entriesErrorMessage?: string;
  reload: () => void;
  onOpenRelease: (releaseId: string) => void;
}

/**
 * 共享数据基座页面(issue #512 第一阶段)。
 *
 * 四个一级 tab:概览 / 数据集 / 同步与发布(只读状态 + #511 占位)/ 技术详情。
 * 页签、数据集、发布与过滤条件全部进 URL:旧深链可保全参数跳转,
 * 刷新与分享不丢上下文。
 */
export default function DataBase() {
  const { tl } = useT();
  const [params, setSearchParams] = useSearchParams();
  const rawTab = params.get("tab");
  const releaseId = params.get("release");
  // 新式 `?release=` 深链缺 tab 时落「数据集」tab(计划 6.1:tab 缺省按 release 推断)。
  const tab = rawTab
    ? parseDataTab(rawTab)
    : releaseId
      ? "datasets"
      : "overview";
  const all = useCatalogEntries();
  // 只读发布头:仅用于把 `?release=` 深链落到对应数据集的过滤条件。
  const releaseHeader = useQuery({
    queryKey: ["data-catalog", "release", releaseId],
    queryFn: () => catalogApi.release(releaseId as string),
    enabled: Boolean(releaseId),
    staleTime: 30_000,
  });

  const setTab = React.useCallback(
    (next: string) => {
      const search = new URLSearchParams(params);
      if (next === "overview") search.delete("tab");
      else search.set("tab", next);
      setSearchParams(search, { replace: false });
    },
    [params, setSearchParams],
  );

  const setParams = React.useCallback(
    (patch: Record<string, string | number | null | undefined>) => {
      const search = new URLSearchParams(params);
      for (const [key, value] of Object.entries(patch)) {
        if (value === undefined || value === null || value === "") search.delete(key);
        else search.set(key, String(value));
      }
      setSearchParams(search, { replace: true });
    },
    [params, setSearchParams],
  );

  const onOpenRelease = React.useCallback(
    (id: string) => {
      const search = new URLSearchParams(params);
      search.set("release", id);
      if (!search.get("tab")) search.set("tab", "datasets");
      setSearchParams(search, { replace: true });
    },
    [params, setSearchParams],
  );

  const closeRelease = React.useCallback(() => {
    const search = new URLSearchParams(params);
    search.delete("release");
    setSearchParams(search, { replace: true });
  }, [params, setSearchParams]);

  const reload = React.useCallback(() => {
    void all.refetch();
  }, [all]);

  // release 深链:补齐 tab=datasets 与 dataset 过滤,保证分享/刷新后仍落在对应发布。
  // 每个 release 只自动补一次,避免用户切到其他 tab 后被反复拉回。
  const autoLinkedRelease = React.useRef<string | null>(null);
  React.useEffect(() => {
    if (!releaseId || !releaseHeader.data) return;
    if (autoLinkedRelease.current === releaseId) return;
    autoLinkedRelease.current = releaseId;
    const datasetKey = RELEASE_KIND_DATASET[releaseHeader.data.dataset_kind];
    const search = new URLSearchParams(params);
    if (!search.get("tab")) search.set("tab", "datasets");
    if (datasetKey && !search.get("dataset")) search.set("dataset", datasetKey);
    setSearchParams(search, { replace: true });
  }, [releaseId, releaseHeader.data, params, setSearchParams]);

  // 旧 `/data?tab=<旧别名>` 深链(重定向前的书签)同样映射,不静默丢参数。
  if (rawTab && !(DATA_TABS as readonly string[]).includes(rawTab)) {
    return <Navigate to={legacyResearchDataTarget(`?${params.toString()}`)} replace />;
  }

  const tabProps: DataTabProps = {
    params,
    setParams,
    entries: all.data?.items ?? [],
    entriesLoading: all.isLoading,
    entriesError: all.isError,
    entriesErrorMessage: all.error instanceof Error ? all.error.message : undefined,
    reload,
    onOpenRelease,
  };

  return (
    <PageContainer>
      <PageHeader
        title={tl({ zh: "数据基座", en: "Shared Data Base" })}
        description={tl({
          zh: "共享研究数据的唯一入口:能用哪些、缺什么、怎样补。目录只读,同步与发布仍走既有任务与发布入口。",
          en: "The single entry point for shared research data: what is usable, what is missing, and how to backfill. The catalog is read-only; sync and publish keep their existing entry points.",
        })}
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={reload}
            disabled={all.isFetching}
            aria-label={tl({ zh: "刷新目录", en: "Refresh catalog" })}
          >
            {tl({ zh: "刷新", en: "Refresh" })}
          </Button>
        }
      />

      <Tabs value={tab} onValueChange={setTab}>
        {/* 窄屏:TabsList 横向滚动,不折行挤压(issue #512 窄屏用例)。 */}
        <TabsList className="flex h-auto w-full max-w-full justify-start overflow-x-auto">
          <TabsTrigger value="overview">
            <LayoutGrid className="mr-1.5 h-4 w-4" />
            {tl({ zh: "概览", en: "Overview" })}
          </TabsTrigger>
          <TabsTrigger value="datasets">
            <ListTree className="mr-1.5 h-4 w-4" />
            {tl({ zh: "数据集", en: "Datasets" })}
          </TabsTrigger>
          <TabsTrigger value="sync">
            <SlidersHorizontal className="mr-1.5 h-4 w-4" />
            {tl({ zh: "同步与发布", en: "Sync & Publish" })}
          </TabsTrigger>
          <TabsTrigger value="tech">
            <Wrench className="mr-1.5 h-4 w-4" />
            {tl({ zh: "技术详情", en: "Tech" })}
          </TabsTrigger>
        </TabsList>

        {DATA_TABS.map((value) => (
          <TabsContent key={value} value={value} className="space-y-4">
            {value === tab ? <TabBody tab={value} props={tabProps} /> : null}
          </TabsContent>
        ))}
      </Tabs>

      <ReleaseDetailSheet releaseId={releaseId} onClose={closeRelease} />
    </PageContainer>
  );
}

function TabBody({ tab, props }: { tab: DataTab; props: DataTabProps }) {
  switch (tab) {
    case "datasets":
      return <DatasetsTab {...props} />;
    case "sync":
      return <SyncPublishTab {...props} />;
    case "tech":
      return <TechTab {...props} />;
    case "overview":
    default:
      return <OverviewTab {...props} />;
  }
}
