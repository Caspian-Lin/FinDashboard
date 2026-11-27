import { NavLink } from "react-router-dom";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

export function TaskCenterNav() {
  const { t } = useT();
  return (
    <nav className="flex flex-wrap gap-2 text-sm" aria-label={t("taskCenter.nav")}>
      {[
        ["/jobs", "taskCenter.tasks"],
        ["/research/audit", "taskCenter.audit"],
      ].map(([to, label]) => (
        <NavLink
          end
          key={to}
          to={to}
          className={({ isActive }) =>
            cn("inline-flex min-h-11 items-center rounded-sm px-3 text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:min-h-9",
              isActive && "bg-accent font-medium text-foreground")
          }
        >
          {t(label)}
        </NavLink>
      ))}
    </nav>
  );
}

