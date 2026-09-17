import { Link } from "react-router-dom";
import { Compass, ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";

export default function NotFound() {
  const { t } = useT();
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center p-8 text-center">
      <Compass className="mb-4 h-16 w-16 text-muted-foreground/50" />
      <p className="text-4xl font-bold text-foreground">404</p>
      <h1 className="mt-2 text-sm font-semibold text-muted-foreground">{t("notFound.description")}</h1>
      <Button asChild variant="outline" size="sm" className="mt-6">
        <Link to="/research">
          <ArrowLeft className="mr-2 h-4 w-4" />
          {t("notFound.backResearch")}
        </Link>
      </Button>
    </div>
  );
}
