import { NavLink } from "react-router-dom";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

export function TaskCenterNav() {
  const { t } = useT();
  return (
    <nav className="flex flex-wrap gap-4 border-b border-border text-sm" aria-label={t("taskCenter.nav")}>
      {[
        ["/jobs", "taskCenter.tasks"],
        ["/research/audit", "taskCenter.audit"],
      ].map(([to, label]) => (
        <NavLink
          end
          key={to}
          to={to}
          className={({ isActive }) =>
            cn("border-b-2 border-transparent pb-2 text-muted-foreground hover:text-foreground",
              isActive && "border-primary font-medium text-foreground")
          }
        >
          {t(label)}
        </NavLink>
      ))}
    </nav>
  );
}

