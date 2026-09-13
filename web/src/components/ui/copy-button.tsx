import * as React from "react";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useT } from "@/i18n";

/**
 * 可复制文本按钮(issue #373):点击复制全文,短暂显示已复制反馈。
 *
 * navigator.clipboard 只在安全上下文(https/localhost)可用 —— 局域网
 * http 访问时回退到隐藏 textarea + execCommand,复制失败保持原状不报错。
 */
export function CopyButton({
  text,
  className,
  labels,
}: {
  text: string;
  className?: string;
  /** 覆盖默认取词(如狭小空间只显示图标)。 */
  labels?: { copy?: string; copied?: string };
}) {
  const { t } = useT();
  const [copied, setCopied] = React.useState(false);
  const timer = React.useRef<number | null>(null);

  React.useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  const copy = async () => {
    let ok = false;
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch {
      // 非安全上下文回退:隐藏 textarea + execCommand("copy")。
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        ok = document.execCommand("copy");
      } catch {
        ok = false;
      } finally {
        document.body.removeChild(area);
      }
    }
    if (ok) {
      setCopied(true);
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 1500);
    }
  };

  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className={cn("h-7 gap-1 px-2 text-xs text-muted-foreground", className)}
      onClick={() => void copy()}
      aria-label={copied ? (labels?.copied ?? t("common.copied")) : (labels?.copy ?? t("common.copy"))}
    >
      {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      {copied ? (labels?.copied ?? t("common.copied")) : (labels?.copy ?? t("common.copy"))}
    </Button>
  );
}
