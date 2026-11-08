import { Badge } from "@/components/ui/badge";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import { catalogStatusMeta } from "@/lib/data-labels";

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
