/**
 * 数据基座路由与深链映射(issue #512)。
 *
 * 纯函数:不依赖 React,便于用单测锁定「旧 /research/data 深链参数保全」与
 * 「旧页签 → 新 tab/dataset」映射。页面只负责把结果交给 navigate()。
 */

export const DATA_TABS = ["overview", "datasets", "sync", "tech"] as const;
export type DataTab = (typeof DATA_TABS)[number];

export const DEFAULT_DATA_TAB: DataTab = "overview";

export function parseDataTab(raw: string | null | undefined): DataTab {
  if (raw && (DATA_TABS as readonly string[]).includes(raw)) return raw as DataTab;
  return DEFAULT_DATA_TAB;
}

/**
 * 旧 `/research/data?tab=` 页签 → 目录数据集 key(计划 6.1 的已验证映射)。
 * `fetch` / `releases` 是旧页面把「行情拉取」「数据发布」并进「行情」后的别名。
 */
export const LEGACY_TAB_DATASET: Record<string, string> = {
  bars: "bars",
  fetch: "bars",
  releases: "bars",
  metrics: "daily_metrics",
  financial: "financial_indicators",
  income: "income_statements",
  balance: "balance_sheets",
  cashflow: "cashflow_statements",
  dividends: "dividends",
  convertible: "convertible_metrics",
  instruments: "profiles",
};

/** 发布 kind → 目录数据集 key(`a_share_tushare` / `multi_asset_mixed` 都是 bars)。 */
export const RELEASE_KIND_DATASET: Record<string, string> = {
  bars: "bars",
  daily_metrics: "daily_metrics",
  financial_indicators: "financial_indicators",
  convertible_metrics: "convertible_metrics",
  income_statements: "income_statements",
  balance_sheets: "balance_sheets",
  cashflow_statements: "cashflow_statements",
  dividends: "dividends",
  multi_asset_mixed: "bars",
  a_share_tushare: "bars",
};

/** 旧页签里仍需要旧组件操作面板的(标的与档案),其余落目录视图。 */
const LEGACY_OPS_TAB: Record<string, string> = {
  instruments: "instruments",
};

export interface DataHrefParts {
  tab?: DataTab;
  dataset?: string | null;
  release?: string | null;
  view?: "catalog" | "ops" | string | null;
  sub?: string | null;
  topic?: string | null;
  status?: string | null;
  source?: string | null;
  asset_class?: string | null;
  start?: string | null;
  end?: string | null;
  q?: string | null;
  offset?: number | null;
}

/** 构造 `/data?...` 链接(空值省略,默认 tab 省略以保持短链)。 */
export function dataHref(parts: DataHrefParts): string {
  const params = new URLSearchParams();
  if (parts.tab && parts.tab !== DEFAULT_DATA_TAB) params.set("tab", parts.tab);
  const ordering: (keyof DataHrefParts)[] = [
    "dataset",
    "release",
    "view",
    "sub",
    "topic",
    "status",
    "source",
    "asset_class",
    "start",
    "end",
    "q",
    "offset",
  ];
  for (const key of ordering) {
    const value = parts[key];
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `/data?${qs}` : "/data";
}

/**
 * 旧 `/research/data` 深链 → `/data` 参数保全重定向目标。
 *
 * - `release=<id>` 一律保留(发布详情抽屉直接打开);
 * - `tab` 按 :data:`LEGACY_TAB_DATASET` 映射到新 `dataset`,并统一落「数据集」tab;
 * - `instruments` 额外带 `view=ops&sub=instruments`(标的生命周期在旧操作面板);
 * - 缺 `tab` 但带 `release` → 「数据集」tab;
 * - 未知 `tab` 值丢弃(不 404、不停留在未知页签),其余查询参数原样保留。
 */
export function legacyResearchDataTarget(search: string): string {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const legacyTab = params.get("tab");
  params.delete("tab");
  const release = params.get("release");
  const dataset = params.get("dataset");
  const mappedDataset =
    (legacyTab ? LEGACY_TAB_DATASET[legacyTab] : undefined) ?? dataset;
  const opsTab = legacyTab ? LEGACY_OPS_TAB[legacyTab] : undefined;
  const hasTab = Boolean(legacyTab && LEGACY_TAB_DATASET[legacyTab]);
  // 旧页签没有对应数据集、也没有 release 深链时,落「数据集」列表而不是空筛选。
  const tab = hasTab || release || mappedDataset ? "datasets" : null;

  // 稳定输出顺序,便于深链可读与测试断言。
  const ordered = new URLSearchParams();
  if (tab) ordered.set("tab", tab);
  if (mappedDataset) ordered.set("dataset", mappedDataset);
  if (release) ordered.set("release", release);
  if (opsTab) {
    ordered.set("view", "ops");
    ordered.set("sub", opsTab);
  }
  for (const [key, value] of params.entries()) {
    if (ordered.has(key)) continue;
    ordered.append(key, value);
  }
  const qs = ordered.toString();
  return qs ? `/data?${qs}` : "/data";
}
