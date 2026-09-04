import type { ReactNode } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";

/**
 * 主从布局页的左侧二级列表统一容器:Card + 标题/计数/操作一行 + 统一高度滚动区。
 * 此前 5 个页面各自为政(九种 max-h、两种卡片内边距、三种头部模式),本组件收拢。
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
        <ScrollArea className="max-h-[560px] pr-3">
          <div className="space-y-2">{children}</div>
        </ScrollArea>
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
