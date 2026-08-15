import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { Info } from "lucide-react";
import type { InfoHintDefinition } from "../lib/infoHints";

const VIEWPORT_GAP = 12;
const ANCHOR_GAP = 8;

export interface InfoHintProps {
  content: InfoHintDefinition;
  className?: string;
}

export default function InfoHint({ content, className = "" }: InfoHintProps) {
  const rawId = useId();
  const tooltipId = `info-hint-${rawId.replaceAll(":", "")}`;
  const triggerRef = useRef<HTMLButtonElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const closeTimerRef = useRef<number | null>(null);
  const pinnedRef = useRef(false);
  const triggerHoveredRef = useRef(false);
  const tooltipHoveredRef = useRef(false);
  const triggerFocusedRef = useRef(false);
  const [open, setOpen] = useState(false);

  const clearCloseTimer = useCallback(() => {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
  }, []);

  const close = useCallback(() => {
    clearCloseTimer();
    pinnedRef.current = false;
    setOpen(false);
  }, [clearCloseTimer]);

  const scheduleClose = useCallback(() => {
    clearCloseTimer();
    closeTimerRef.current = window.setTimeout(() => {
      if (
        !pinnedRef.current &&
        !triggerHoveredRef.current &&
        !tooltipHoveredRef.current &&
        !triggerFocusedRef.current
      ) {
        setOpen(false);
      }
    }, 80);
  }, [clearCloseTimer]);

  const positionTooltip = useCallback(() => {
    const trigger = triggerRef.current;
    const tooltip = tooltipRef.current;
    if (!trigger || !tooltip) return;

    const triggerRect = trigger.getBoundingClientRect();
    const tooltipRect = tooltip.getBoundingClientRect();
    const availableBelow = window.innerHeight - triggerRect.bottom;
    const placeBelow =
      availableBelow >= tooltipRect.height + ANCHOR_GAP + VIEWPORT_GAP ||
      triggerRect.top < tooltipRect.height + ANCHOR_GAP + VIEWPORT_GAP;
    const idealLeft =
      triggerRect.left + triggerRect.width / 2 - tooltipRect.width / 2;
    const maxLeft = Math.max(
      VIEWPORT_GAP,
      window.innerWidth - tooltipRect.width - VIEWPORT_GAP,
    );
    const left = Math.min(Math.max(VIEWPORT_GAP, idealLeft), maxLeft);
    const top = placeBelow
      ? triggerRect.bottom + ANCHOR_GAP
      : triggerRect.top - tooltipRect.height - ANCHOR_GAP;

    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${Math.max(VIEWPORT_GAP, top)}px`;
    tooltip.style.visibility = "visible";
  }, []);

  useLayoutEffect(() => {
    if (open) positionTooltip();
  }, [open, positionTooltip]);

  useEffect(() => {
    if (!open) return;

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        !triggerRef.current?.contains(target) &&
        !tooltipRef.current?.contains(target)
      ) {
        close();
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
        triggerRef.current?.focus();
      }
    };

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    window.addEventListener("resize", positionTooltip);
    window.addEventListener("scroll", positionTooltip, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("resize", positionTooltip);
      window.removeEventListener("scroll", positionTooltip, true);
    };
  }, [close, open, positionTooltip]);

  useEffect(() => () => clearCloseTimer(), [clearCloseTimer]);

  return (
    <span className={`inline-flex shrink-0 ${className}`}>
      <button
        ref={triggerRef}
        type="button"
        className="inline-flex size-6 items-center justify-center rounded-full text-muted-foreground/70 transition-colors duration-150 hover:bg-primary/10 hover:text-primary focus:outline-none focus-visible:bg-primary/10 focus-visible:text-primary focus-visible:ring-2 focus-visible:ring-primary/30 motion-reduce:transition-none -m-2.5 p-2.5"
        aria-label={`查看“${content.title}”说明`}
        aria-describedby={open ? tooltipId : undefined}
        aria-expanded={open}
        onPointerEnter={() => {
          clearCloseTimer();
          triggerHoveredRef.current = true;
          setOpen(true);
        }}
        onPointerLeave={() => {
          triggerHoveredRef.current = false;
          scheduleClose();
        }}
        onFocus={() => {
          clearCloseTimer();
          triggerFocusedRef.current = true;
          setOpen(true);
        }}
        onBlur={() => {
          triggerFocusedRef.current = false;
          scheduleClose();
        }}
        onClick={() => {
          const nextPinned = !pinnedRef.current;
          pinnedRef.current = nextPinned;
          setOpen(nextPinned);
        }}
      >
        <Info size={14} strokeWidth={2.25} aria-hidden="true" />
      </button>

      {open &&
        createPortal(
          <div
            ref={tooltipRef}
            id={tooltipId}
            role="tooltip"
            className="fixed z-50 max-h-[calc(100vh-1.5rem)] w-[min(20rem,calc(100vw-1.5rem))] overflow-y-auto rounded-lg bg-popover px-3.5 py-3 text-left text-popover-foreground"
            style={{ visibility: "hidden" }}
            onPointerEnter={() => {
              clearCloseTimer();
              tooltipHoveredRef.current = true;
            }}
            onPointerLeave={() => {
              tooltipHoveredRef.current = false;
              scheduleClose();
            }}
          >
            <div className="text-sm font-semibold">{content.title}</div>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              {content.description}
            </p>
            {content.detail && (
              <p className="mt-1.5 border-t border-border pt-1.5 text-xs leading-5 text-muted-foreground">
                {content.detail}
              </p>
            )}
          </div>,
          document.body,
        )}
    </span>
  );
}

export function HintLabel({
  htmlFor,
  children,
  hint,
  className = "mb-1",
  labelClassName = "text-sm text-muted-foreground",
}: {
  htmlFor?: string;
  children: ReactNode;
  hint: InfoHintDefinition;
  className?: string;
  labelClassName?: string;
}) {
  return (
    <div className={`flex items-center gap-1 ${className}`}>
      <label htmlFor={htmlFor} className={labelClassName}>
        {children}
      </label>
      <InfoHint content={hint} />
    </div>
  );
}
