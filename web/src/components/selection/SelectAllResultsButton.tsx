import { cn } from "@/lib/utils";

interface SelectAllResultsButtonProps {
  totalCount: number;
  isPending: boolean;
  onSelectAll: () => void;
  className?: string;
}

export function SelectAllResultsButton({
  totalCount,
  isPending,
  onSelectAll,
  className,
}: SelectAllResultsButtonProps) {
  return (
    <button
      type="button"
      onClick={onSelectAll}
      disabled={isPending || totalCount === 0}
      className={cn(
        "text-xs text-primary hover:underline disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
    >
      {isPending ? "正在选择全部结果…" : `全选筛选结果（${totalCount}）`}
    </button>
  );
}
