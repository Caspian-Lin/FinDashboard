import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import InfoHint, { HintLabel } from "../components/InfoHint";
import { api, ApiError, fetchJSON, type SchedulerConfig } from "../lib/api";
import { INFO_HINTS } from "../lib/infoHints";
import { PageHeader } from "../components/ui/page-header";
import { PageContainer } from "../components/ui/page-container";
import { Input } from "../components/ui/input";
import { Switch } from "../components/ui/switch";
import { Button } from "../components/ui/button";
import { useT, useLanguage, type Language } from "@/i18n";
import { useTheme } from "@/components/ui/theme-provider";
import { ErrorState, LoadingState } from "@/components/ui/states";
import { catalogApi } from "@/lib/data-catalog";
import { RESEARCH_EDITION } from "@/lib/edition";

const sectionClass = "space-y-4 rounded-lg border bg-card p-4 sm:p-5";
const selectClass = "h-11 w-full rounded-md border border-input bg-background px-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring";

function Preferences() {
  const { t, tl } = useT();
  const { lang, setLanguage } = useLanguage();
  const { theme, setTheme } = useTheme();
  return (
    <section className={sectionClass} aria-labelledby="preferences-heading">
      <h2 id="preferences-heading" className="text-lg font-semibold">{tl({ zh: "偏好", en: "Preferences" })}</h2>
      <p className="text-sm text-muted-foreground">
        {tl({ zh: "仅用于当前浏览器，立即生效。默认中文、深色。", en: "Applies immediately in this browser. Defaults to Chinese and dark mode." })}
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1 text-sm">
          <span className="block font-medium">{t("settings.languageLabel")}</span>
          <select className={selectClass} value={lang} onChange={(event) => setLanguage(event.target.value as Language)}>
            <option value="zh">简体中文</option>
            <option value="en">English</option>
          </select>
        </label>
        <label className="space-y-1 text-sm">
          <span className="block font-medium">{tl({ zh: "界面主题", en: "Theme" })}</span>
          <select className={selectClass} value={theme} onChange={(event) => setTheme(event.target.value === "light" ? "light" : "dark")}>
            <option value="dark">{tl({ zh: "深色", en: "Dark" })}</option>
            <option value="light">{tl({ zh: "浅色", en: "Light" })}</option>
          </select>
        </label>
      </div>
    </section>
  );
}

function configError(error: Error) {
  const status = error instanceof ApiError ? error.status : 0;
  if (status === 409) return {
    title: { zh: "配置已由其他会话更新", en: "Another session updated this configuration" },
    message: { zh: "重读服务器配置后重新核对要修改的设置。", en: "Reload the server settings and reapply your intended changes." },
  };
  if (status === 401 || status === 403) return {
    title: { zh: "缺少配置权限", en: "Configuration access denied" },
    message: { zh: "请检查本机服务的配置文件权限，或联系部署管理员。", en: "Check the host configuration permissions or contact the deployment administrator." },
  };
  if (!status || status === 503) return {
    title: { zh: "配置服务不可用", en: "Configuration service unavailable" },
    message: { zh: "检查服务是否运行，恢复后重试。", en: "Check that the service is running and try again when it recovers." },
  };
  return {
    title: { zh: "配置读取或保存失败", en: "Could not read or save configuration" },
    message: { zh: "检查表单输入或请管理员检查宿主机配置。", en: "Check the form or ask an administrator to inspect the host configuration." },
  };
}

function DataSchedule() {
  const { t, tl } = useT();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["scheduler-config"],
    queryFn: api.getConfig,
  });
  const [draft, setForm] = useState<SchedulerConfig | null>(null);
  const save = useMutation({
    mutationFn: (body: SchedulerConfig) => api.updateConfig({
      sync_enabled: body.sync_enabled,
      sync_time: body.sync_time,
      download_enabled: body.download_enabled,
      download_time: body.download_time,
      download_lookback_days: body.download_lookback_days,
      download_markets: body.download_markets,
      download_types: body.download_types,
    }, body.revision),
    onSuccess: (config) => {
      queryClient.setQueryData(["scheduler-config"], config);
      setForm(null);
    },
  });
  const form = draft ?? query.data;
  if (query.isError && !form) {
    const error = configError(query.error);
    return <ErrorState title={tl(error.title)} message={tl(error.message)} onRetry={() => void query.refetch()} />;
  }
  if (!form) return <LoadingState rows={2} />;

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

  const error = save.error ? configError(save.error) : null;
  const conflict = save.error instanceof ApiError && save.error.status === 409;
  return (
    <div className="space-y-4">
      <fieldset disabled={save.isPending || conflict}>
        <legend className="mb-2 font-medium">{t("settings.sectionScheduler")}</legend>
        <p className="mb-3 text-sm text-muted-foreground">
          {tl({
            zh: "旧版调度偏好保存到本机配置。当前数据同步在数据基座按需提交。默认交易日 15:35 同步、15:45 下载，回看 5 天。",
            en: "Legacy schedule preferences are saved on this host. Start data sync on demand in Data. Defaults are 15:35 sync and 15:45 download on trading days, with a 5-day lookback.",
          })}
        </p>
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
      </fieldset>
      <div className="flex flex-wrap items-center gap-4">
        <Button onClick={() => save.mutate(form)} disabled={!draft || save.isPending || conflict}>
          {save.isPending ? t("common.saving") : t("settings.saveConfig")}
        </Button>
        {save.isSuccess && !draft && (
          <p role="status" className="text-sm text-success">
            {tl({ zh: "调度偏好已保存到本机", en: "Schedule preferences saved on this host" })}
          </p>
        )}
      </div>
      {error && (
        <div role="alert" className="space-y-2 rounded-md border border-destructive/30 p-3 text-sm">
          <p className="font-medium text-destructive">{tl(error.title)}</p>
          <p className="text-muted-foreground">{tl(error.message)}</p>
          {conflict && (
            <Button variant="outline" disabled={query.isFetching} onClick={() => {
              void query.refetch().then((result) => {
                if (result.isSuccess) {
                  setForm(null);
                  save.reset();
                }
              });
            }}>
              {tl({ zh: "重读服务器配置", en: "Reload server configuration" })}
            </Button>
          )}
          {conflict && query.isError && <p className="text-destructive">{tl(configError(query.error).title)}</p>}
        </div>
      )}
    </div>
  );
}

export default function Settings({ research = RESEARCH_EDITION }: { research?: boolean }) {
  const { t, tl } = useT();
  const source = useQuery({ queryKey: ["data-source-status"], queryFn: api.getDataSourceStatus });
  const health = useQuery({
    queryKey: ["research-edition-health"],
    queryFn: () => fetchJSON<{ status: string }>("/research-edition/health"),
    enabled: research,
  });
  const quota = useQuery({
    queryKey: ["tushare-quota", "settings"],
    queryFn: api.getTushareQuota,
    enabled: source.data?.tushare_configured === true,
  });
  const releases = useQuery({
    queryKey: ["first-research-release"],
    queryFn: () => catalogApi.list({ dataset: "bars", limit: 1 }),
    enabled: research,
  });
  const hasResearchBars = releases.data?.items.some((item) =>
    item.release && (item.status === "usable" || item.status === "stale"),
  );
  const statusError = source.error ?? health.error;
  const permittedError = statusError instanceof ApiError && (statusError.status === 401 || statusError.status === 403);
  return (
    <PageContainer className="max-w-5xl">
      <PageHeader title={t("settings.title")} />
      <Preferences />
      <section className={sectionClass} aria-labelledby="data-settings-heading">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="data-settings-heading" className="text-lg font-semibold">{tl({ zh: "数据同步", en: "Data Sync" })}</h2>
          <Button asChild variant="outline">
            <Link to="/data?tab=sync">{tl({ zh: "打开数据基座", en: "Open Data Sync" })}</Link>
          </Button>
        </div>
        <dl className="grid gap-3 text-sm sm:grid-cols-2">
          <div>
            <dt className="flex items-center gap-1 text-muted-foreground">
              {t("settings.sectionDataSource")}
              <InfoHint content={INFO_HINTS.settings.dataProvider} />
            </dt>
            <dd className="font-medium">{source.data?.provider ?? "—"}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Tushare</dt>
            <dd className={source.data?.tushare_configured ? "font-medium text-success" : "font-medium text-warning"}>
              {source.data
                ? source.data.tushare_configured
                  ? tl({ zh: "已配置", en: "Configured" })
                  : tl({ zh: "未配置", en: "Not configured" })
                : "—"}
            </dd>
          </div>
        </dl>
        {research ? (
          <details className="rounded-md border p-3">
            <summary className="min-h-8 cursor-pointer text-sm font-medium">{tl({ zh: "查看旧版调度偏好", en: "Legacy schedule preferences" })}</summary>
            <DataSchedule />
          </details>
        ) : <DataSchedule />}
      </section>
      <section className={sectionClass} aria-labelledby="agent-settings-heading">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="agent-settings-heading" className="text-lg font-semibold">{tl({ zh: "Agent 接入", en: "Agent Connection" })}</h2>
          <Button asChild variant="outline">
            <Link to="/research/agent-connect">{tl({ zh: "查看连接与权限", en: "View Connection and Permissions" })}</Link>
          </Button>
        </div>
        <p className="text-sm text-muted-foreground">
          {tl({ zh: "按客户端分配权限和凭据。凭据变更后在对应客户端重连。", en: "Access and credentials are scoped to each client. Reconnect that client after a credential change." })}
        </p>
      </section>
      <section className={sectionClass} aria-labelledby="diagnostics-heading">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="diagnostics-heading" className="text-lg font-semibold">{tl({ zh: "系统诊断", en: "System Diagnostics" })}</h2>
          <Button variant="outline" disabled={source.isFetching || health.isFetching} onClick={() => {
            void source.refetch();
            if (research) {
              void health.refetch();
              void releases.refetch();
            }
            if (source.data?.tushare_configured) void quota.refetch();
          }}>
            {tl({ zh: "刷新诊断", en: "Refresh Diagnostics" })}
          </Button>
        </div>
        {statusError ? (
          <div role="alert"><ErrorState
            title={permittedError
              ? tl({ zh: "诊断权限不足", en: "Diagnostics access denied" })
              : tl({ zh: "服务不可用", en: "Service unavailable" })}
            message={permittedError
              ? tl({ zh: "联系部署管理员核对本机访问权限。", en: "Ask the deployment administrator to check host access." })
              : tl({ zh: "在桌面控制台查看服务状态。在线 ZIP 可运行 .\\finboard.cmd status，再刷新诊断。", en: "Check service status in the desktop console. For the online ZIP, run .\\finboard.cmd status, then refresh." })}
          /></div>
        ) : source.data && (!research || health.data?.status === "ready") ? (
          <p role="status" className="text-sm text-success">
            {research
              ? tl({ zh: "研究 REST 和数据库可用", en: "Research REST and database ready" })
              : tl({ zh: "数据服务可用", en: "Data service ready" })}
          </p>
        ) : <LoadingState rows={1} />}
        {source.data?.sandbox_enabled != null && (
          <p className="text-sm text-muted-foreground">
            {source.data.sandbox_enabled
              ? tl({ zh: "研究代码沙箱已启用", en: "Research code sandbox enabled" })
              : tl({ zh: "研究代码沙箱未启用，无代码研究可继续。", en: "The code sandbox is disabled. No-code research is available." })}
          </p>
        )}
        {quota.data && (
          <p className="text-sm text-muted-foreground">
            {tl({ zh: "本机 Tushare 预算", en: "Local Tushare budget" })} {quota.data.remaining} / {quota.data.daily_limit}
          </p>
        )}
        {quota.isError && <p className="text-sm text-warning">{tl({ zh: "当前无法读取本机额度记录。", en: "Local request usage is unavailable." })}</p>}
        <p className="text-sm text-muted-foreground">
          {research
            ? tl({
              zh: "修改宿主凭据后需重启研究服务。在桌面控制台配置 Tushare；在线 ZIP 运行 .\\finboard.cmd source-configure，然后运行 .\\finboard.cmd source-status 核对认证、接口权限和配额。",
              en: "Restart the research service after host credentials change. Configure Tushare in the desktop console, or run .\\finboard.cmd source-configure from the online ZIP, then .\\finboard.cmd source-status to verify authentication, permissions and quotas.",
            })
            : tl({
              zh: "在宿主机环境中修改数据源或凭据，重启服务后再刷新诊断。",
              en: "Change providers or credentials in the host environment, then restart the service and refresh diagnostics.",
            })}
        </p>
        {research && releases.data && (
          <div className="space-y-2 border-t pt-3">
            <h3 className="font-medium">{tl({ zh: "开始首次研究", en: "Start Your First Research" })}</h3>
            {hasResearchBars ? (
              <div className="flex flex-wrap gap-2">
                <Button asChild><Link to="/research/topics">{tl({ zh: "打开研究课题", en: "Open Research Topics" })}</Link></Button>
                <Button asChild variant="outline"><Link to="/research/strategy">{tl({ zh: "打开策略 Studio", en: "Open Strategy Studio" })}</Link></Button>
              </div>
            ) : (
              <>
                <p className="text-sm text-muted-foreground">
                  {tl({
                    zh: "当前没有可用发布。从在线 ZIP 安装目录导入合成入门包，再打开数据基座确认版本。",
                    en: "There are no usable releases yet. Import the synthetic starter bundle from the online ZIP directory, then verify the release in Data.",
                  })}
                </p>
                <code className="block break-all rounded bg-muted p-2 text-xs">{".\\finboard.cmd import --bundle .\\synthetic-starter-v1.zip"}</code>
                <Button asChild variant="outline"><Link to="/data?tab=datasets">{tl({ zh: "查看数据集", en: "View Datasets" })}</Link></Button>
              </>
            )}
          </div>
        )}
        {research && releases.isError && (
          <p className="text-sm text-warning">{tl({ zh: "数据目录不可用，请恢复服务后重新检查发布。", en: "The data catalog is unavailable. Recheck releases once the service recovers." })}</p>
        )}
      </section>
    </PageContainer>
  );
}
