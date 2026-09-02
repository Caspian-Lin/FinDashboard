import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import InfoHint, { HintLabel } from "../components/InfoHint";
import { api, type SchedulerConfig } from "../lib/api";
import { INFO_HINTS } from "../lib/infoHints";
import { PageHeader } from "../components/ui/page-header";
import { PageContainer } from "../components/ui/page-container";
import { Input } from "../components/ui/input";
import { Switch } from "../components/ui/switch";
import { Button } from "../components/ui/button";
import { useT, useLanguage, type Language } from "@/i18n";

export default function Settings() {
  const { t } = useT();
  const { lang, setLanguage } = useLanguage();
  const queryClient = useQueryClient();
  const { data: config } = useQuery({
    queryKey: ["scheduler-config"],
    queryFn: api.getConfig,
  });

  const [form, setForm] = useState<SchedulerConfig | null>(null);

  useEffect(() => {
    if (config) setForm(config);
  }, [config]);

  const save = useMutation({
    mutationFn: (body: Partial<SchedulerConfig>) => api.updateConfig(body),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["scheduler-config"] }),
  });

  if (!form) return <div className="text-muted-foreground/70">{t("common.loading")}</div>;

  const marketOptions = [
    { value: "a_share", label: t("settings.marketAShare") },
    { value: "hk", label: t("settings.marketHk") },
    { value: "us", label: t("settings.marketUs") },
  ];

  const typeOptions = [
    { value: "stock", label: t("settings.typeStock") },
    { value: "etf", label: t("settings.typeEtf") },
    { value: "index", label: t("settings.typeIndex") },
  ];

  const languageOptions: { value: Language; label: string }[] = [
    { value: "zh", label: t("settings.languageZh") },
    { value: "en", label: t("settings.languageEn") },
  ];

  const toggleMarket = (value: string, checked: boolean) =>
    setForm({
      ...form,
      download_markets: checked
        ? [...form.download_markets, value]
        : form.download_markets.filter((m) => m !== value),
    });

  const toggleType = (value: string, checked: boolean) =>
    setForm({
      ...form,
      download_types: checked
        ? [...form.download_types, value]
        : form.download_types.filter((item) => item !== value),
    });

  return (
    <PageContainer>
      <PageHeader title={t("settings.title")} description={t("settings.description")} />

      {/* Interface Language */}
      <section className="rounded-lg border border-border bg-card p-5">
        <h2 className="mb-3 text-lg font-semibold">{t("settings.sectionLanguage")}</h2>
        <div className="flex flex-wrap items-center gap-4">
          <div>
            <label htmlFor="settings-language" className="text-sm font-medium text-foreground">
              {t("settings.languageLabel")}
            </label>
            <p className="mt-0.5 text-xs text-muted-foreground">{t("settings.languageHint")}</p>
          </div>
          <select
            id="settings-language"
            value={lang}
            onChange={(e) => setLanguage(e.target.value as Language)}
            className="h-9 w-44 rounded-md border border-input bg-transparent px-3 py-1.5 text-sm"
          >
            {languageOptions.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
      </section>

      {/* Data Source */}
      <section className="rounded-lg border border-border bg-card p-5">
        <h2 className="mb-3 text-lg font-semibold">{t("settings.sectionDataSource")}</h2>
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-1 text-sm text-muted-foreground">
            <span>{t("settings.currentProvider")}</span>
            <InfoHint content={INFO_HINTS.settings.dataProvider} />
          </div>
          <span
            className={`rounded-md px-3 py-1 text-sm font-medium ${
              form.data_provider === "yfinance"
                ? "bg-primary/10 text-primary"
                : "bg-warning/10 text-warning"
            }`}
          >
            {form.data_provider}
          </span>
          <span className="text-sm text-muted-foreground/70">
            {t("settings.providerSwitchHint")}
          </span>
        </div>
      </section>

      {/* Scheduled Tasks */}
      <section className="rounded-lg border border-border bg-card p-5">
        <h2 className="mb-4 text-lg font-semibold">{t("settings.sectionScheduler")}</h2>

        {/* Universe Sync Task */}
        <div className="mb-4 rounded-lg border border-border p-4">
          <div className="mb-3 flex items-center justify-between gap-4">
            <div>
              <h3 className="font-medium">{t("settings.universeSync")}</h3>
              <p className="mt-0.5 text-sm text-muted-foreground">
                {t("settings.universeSyncDesc")}
              </p>
            </div>
            <Switch
              checked={form.sync_enabled}
              onCheckedChange={(checked) =>
                setForm({ ...form, sync_enabled: checked })
              }
              aria-label={t("settings.universeSyncSwitch")}
            />
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <HintLabel
              htmlFor="settings-sync-time"
              hint={INFO_HINTS.settings.syncTime}
              className=""
            >
              {t("settings.triggerTime")}
            </HintLabel>
            <Input
              id="settings-sync-time"
              type="time"
              value={form.sync_time}
              onChange={(e) => setForm({ ...form, sync_time: e.target.value })}
              className="w-auto"
            />
            <span className="text-sm text-muted-foreground/70">{t("settings.tradingDaysOnly")}</span>
          </div>
        </div>

        {/* Bulk Download Task */}
        <div className="rounded-lg border border-border p-4">
          <div className="mb-3 flex items-center justify-between gap-4">
            <div>
              <h3 className="font-medium">{t("settings.bulkDownload")}</h3>
              <p className="mt-0.5 text-sm text-muted-foreground">
                {t("settings.bulkDownloadDesc")}
              </p>
            </div>
            <Switch
              checked={form.download_enabled}
              onCheckedChange={(checked) =>
                setForm({ ...form, download_enabled: checked })
              }
              aria-label={t("settings.bulkDownloadSwitch")}
            />
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
            <div>
              <HintLabel
                htmlFor="settings-download-time"
                hint={INFO_HINTS.settings.downloadTime}
              >
                {t("settings.triggerTime")}
              </HintLabel>
              <Input
                id="settings-download-time"
                type="time"
                value={form.download_time}
                onChange={(e) =>
                  setForm({ ...form, download_time: e.target.value })
                }
              />
            </div>
            <div>
              <HintLabel
                htmlFor="settings-lookback-days"
                hint={INFO_HINTS.settings.lookbackDays}
              >
                {t("settings.lookbackDays")}
              </HintLabel>
              <Input
                id="settings-lookback-days"
                type="number"
                value={form.download_lookback_days}
                onChange={(e) =>
                  setForm({
                    ...form,
                    download_lookback_days: Number(e.target.value),
                  })
                }
              />
            </div>
            <div className="flex items-end">
              <span className="text-sm text-muted-foreground/70">{t("settings.lookbackDaysUnit")}</span>
            </div>
          </div>

          <fieldset className="mt-3">
            <legend className="mb-1 flex items-center gap-1 text-sm text-muted-foreground">
              {t("settings.downloadMarkets")}
              <InfoHint content={INFO_HINTS.settings.downloadMarkets} />
            </legend>
            <div className="flex flex-wrap gap-4">
              {marketOptions.map((opt) => (
                <label key={opt.value} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={form.download_markets.includes(opt.value)}
                    onChange={(e) => toggleMarket(opt.value, e.target.checked)}
                  />
                  {opt.label}
                </label>
              ))}
            </div>
          </fieldset>

          <fieldset className="mt-3">
            <legend className="mb-1 flex items-center gap-1 text-sm text-muted-foreground">
              {t("settings.downloadTypes")}
              <InfoHint content={INFO_HINTS.settings.downloadTypes} />
            </legend>
            <div className="flex flex-wrap gap-4">
              {typeOptions.map((opt) => (
                <label key={opt.value} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={form.download_types.includes(opt.value)}
                    onChange={(e) => toggleType(opt.value, e.target.checked)}
                  />
                  {opt.label}
                </label>
              ))}
            </div>
          </fieldset>
        </div>
      </section>

      {/* Save button */}
      <div className="flex items-center gap-4">
        <Button onClick={() => save.mutate(form)} disabled={save.isPending}>
          {save.isPending ? t("common.saving") : t("settings.saveConfig")}
        </Button>
        {save.isSuccess && <span className="text-sm text-success">{t("common.saved")}</span>}
        {save.isError && (
          <span className="text-sm text-destructive">
            {(save.error as Error).message}
          </span>
        )}
      </div>
    </PageContainer>
  );
}
