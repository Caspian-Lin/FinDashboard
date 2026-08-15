import * as React from "react";
import { cn } from "@/lib/utils";

interface PageContainerProps {
  children: React.ReactNode;
  className?: string;
}

/** 页面内容统一容器:最大宽度、水平居中与垂直节奏(issue #163)。 */
export function PageContainer({ children, className }: PageContainerProps) {
  return <div className={cn("mx-auto w-full max-w-7xl space-y-6", className)}>{children}</div>;
}
