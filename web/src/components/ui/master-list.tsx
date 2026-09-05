import type { ReactNode } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/**
 * 主从布局页的左侧二级列表统一容器:Card + 标题/计数/操作一行 + 统一高度滚动区。
 * 此前 5 个页面各自为政(九种 max-h、两种卡片内边距、三种头部模式),本组件收拢。
 *
 * 滚动用原生 overflow-y-auto 而非 Radix ScrollArea:后者的 Viewport 高度
 * 约束依赖 max-h 在 Root/Viewport 间的传递,真实客户端出现过约束失效
 * (内容被裁切且不可滚);原生溢出滚动无此类高度传递前提。
 */
export function MasterList({
  title,
  count,
  actions,
  toolbar,
  children,
  className,
}: {
  title: ReactNode;
  count?: number;
  actions?: ReactNode;
  /** 头部下方的筛选/工具行(可选)。 */
  toolbar?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Card className={className}>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-1.5 text-base">
            {title}
            {typeof count === "number" && (
              <span className="text-xs font-normal text-muted-foreground">{count}</span>
            )}
          </CardTitle>
          {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
        </div>
        {toolbar && <div className="mt-2">{toolbar}</div>}
      </CardHeader>
      <CardContent className="p-2 pt-0">
        <div className="max-h-[560px] space-y-2 overflow-x-hidden overflow-y-auto scrollbar-thin p-1">
          {children}
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * 统一列表项:选中态描边高亮。子内容默认套 min-w-0,
 * 防止长 mono ID 以 min-content 宽度撑破卡片被按字符裁切。
 */
export function MasterListItem({
  selected,
  onClick,
  children,
  className,
}: {
  selected?: boolean;
  onClick?: () => void;
  children: ReactNode;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      className={cn(
        "block w-full min-w-0 rounded-md border border-border p-3 text-left transition-colors hover:bg-accent",
        selected && "border-primary bg-accent ring-1 ring-primary/40",
        className,
      )}
    >
      {children}
    </button>
  );
}
