import { Link, useLocation } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { PageContainer } from "@/components/ui/page-container";
import { PageHeader } from "@/components/ui/page-header";
import { BlockedState } from "@/components/ui/states";
import { useT } from "@/i18n";

export function EditionUnavailable({ capability }: { capability: "live" | "simulation" }) {
  const { tl } = useT();
  const { pathname } = useLocation();
  const title = capability === "live"
    ? tl({ zh: "实盘功能需要完整版", en: "Live trading requires the full edition" })
    : tl({ zh: "模拟盘需要完整版", en: "Simulation requires the full edition" });

  return (
    <PageContainer className="max-w-3xl">
      <PageHeader title={title} />
      <BlockedState
        title={tl({ zh: "当前部署为研究版", en: "This installation is the research edition" })}
        description={tl({
          zh: `可以在完整版中重新打开原地址 ${pathname}，或回到当前实例的研究总览。`,
          en: `Reopen ${pathname} in the full edition, or return to this installation's research overview.`,
        })}
      />
      <Button asChild variant="outline">
        <Link to="/research">{tl({ zh: "打开研究总览", en: "Open research overview" })}</Link>
      </Button>
    </PageContainer>
  );
}
