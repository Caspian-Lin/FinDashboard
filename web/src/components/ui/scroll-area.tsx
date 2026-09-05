import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * 竖向滚动区:原生 overflow-y-auto(scrollbar-thin 细滚动条)。
 *
 * 曾用 Radix ScrollArea:其 Viewport 高度约束依赖 max-h 在 Root/Viewport
 * 之间传递,`max-h` 加在 Root 上时 Viewport 的 h-full 解析不出确定高度,
 * 真实客户端出现过内容被裁切且不可滚动(shadcn#594,inherit 修复仍不可靠)。
 * 本仓全部调用点都是「max-h 限高的竖向列表」,原生溢出滚动无高度传递前提,
 * 滚轮/触摸/键盘行为均为浏览器原生。
 */
const ScrollArea = React.forwardRef<
  HTMLDivElement,
  React.ComponentPropsWithoutRef<"div">
>(({ className, children, ...props }, ref) => (
  <div
    ref={ref}
    className={cn("overflow-x-hidden overflow-y-auto scrollbar-thin", className)}
    {...props}
  >
    {children}
  </div>
));
ScrollArea.displayName = "ScrollArea";

export { ScrollArea };
