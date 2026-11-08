import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

interface PagerProps {
  total: number;
  limit: number;
  offset: number;
  onChange: (nextOffset: number) => void;
  disabled?: boolean;
  className?: string;
}

/**
 * 列表分页器(issue #512 抽出;数据集列表 / 发布逐标的共用)。
 *
 * 只做偏移量换算,不做数据聚合;页码与总数都来自服务端 `total`。
 */
export function Pager({ total, limit, offset, onChange, disabled, className }: PagerProps) {
  const { tl } = useT();
  if (total <= 0) return null;
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  const from = offset + 1;
  const to = Math.min(offset + limit, total);
  return (
    <div className={cn("flex flex-wrap items-center justify-between gap-3 text-sm", className)}>
      <p className="text-muted-foreground" role="status">
        {tl({ zh: `共 ${total} 条 · 第 ${from}-${to} 条 · 第 ${page}/${pages} 页`, en: `${total} total · ${from}-${to} · page ${page}/${pages}` })}
      </p>
      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() => onChange(Math.max(0, offset - limit))}
          disabled={disabled || offset <= 0}
          aria-label={tl({ zh: "上一页", en: "Previous page" })}
        >
          <ChevronLeft className="h-4 w-4" />
          {tl({ zh: "上一页", en: "Previous" })}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => onChange(offset + limit)}
          disabled={disabled || to >= total}
          aria-label={tl({ zh: "下一页", en: "Next page" })}
        >
          {tl({ zh: "下一页", en: "Next" })}
          <ChevronRight className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
