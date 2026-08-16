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

export default function Settings() {
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

  if (!form) return <div className="text-muted-foreground/70">加载中...</div>;

  const marketOptions = [
    { value: "a_share", label: "A股" },
    { value: "hk", label: "港股" },
    { value: "us", label: "美股" },
  ];

  const typeOptions = [
    { value: "stock", label: "股票" },
    { value: "etf", label: "ETF" },
    { value: "index", label: "指数" },
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
        : form.download_types.filter((t) => t !== value),
    });

  return (
    <PageContainer>
      <PageHeader title="设置" description="数据源与定时任务配置(仅数据域,不影响实盘交易内核)。" />

      {/* Data Source */}
      <section className="rounded-lg border border-border bg-card p-5">
        <h2 className="mb-3 text-lg font-semibold">数据源</h2>
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-1 text-sm text-muted-foreground">
            <span>当前数据源</span>
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
            切换: FINBOARD_DATA_PROVIDER=tushare / akshare / yfinance 环境变量
          </span>
        </div>
      </section>

      {/* Scheduled Tasks */}
      <section className="rounded-lg border border-border bg-card p-5">
        <h2 className="mb-4 text-lg font-semibold">定时任务</h2>

        {/* Universe Sync Task */}
        <div className="mb-4 rounded-lg border border-border p-4">
          <div className="mb-3 flex items-center justify-between gap-4">
            <div>
              <h3 className="font-medium">标的池同步</h3>
              <p className="mt-0.5 text-sm text-muted-foreground">
                每日自动从 akshare 发现新上市/退市标的,更新 instruments 表
              </p>
            </div>
            <Switch
              checked={form.sync_enabled}
              onCheckedChange={(checked) =>
                setForm({ ...form, sync_enabled: checked })
              }
              aria-label="标的池同步开关"
            />
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <HintLabel
              htmlFor="settings-sync-time"
              hint={INFO_HINTS.settings.syncTime}
              className=""
            >
              触发时间
            </HintLabel>
            <Input
              id="settings-sync-time"
              type="time"
              value={form.sync_time}
              onChange={(e) => setForm({ ...form, sync_time: e.target.value })}
              className="w-auto"
            />
            <span className="text-sm text-muted-foreground/70">(Asia/Shanghai, 仅交易日)</span>
          </div>
        </div>

        {/* Bulk Download Task */}
        <div className="rounded-lg border border-border p-4">
          <div className="mb-3 flex items-center justify-between gap-4">
            <div>
              <h3 className="font-medium">增量数据拉取</h3>
              <p className="mt-0.5 text-sm text-muted-foreground">
                每日盘后增量拉取所有活跃标的的最新行情数据到 parquet 缓存
              </p>
            </div>
            <Switch
              checked={form.download_enabled}
              onCheckedChange={(checked) =>
                setForm({ ...form, download_enabled: checked })
              }
              aria-label="增量数据拉取开关"
            />
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
            <div>
              <HintLabel
                htmlFor="settings-download-time"
                hint={INFO_HINTS.settings.downloadTime}
              >
                触发时间
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
                回溯天数
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
              <span className="text-sm text-muted-foreground/70">天 (增量拉取)</span>
            </div>
          </div>

          <fieldset className="mt-3">
            <legend className="mb-1 flex items-center gap-1 text-sm text-muted-foreground">
              拉取市场
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
              拉取类型
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
          {save.isPending ? "保存中..." : "保存配置"}
        </Button>
        {save.isSuccess && <span className="text-sm text-success">已保存</span>}
        {save.isError && (
          <span className="text-sm text-destructive">
            {(save.error as Error).message}
          </span>
        )}
      </div>
    </PageContainer>
  );
}
