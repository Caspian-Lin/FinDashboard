import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  ArrowRight,
  FlaskConical,
  Plus,
  Save,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import InfoHint, { HintLabel } from "../components/InfoHint";
import StrategyParamForm from "../components/StrategyParamForm";
import {
  defaultStrategyParams,
  normalizeStrategyParams,
  strategyFieldErrorsFrom,
  type StrategyFieldErrors,
  type StrategyParams,
  validateStrategyParams,
} from "../lib/strategyParams";
import { api, type StrategyPresetInput } from "../lib/api";
import { INFO_HINTS } from "../lib/infoHints";

export default function Strategies() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [selectedKind, setSelectedKind] = useState("ma_cross");
  const [selectedPresetId, setSelectedPresetId] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [params, setParams] = useState<StrategyParams>({});
  const [fieldErrors, setFieldErrors] = useState<StrategyFieldErrors>({});
  const [notice, setNotice] = useState("");
  const [deleteConfirmId, setDeleteConfirmId] = useState<number | null>(null);

  const strategiesQuery = useQuery({
    queryKey: ["strategies"],
    queryFn: api.getStrategies,
  });
  const presetsQuery = useQuery({
    queryKey: ["strategy-presets"],
    queryFn: api.getStrategyPresets,
  });

  const definition = useMemo(
    () => strategiesQuery.data?.find((item) => item.kind === selectedKind),
    [selectedKind, strategiesQuery.data],
  );

  useEffect(() => {
    if (definition && definition.params.length > 0 && Object.keys(params).length === 0) {
      setParams(defaultStrategyParams(definition));
    }
  }, [definition, params]);

  const savePreset = useMutation({
    mutationFn: (input: StrategyPresetInput) =>
      selectedPresetId === null
        ? api.createStrategyPreset(input)
        : api.updateStrategyPreset(selectedPresetId, input),
    onSuccess: (preset) => {
      setSelectedPresetId(preset.id);
      setName(preset.name);
      setParams(preset.params);
      setFieldErrors({});
      setNotice("预设已保存。它不会启动策略或改变实盘配置。");
      queryClient.invalidateQueries({ queryKey: ["strategy-presets"] });
    },
    onError: (error) => {
      setFieldErrors(strategyFieldErrorsFrom(error));
      setNotice("");
    },
  });

  const deletePreset = useMutation({
    mutationFn: api.deleteStrategyPreset,
    onSuccess: () => {
      if (selectedPresetId === deleteConfirmId) resetEditor();
      setDeleteConfirmId(null);
      queryClient.invalidateQueries({ queryKey: ["strategy-presets"] });
    },
  });

  function resetEditor(nextKind = selectedKind) {
    const nextDefinition = strategiesQuery.data?.find((item) => item.kind === nextKind);
    setSelectedPresetId(null);
    setName("");
    setParams(nextDefinition ? defaultStrategyParams(nextDefinition) : {});
    setFieldErrors({});
    setNotice("");
  }

  function selectStrategy(kind: string) {
    savePreset.reset();
    setSelectedKind(kind);
    resetEditor(kind);
  }

  function loadPreset(presetId: number) {
    const preset = presetsQuery.data?.find((item) => item.id === presetId);
    const presetDefinition = strategiesQuery.data?.find(
      (item) => item.kind === preset?.strategy,
    );
    if (!preset || !presetDefinition) return;
    setSelectedKind(preset.strategy);
    setSelectedPresetId(preset.id);
    setName(preset.name);
    setParams({ ...defaultStrategyParams(presetDefinition), ...preset.params });
    setFieldErrors({});
    setNotice("");
    savePreset.reset();
  }

  function submit() {
    if (!definition) return;
    const errors = validateStrategyParams(definition, params);
    if (!name.trim()) errors.name = "请输入预设名称";
    setFieldErrors(errors);
    setNotice("");
    if (Object.keys(errors).length > 0) return;

    savePreset.mutate({
      name: name.trim(),
      strategy: definition.kind,
      params: normalizeStrategyParams(definition, params),
    });
  }

  function openBacktest() {
    if (selectedPresetId !== null) {
      navigate(`/backtest?preset=${selectedPresetId}`);
      return;
    }
    navigate("/backtest", {
      state: definition
        ? {
            strategyDraft: {
              strategy: definition.kind,
              params: normalizeStrategyParams(definition, params),
            },
          }
        : undefined,
    });
  }

  if (strategiesQuery.isPending) {
    return <StrategiesSkeleton />;
  }

  if (strategiesQuery.error || !definition) {
    return (
      <div className="max-w-3xl">
        <h1 className="text-2xl font-bold text-slate-900">策略配置</h1>
        <p className="mt-3 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-700">
          无法加载内置策略：{(strategiesQuery.error as Error | null)?.message ?? "没有可用策略"}
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-7xl">
      <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">策略配置</h1>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-slate-600">
            配置经过测试的内置策略参数，并保存为可复用预设。参数由后端 schema
            自动生成，页面不接收或执行策略代码。
          </p>
        </div>
        <div className="flex items-center gap-2 rounded-lg bg-emerald-50 px-3 py-2 text-xs font-medium text-emerald-800">
          <ShieldCheck size={16} aria-hidden="true" />
          保存预设不会启动策略
        </div>
      </div>

      <div className="grid overflow-hidden rounded-xl border border-slate-200 bg-white lg:grid-cols-[15rem_minmax(0,1fr)_18rem]">
        <aside className="border-b border-slate-200 bg-slate-50 p-3 lg:border-b-0 lg:border-r">
          <div className="mb-2 flex items-center gap-1 px-2 text-xs font-semibold text-slate-500">
            <span>内置策略</span>
            <InfoHint content={INFO_HINTS.strategies.builtinStrategies} />
          </div>
          <div className="space-y-1">
            {strategiesQuery.data?.map((strategy) => (
              <button
                key={strategy.kind}
                type="button"
                onClick={() => selectStrategy(strategy.kind)}
                className={`w-full rounded-md px-3 py-2.5 text-left transition-colors focus:outline-none focus:ring-2 focus:ring-blue-300 ${
                  strategy.kind === selectedKind
                    ? "bg-slate-900 text-white"
                    : "text-slate-700 hover:bg-slate-200"
                }`}
              >
                <span className="block text-sm font-medium">{strategy.name}</span>
                <span
                  className={`mt-0.5 block text-xs ${
                    strategy.kind === selectedKind ? "text-slate-300" : "text-slate-500"
                  }`}
                >
                  {strategy.supports_backtest ? "可用于回测" : "实时时钟策略"}
                </span>
              </button>
            ))}
          </div>
        </aside>

        <section className="min-w-0 p-5 sm:p-6">
          <div className="mb-5 flex flex-wrap items-start justify-between gap-3 border-b border-slate-200 pb-5">
            <div>
              <div className="flex items-center gap-1">
                <h2 className="text-lg font-semibold text-slate-900">{definition.name}</h2>
                <InfoHint content={INFO_HINTS.strategies.backtestCapability} />
              </div>
              <p className="mt-1 max-w-2xl text-sm leading-6 text-slate-600">
                {definition.description}
              </p>
            </div>
            <code className="rounded bg-slate-100 px-2 py-1 text-xs text-slate-600">
              {definition.kind}
            </code>
          </div>

          <div className="mb-5">
            <HintLabel
              htmlFor="preset-name"
              hint={INFO_HINTS.strategies.presetName}
              labelClassName="text-sm font-medium text-slate-700"
            >
              预设名称 <span className="text-red-600">*</span>
            </HintLabel>
            <input
              id="preset-name"
              value={name}
              onChange={(event) => {
                setName(event.target.value);
                setFieldErrors((current) => ({ ...current, name: "" }));
                savePreset.reset();
              }}
              maxLength={100}
              placeholder="例如：沪深 300 中期趋势"
              aria-invalid={Boolean(fieldErrors.name)}
              className={`w-full rounded-md border px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-blue-200 ${
                fieldErrors.name ? "border-red-400" : "border-slate-300 focus:border-blue-500"
              }`}
            />
            {fieldErrors.name && (
              <p className="mt-1 text-xs text-red-600">{fieldErrors.name}</p>
            )}
          </div>

          <StrategyParamForm
            definition={definition}
            values={params}
            onChange={(next) => {
              setParams(next);
              setFieldErrors({});
              setNotice("");
              savePreset.reset();
            }}
            errors={fieldErrors}
            disabled={savePreset.isPending}
          />

          {savePreset.error && (
            <p className="mt-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
              {(savePreset.error as Error).message}
            </p>
          )}
          {notice && (
            <p className="mt-4 rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
              {notice}
            </p>
          )}

          <div className="mt-6 flex flex-wrap items-center gap-2 border-t border-slate-200 pt-5">
            <button
              type="button"
              onClick={submit}
              disabled={savePreset.isPending}
              className="inline-flex items-center gap-2 rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-300 disabled:opacity-50"
            >
              <Save size={16} aria-hidden="true" />
              {savePreset.isPending
                ? "保存中…"
                : selectedPresetId === null
                  ? "保存新预设"
                  : "更新预设"}
            </button>
            {selectedPresetId !== null && (
              <button
                type="button"
                onClick={() => resetEditor()}
                className="inline-flex items-center gap-2 rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
              >
                <Plus size={16} aria-hidden="true" />
                另存新预设
              </button>
            )}
            {definition.supports_backtest && (
              <button
                type="button"
                onClick={openBacktest}
                className="ml-auto inline-flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium text-blue-700 hover:bg-blue-50"
              >
                <FlaskConical size={16} aria-hidden="true" />
                带入回测
                <ArrowRight size={15} aria-hidden="true" />
              </button>
            )}
          </div>
        </section>

        <aside className="border-t border-slate-200 bg-slate-50 p-4 lg:border-l lg:border-t-0">
          <div className="mb-3 flex items-center justify-between">
            <div className="flex items-center gap-1">
              <h2 className="text-sm font-semibold text-slate-800">已保存预设</h2>
              <InfoHint content={INFO_HINTS.strategies.savedPresets} />
            </div>
            <span className="text-xs text-slate-500">{presetsQuery.data?.length ?? 0} 个</span>
          </div>

          {presetsQuery.isPending && <PresetListSkeleton />}
          {presetsQuery.error && (
            <p className="rounded-md bg-red-50 p-3 text-xs text-red-700">
              {(presetsQuery.error as Error).message}
            </p>
          )}
          {presetsQuery.data?.length === 0 && (
            <div className="rounded-lg border border-dashed border-slate-300 p-4 text-center">
              <p className="text-sm font-medium text-slate-700">还没有策略预设</p>
              <p className="mt-1 text-xs leading-5 text-slate-500">
                在左侧选择策略，填写参数后保存；以后可以直接载入回测。
              </p>
            </div>
          )}

          <div className="space-y-2">
            {presetsQuery.data?.map((preset) => (
              <div
                key={preset.id}
                className={`rounded-lg border bg-white p-3 ${
                  preset.id === selectedPresetId ? "border-blue-500" : "border-slate-200"
                }`}
              >
                <button
                  type="button"
                  onClick={() => loadPreset(preset.id)}
                  className="w-full text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-300"
                >
                  <span className="block truncate text-sm font-medium text-slate-800">
                    {preset.name}
                  </span>
                  <span className="mt-1 block text-xs text-slate-500">
                    {strategiesQuery.data?.find((item) => item.kind === preset.strategy)?.name ??
                      preset.strategy}
                  </span>
                </button>

                {deleteConfirmId === preset.id ? (
                  <div className="mt-3 flex items-center justify-between gap-2 border-t border-slate-200 pt-2">
                    <span className="text-xs text-red-700">确认删除？</span>
                    <div className="flex gap-2">
                      <button
                        type="button"
                        onClick={() => setDeleteConfirmId(null)}
                        className="text-xs text-slate-600 hover:text-slate-900"
                      >
                        取消
                      </button>
                      <button
                        type="button"
                        onClick={() => deletePreset.mutate(preset.id)}
                        disabled={deletePreset.isPending}
                        className="text-xs font-medium text-red-700 hover:text-red-900"
                      >
                        删除
                      </button>
                    </div>
                  </div>
                ) : (
                  <button
                    type="button"
                    onClick={() => setDeleteConfirmId(preset.id)}
                    className="mt-2 inline-flex items-center gap-1 text-xs text-slate-400 hover:text-red-700"
                  >
                    <Trash2 size={13} aria-hidden="true" />
                    删除
                  </button>
                )}
              </div>
            ))}
          </div>
        </aside>
      </div>

      <p className="mt-4 text-xs leading-5 text-slate-500">
        安全边界：本页面不上传、导入或执行 Python 代码，也不会修改正在运行的实盘策略。
        在线策略代码能力不在本阶段范围内。
      </p>
    </div>
  );
}

function StrategiesSkeleton() {
  return (
    <div className="mx-auto max-w-7xl animate-pulse" aria-label="正在加载策略配置">
      <div className="h-8 w-36 rounded bg-slate-200" />
      <div className="mt-3 h-4 w-full max-w-xl rounded bg-slate-200" />
      <div className="mt-6 h-[30rem] rounded-xl bg-slate-200" />
    </div>
  );
}

function PresetListSkeleton() {
  return (
    <div className="space-y-2 animate-pulse" aria-label="正在加载策略预设">
      <div className="h-16 rounded-lg bg-slate-200" />
      <div className="h-16 rounded-lg bg-slate-200" />
    </div>
  );
}
