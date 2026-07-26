import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import InfoHint, { HintLabel } from "../components/InfoHint";
import { api, type SchedulerConfig } from "../lib/api";
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

  if (!form) return <div className="text-gray-400">加载中...</div>;

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
      <div className="bg-white rounded-lg shadow p-5 mb-6">
        <h2 className="text-lg font-semibold mb-3">数据源</h2>
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-1 text-sm text-gray-600">
            <span>当前数据源</span>
            <InfoHint content={INFO_HINTS.settings.dataProvider} />
          </div>
          <span
            className={`px-3 py-1 rounded text-sm font-medium ${
              form.data_provider === "yfinance"
                ? "bg-blue-100 text-blue-700"
                : "bg-orange-100 text-orange-700"
            }`}
          >
            {form.data_provider}
          </span>
          <span className="text-sm text-gray-400">
            切换: FINBOARD_DATA_PROVIDER=akshare / yfinance 环境变量
          </span>
        </div>
      </div>

      {/* Scheduled Tasks */}
      <div className="bg-white rounded-lg shadow p-5 mb-6">
        <h2 className="text-lg font-semibold mb-4">定时任务</h2>

        {/* Universe Sync Task */}
        <div className="border rounded-lg p-4 mb-4">
          <div className="flex items-center justify-between mb-3">
            <div>
              <h3 className="font-medium">标的池同步</h3>
              <p className="text-sm text-gray-500 mt-0.5">
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
              <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-blue-600" />
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
            <span className="text-sm text-gray-400">(Asia/Shanghai, 仅交易日)</span>
          </div>
        </div>

        {/* Bulk Download Task */}
        <div className="border rounded-lg p-4">
          <div className="flex items-center justify-between mb-3">
            <div>
              <h3 className="font-medium">增量数据拉取</h3>
              <p className="text-sm text-gray-500 mt-0.5">
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
              <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-blue-600" />
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
              <label className="block text-sm text-gray-600 mb-1">&nbsp;</label>
              <span className="text-sm text-gray-400">天 (增量拉取)</span>
            </div>
          </div>

          <div className="mt-3">
            <div className="mb-1 flex items-center gap-1 text-sm text-gray-600">
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
            <div className="mb-1 flex items-center gap-1 text-sm text-gray-600">
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
          className="bg-blue-600 text-white rounded px-6 py-2 text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
        >
          {save.isPending ? "保存中..." : "保存配置"}
        </button>
        {save.isSuccess && (
          <span className="text-sm text-green-600">已保存</span>
        )}
        {save.isError && (
          <span className="text-sm text-red-600">
            {(save.error as Error).message}
          </span>
        )}
      </div>
    </div>
  );
}
