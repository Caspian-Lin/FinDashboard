import { Badge } from "@/components/ui/badge";
import { useT } from "@/i18n";
import type { LocalizedText } from "@/i18n";
import { cn } from "@/lib/utils";
import type { CatalogStatus } from "@/lib/data-catalog";

interface StatusMeta {
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
export const CATALOG_STATUS_META: Record<CatalogStatus, StatusMeta> = {
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

export function catalogStatusMeta(status: string): StatusMeta {
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

/** 目录状态胶囊:文案与配色都走映射表,不依赖颜色单独传达语义。 */
export function StatusPill({ status, className }: { status: string; className?: string }) {
  const { tl } = useT();
  const meta = catalogStatusMeta(status);
  return (
    <Badge variant={meta.variant} className={cn("font-medium", className)}>
      {tl(meta.label)}
    </Badge>
  );
}

export function statusHint(status: string, lang: "zh" | "en"): string {
  const hint = catalogStatusMeta(status).hint;
  return hint[lang] ?? hint.zh;
}
