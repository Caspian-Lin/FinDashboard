import { ArrowUpRight } from "lucide-react";
import { Link } from "react-router-dom";
import { availableResearchTools } from "@/components/layout/nav-config";
import { PageContainer } from "@/components/ui/page-container";
import { PageHeader } from "@/components/ui/page-header";
import { useT } from "@/i18n";
import { RESEARCH_EDITION } from "@/lib/edition";

export default function ResearchTools() {
  const { tl } = useT();

  return (
    <PageContainer className="max-w-4xl">
      <PageHeader title={tl({ zh: "高级研究工具", en: "Advanced research tools" })} />
      <ul className="divide-y divide-border rounded-lg border bg-card">
        {availableResearchTools(RESEARCH_EDITION).map(({ to, label, description, icon: Icon }) => (
          <li key={to}>
            <Link
              to={to}
              className="flex min-h-14 items-center gap-3 px-4 py-2 hover:bg-muted focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
            >
              <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-semibold">{tl(label)}</p>
                <p className="text-xs leading-relaxed text-muted-foreground">{tl(description)}</p>
              </div>
              <ArrowUpRight className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            </Link>
          </li>
        ))}
      </ul>
    </PageContainer>
  );
}
