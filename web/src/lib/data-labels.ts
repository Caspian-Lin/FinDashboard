/**
 * 数据域展示文案与状态映射(issue #512)。
 *
 * 纯数据/纯函数模块:不进组件文件,避免 react-refresh「only-export-components」
 * 警告,也便于单测与复用。
 */
import type { LocalizedText } from "@/i18n";
import type { CatalogStatus } from "@/lib/data-catalog";

export interface CatalogStatusMeta {
  label: LocalizedText;
  variant: "default" | "secondary" | "destructive" | "success" | "warning" | "info" | "outline";
  hint: LocalizedText;
}

/**
 * 目录状态 → 展示口径(issue #512)。
 *
 * 后端 `status` 是唯一事实源(#509),前端只做文案/配色映射:
 * `usable`=可研究、`ingested_not_published`=待发布(已同步)、
 * `blocked`=受阻、`not_ingested`=未同步、`stale`=过期、`unknown`=未知。
 */
export const CATALOG_STATUS_META: Record<CatalogStatus, CatalogStatusMeta> = {
  usable: {
    label: { zh: "可研究", en: "Usable" },
    variant: "success",
    hint: { zh: "有可用发布且最近 30 天内有更新", en: "Usable release updated within 30 days" },
  },
  ingested_not_published: {
    label: { zh: "待发布", en: "Not published" },
    variant: "warning",
    hint: {
      zh: "已同步/已缓存,但还没有可用发布",
      en: "Synced or cached, but no usable release yet",
    },
  },
  blocked: {
    label: { zh: "受阻", en: "Blocked" },
    variant: "destructive",
    hint: {
      zh: "最近一次同步失败或质量拒绝,晚于最近发布",
      en: "Latest sync failed or was quality-rejected after the last release",
    },
  },
  not_ingested: {
    label: { zh: "未同步", en: "Not synced" },
    variant: "secondary",
    hint: { zh: "无批次、无发布、无缓存", en: "No batch, release, or cache" },
  },
  stale: {
    label: { zh: "过期", en: "Stale" },
    variant: "warning",
    hint: {
      zh: "有可用发布,但超过 30 天未更新",
      en: "Has a usable release, but nothing updated for over 30 days",
    },
  },
  unknown: {
    label: { zh: "未知", en: "Unknown" },
    variant: "outline",
    hint: { zh: "元数据缺失,不代表没有数据", en: "Missing metadata; not the same as no data" },
  },
};

export function catalogStatusMeta(status: string): CatalogStatusMeta {
  return (
    CATALOG_STATUS_META[status as CatalogStatus] ?? {
      label: { zh: status, en: status },
      variant: "outline",
      hint: { zh: status, en: status },
    }
  );
}

export function catalogStatusLabel(status: string, lang: "zh" | "en"): string {
  const meta = catalogStatusMeta(status);
  return meta.label[lang] ?? meta.label.zh;
}

export function statusHint(status: string, lang: "zh" | "en"): string {
  const hint = catalogStatusMeta(status).hint;
  return hint[lang] ?? hint.zh;
}

/** 课题缺口原因码 → 文案(#512 稳定码;服务端只回码,不在 API 里拼文案)。 */
export const TOPIC_GAP_LABELS: Record<string, LocalizedText> = {
  release_not_found: { zh: "引用的发布不存在", en: "Referenced release missing" },
  run_not_found: { zh: "引用的研究运行不存在", en: "Referenced run missing" },
  dataset_not_published: { zh: "数据集尚无可用发布", en: "Dataset has no usable release" },
  dataset_kind_unmapped: { zh: "发布类型未注册到目录", en: "Release kind not in catalog" },
  quality_blocked: { zh: "发布质量受阻", en: "Release quality blocked" },
  coverage_gap: { zh: "覆盖缺口", en: "Coverage gap" },
};

/** 课题数据视图的下一步动作码 → 文案。 */
export const TOPIC_NEXT_STEP_LABELS: Record<string, LocalizedText> = {
  ready: { zh: "数据可研究", en: "Data is usable" },
  fix_gaps: { zh: "有覆盖缺口,建议补数", en: "Coverage gaps; backfill suggested" },
  blocked: { zh: "引用或质量受阻,先修复", en: "Blocked references or quality; fix first" },
  no_refs: { zh: "课题尚未引用数据发布", en: "No data releases referenced" },
};

/** 课题数据范围来源(显式 scope / 轮次引用推导)。 */
export const TOPIC_RESOLVED_BY_LABELS: Record<string, LocalizedText> = {
  refs: { zh: "按轮次引用推导", en: "Derived from round refs" },
  data_scope: { zh: "按课题显式数据范围", en: "Explicit topic data scope" },
};
