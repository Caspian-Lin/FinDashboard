import { useQuery } from "@tanstack/react-query";
import { catalogApi } from "@/lib/data-catalog";

/** 全量目录条目(10 个数据集;limit=200 覆盖后端上限,不轮询)。 */
export const ALL_ENTRIES_LIMIT = 200;

export function useCatalogEntries() {
  return useQuery({
    queryKey: ["data-catalog", "all"],
    queryFn: () => catalogApi.list({ limit: ALL_ENTRIES_LIMIT }),
    staleTime: 30_000,
  });
}
