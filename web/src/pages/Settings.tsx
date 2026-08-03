import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import InfoHint, { HintLabel } from "../components/InfoHint";
import { api, type LLMConfig, type LLMConfigUpdate, type SchedulerConfig } from "../lib/api";
import { INFO_HINTS } from "../lib/infoHints";

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

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">设置</h1>

      {/* Data Source */}
      <div className="bg-card rounded-lg shadow p-5 mb-6">
        <h2 className="text-lg font-semibold mb-3">数据源</h2>
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-1 text-sm text-muted-foreground">
            <span>当前数据源</span>
            <InfoHint content={INFO_HINTS.settings.dataProvider} />
          </div>
          <span
            className={`px-3 py-1 rounded text-sm font-medium ${
              form.data_provider === "yfinance"
                ? "bg-primary/10 text-primary"
                : "bg-orange-100 text-orange-700"
            }`}
          >
            {form.data_provider}
          </span>
          <span className="text-sm text-muted-foreground/70">
            切换: FINBOARD_DATA_PROVIDER=tushare / akshare / yfinance 环境变量
          </span>
        </div>
      </div>

      {/* LLM Provider */}
      <LLMProviderSection />

      {/* Scheduled Tasks */}
      <div className="bg-card rounded-lg shadow p-5 mb-6">
        <h2 className="text-lg font-semibold mb-4">定时任务</h2>

        {/* Universe Sync Task */}
        <div className="border rounded-lg p-4 mb-4">
          <div className="flex items-center justify-between mb-3">
            <div>
              <h3 className="font-medium">标的池同步</h3>
              <p className="text-sm text-muted-foreground mt-0.5">
                每日自动从 akshare 发现新上市/退市标的,更新 instruments 表
              </p>
            </div>
            <label className="relative inline-flex items-center cursor-pointer">
              <input
                type="checkbox"
                className="sr-only peer"
                checked={form.sync_enabled}
                onChange={(e) =>
                  setForm({ ...form, sync_enabled: e.target.checked })
                }
              />
              <div className="w-11 h-6 bg-muted peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-card after:border-border after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-primary" />
            </label>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <HintLabel
              htmlFor="settings-sync-time"
              hint={INFO_HINTS.settings.syncTime}
              className=""
            >
              触发时间
            </HintLabel>
            <input
              id="settings-sync-time"
              type="time"
              value={form.sync_time}
              onChange={(e) => setForm({ ...form, sync_time: e.target.value })}
              className="border rounded px-3 py-1.5 text-sm"
            />
            <span className="text-sm text-muted-foreground/70">(Asia/Shanghai, 仅交易日)</span>
          </div>
        </div>

        {/* Bulk Download Task */}
        <div className="border rounded-lg p-4">
          <div className="flex items-center justify-between mb-3">
            <div>
              <h3 className="font-medium">增量数据拉取</h3>
              <p className="text-sm text-muted-foreground mt-0.5">
                每日盘后增量拉取所有活跃标的的最新行情数据到 parquet 缓存
              </p>
            </div>
            <label className="relative inline-flex items-center cursor-pointer">
              <input
                type="checkbox"
                className="sr-only peer"
                checked={form.download_enabled}
                onChange={(e) =>
                  setForm({ ...form, download_enabled: e.target.checked })
                }
              />
              <div className="w-11 h-6 bg-muted peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-card after:border-border after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-primary" />
            </label>
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
            <div>
              <HintLabel
                htmlFor="settings-download-time"
                hint={INFO_HINTS.settings.downloadTime}
              >
                触发时间
              </HintLabel>
              <input
                id="settings-download-time"
                type="time"
                value={form.download_time}
                onChange={(e) =>
                  setForm({ ...form, download_time: e.target.value })
                }
                className="w-full border rounded px-3 py-1.5 text-sm"
              />
            </div>
            <div>
              <HintLabel
                htmlFor="settings-lookback-days"
                hint={INFO_HINTS.settings.lookbackDays}
              >
                回溯天数
              </HintLabel>
              <input
                id="settings-lookback-days"
                type="number"
                value={form.download_lookback_days}
                onChange={(e) =>
                  setForm({
                    ...form,
                    download_lookback_days: Number(e.target.value),
                  })
                }
                className="w-full border rounded px-3 py-1.5 text-sm"
              />
            </div>
            <div>
              <label className="block text-sm text-muted-foreground mb-1">&nbsp;</label>
              <span className="text-sm text-muted-foreground/70">天 (增量拉取)</span>
            </div>
          </div>

          <div className="mt-3">
            <div className="mb-1 flex items-center gap-1 text-sm text-muted-foreground">
              <span>拉取市场</span>
              <InfoHint content={INFO_HINTS.settings.downloadMarkets} />
            </div>
            <div className="flex gap-4">
              {marketOptions.map((opt) => (
                <label
                  key={opt.value}
                  className="flex items-center gap-2 text-sm"
                >
                  <input
                    type="checkbox"
                    checked={form.download_markets.includes(opt.value)}
                    onChange={(e) => {
                      const markets = e.target.checked
                        ? [...form.download_markets, opt.value]
                        : form.download_markets.filter((m) => m !== opt.value);
                      setForm({ ...form, download_markets: markets });
                    }}
                  />
                  {opt.label}
                </label>
              ))}
            </div>
          </div>

          <div className="mt-3">
            <div className="mb-1 flex items-center gap-1 text-sm text-muted-foreground">
              <span>拉取类型</span>
              <InfoHint content={INFO_HINTS.settings.downloadTypes} />
            </div>
            <div className="flex gap-4">
              {typeOptions.map((opt) => (
                <label
                  key={opt.value}
                  className="flex items-center gap-2 text-sm"
                >
                  <input
                    type="checkbox"
                    checked={form.download_types.includes(opt.value)}
                    onChange={(e) => {
                      const types = e.target.checked
                        ? [...form.download_types, opt.value]
                        : form.download_types.filter((t) => t !== opt.value);
                      setForm({ ...form, download_types: types });
                    }}
                  />
                  {opt.label}
                </label>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* Save button */}
      <div className="flex items-center gap-4">
        <button
          onClick={() => save.mutate(form)}
          disabled={save.isPending}
          className="bg-primary text-white rounded px-6 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
        >
          {save.isPending ? "保存中..." : "保存配置"}
        </button>
        {save.isSuccess && (
          <span className="text-sm text-success">已保存</span>
        )}
        {save.isError && (
          <span className="text-sm text-destructive">
            {(save.error as Error).message}
          </span>
        )}
      </div>
    </div>
  );
}

function LLMProviderSection() {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ["llm-config"],
    queryFn: api.getLlmConfig,
  });

  const [form, setForm] = useState<LLMConfig | null>(null);

  useEffect(() => {
    if (data) setForm(data);
  }, [data]);

  const save = useMutation({
    mutationFn: (body: Partial<LLMConfigUpdate>) => api.updateLlmConfig(body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["llm-config"] }),
  });

  if (!form) return null;

  const onSave = () => {
    const body: Partial<LLMConfigUpdate> = {
      provider: form.provider,
      base_url: form.base_url,
      model: form.model,
      timeout_seconds: form.timeout_seconds,
      max_retries: form.max_retries,
    };
    // api_key 哨兵:保留掩码 "********" 不传,后端保持原值;其它值(含空串)覆盖。
    if (form.api_key !== "********") {
      body.api_key = form.api_key;
    }
    save.mutate(body);
  };

  const isHttp = form.provider === "openai_compatible";

  return (
    <div className="bg-card rounded-lg shadow p-5 mb-6">
      <h2 className="text-lg font-semibold mb-1">LLM Provider</h2>
      <p className="text-sm text-muted-foreground/80 mb-4">
        AI 研究助手使用 OpenAI 兼容 provider 生成因子假设、策略草案与问答。仅服务研究,
        不连接实盘账户 / 订单 / 持仓。修改后立即保存到 .env 并热重建 provider。
      </p>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div>
          <label className="block text-sm text-muted-foreground mb-1">Provider</label>
          <select
            value={form.provider}
            onChange={(e) =>
              setForm({ ...form, provider: e.target.value as LLMConfig["provider"] })
            }
            className="w-full border rounded px-3 py-1.5 text-sm bg-card"
          >
            <option value="fake">fake（不调用公网,默认）</option>
            <option value="openai_compatible">openai_compatible（真实 HTTP）</option>
          </select>
        </div>
        <div>
          <label className="block text-sm text-muted-foreground mb-1">模型</label>
          <input
            type="text"
            value={form.model}
            onChange={(e) => setForm({ ...form, model: e.target.value })}
            className="w-full border rounded px-3 py-1.5 text-sm"
            placeholder="gpt-4o-mini"
          />
        </div>
        <div className="sm:col-span-2">
          <label className="block text-sm text-muted-foreground mb-1">Base URL</label>
          <input
            type="text"
            value={form.base_url}
            onChange={(e) => setForm({ ...form, base_url: e.target.value })}
            className="w-full border rounded px-3 py-1.5 text-sm"
            placeholder="https://api.openai.com/v1"
            disabled={!isHttp}
          />
        </div>
        <div className="sm:col-span-2">
          <label className="block text-sm text-muted-foreground mb-1">
            API Key
            {form.api_key_set
              ? "（已设置;保留掩码不变,输入新值覆盖）"
              : "（未设置）"}
          </label>
          <input
            type="password"
            value={form.api_key}
            onChange={(e) => setForm({ ...form, api_key: e.target.value })}
            className="w-full border rounded px-3 py-1.5 text-sm"
            placeholder="sk-..."
            disabled={!isHttp}
          />
        </div>
        <div>
          <label className="block text-sm text-muted-foreground mb-1">超时(秒)</label>
          <input
            type="number"
            value={form.timeout_seconds}
            onChange={(e) =>
              setForm({ ...form, timeout_seconds: Number(e.target.value) })
            }
            className="w-full border rounded px-3 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="block text-sm text-muted-foreground mb-1">最大重试</label>
          <input
            type="number"
            value={form.max_retries}
            onChange={(e) =>
              setForm({ ...form, max_retries: Number(e.target.value) })
            }
            className="w-full border rounded px-3 py-1.5 text-sm"
          />
        </div>
      </div>

      <div className="flex items-center gap-4 mt-4">
        <button
          onClick={onSave}
          disabled={save.isPending}
          className="bg-primary text-white rounded px-6 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
        >
          {save.isPending ? "保存中..." : "保存 LLM 配置"}
        </button>
        {save.isSuccess && <span className="text-sm text-success">已保存</span>}
        {save.isError && (
          <span className="text-sm text-destructive">
            {(save.error as Error).message}
          </span>
        )}
      </div>
    </div>
  );
}
