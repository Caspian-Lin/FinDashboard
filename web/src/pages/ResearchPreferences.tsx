import { Link } from "react-router-dom";
import { PageContainer } from "@/components/ui/page-container";
import { PageHeader } from "@/components/ui/page-header";
import { Button } from "@/components/ui/button";
import { useTheme } from "@/components/ui/theme-provider";
import { useLanguage, useT, type Language } from "@/i18n";

export default function ResearchPreferences() {
  const { lang, setLanguage } = useLanguage();
  const { theme, setTheme } = useTheme();
  const { tl } = useT();

  return (
    <PageContainer className="max-w-3xl">
      <PageHeader title={tl({ zh: "设置", en: "Settings" })} />
      <section className="space-y-4 border-b pb-5" aria-labelledby="preferences-heading">
        <h2 id="preferences-heading" className="font-semibold">{tl({ zh: "偏好", en: "Preferences" })}</h2>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-2 text-sm">
            <span className="block">{tl({ zh: "界面语言", en: "Display language" })}</span>
            <select
              value={lang}
              onChange={(event) => setLanguage(event.target.value as Language)}
              className="h-11 w-full rounded-md border border-input bg-background px-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
            >
              <option value="zh">简体中文</option>
              <option value="en">English</option>
            </select>
          </label>
          <label className="space-y-2 text-sm">
            <span className="block">{tl({ zh: "界面主题", en: "Theme" })}</span>
            <select
              value={theme}
              onChange={(event) => setTheme(event.target.value === "light" ? "light" : "dark")}
              className="h-11 w-full rounded-md border border-input bg-background px-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
            >
              <option value="dark">{tl({ zh: "深色", en: "Dark" })}</option>
              <option value="light">{tl({ zh: "浅色", en: "Light" })}</option>
            </select>
          </label>
        </div>
      </section>
      <div className="flex flex-wrap gap-3">
        <Button asChild variant="outline"><Link to="/data?tab=sync">{tl({ zh: "查看数据同步", en: "View data sync" })}</Link></Button>
        <Button asChild variant="outline"><Link to="/research/agent-connect">{tl({ zh: "查看 Agent 接入", en: "View agent connection" })}</Link></Button>
      </div>
    </PageContainer>
  );
}
