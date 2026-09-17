import { cn } from "@/lib/utils";
import { useT } from "@/i18n";

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
  const { t } = useT();
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
      {isPending ? t("selectAll.selecting") : t("selectAll.selectAll", { count: totalCount })}
    </button>
  );
}
