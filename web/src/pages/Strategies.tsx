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
import FactorSelectionForm from "../components/FactorSelectionForm";
import StrategyParamForm from "../components/StrategyParamForm";
import { useT } from "@/i18n";
import {
  defaultStrategyParams,
  normalizeStrategyParams,
  strategyFieldErrorsFrom,
  type StrategyFieldErrors,
  type StrategyParams,
  validateStrategyParams,
} from "../lib/strategyParams";
import {
  api,
  DEFAULT_FACTOR_SELECTION,
  type FactorSelectionInput,
  type StrategyPresetInput,
} from "../lib/api";
import { INFO_HINTS } from "../lib/infoHints";

export default function Strategies() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { tl } = useT();
  const [selectedKind, setSelectedKind] = useState("ma_cross");
  const [selectedPresetId, setSelectedPresetId] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [params, setParams] = useState<StrategyParams>({});
  const [selection, setSelection] = useState<FactorSelectionInput>({
    ...DEFAULT_FACTOR_SELECTION,
  });
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
      setSelection(preset.selection);
      setFieldErrors({});
      setNotice(
        tl({
          zh: "预设已保存。它不会启动策略或改变实盘配置。",
          en: "Preset saved. It does not start any strategy or change live trading configuration.",
        }),
      );
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
    setSelection({ ...DEFAULT_FACTOR_SELECTION });
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
    setSelection(preset.selection);
    setFieldErrors({});
    setNotice("");
    savePreset.reset();
  }

  function submit() {
    if (!definition) return;
    const errors = validateStrategyParams(definition, params);
    if (!name.trim())
      errors.name = tl({ zh: "请输入预设名称", en: "Preset name is required" });
    setFieldErrors(errors);
    setNotice("");
    if (Object.keys(errors).length > 0) return;

    savePreset.mutate({
      name: name.trim(),
      strategy: definition.kind,
      params: normalizeStrategyParams(definition, params),
      selection,
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
              selection,
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
      <div className="mx-auto max-w-7xl">
        <h1 className="text-2xl font-bold text-foreground">
          {tl({ zh: "策略配置", en: "Strategy configuration" })}
        </h1>
        <p className="mt-3 rounded-lg bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {tl({ zh: "无法加载内置策略：", en: "Unable to load built-in strategies: " })}
          {(strategiesQuery.error as Error | null)?.message ??
            tl({ zh: "没有可用策略", en: "No strategies available" })}
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-7xl space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-foreground">
            {tl({ zh: "策略配置", en: "Strategy configuration" })}
          </h1>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-muted-foreground">
            {tl({
              zh: "配置经过测试的内置策略参数，并保存为可复用预设。参数由后端 schema 自动生成，页面不接收或执行策略代码。",
              en: "Configure parameters for tested built-in strategies and save them as reusable presets. Parameters are generated from the backend schema; the page never receives or executes strategy code.",
            })}
          </p>
        </div>
        <div className="flex items-center gap-2 rounded-md bg-success/10 px-3 py-2 text-xs font-medium text-success">
          <ShieldCheck size={16} aria-hidden="true" />
          {tl({ zh: "保存预设不会启动策略", en: "Saving a preset does not start a strategy" })}
        </div>
      </div>

      <div className="grid overflow-hidden rounded-lg border border-border bg-card lg:grid-cols-[15rem_minmax(0,1fr)_18rem]">
        <aside className="border-b border-border bg-background p-3 lg:border-b-0 lg:border-r">
          <div className="mb-2 flex items-center gap-1 px-2 text-xs font-semibold text-muted-foreground">
            <span>{tl({ zh: "内置策略", en: "Built-in strategies" })}</span>
            <InfoHint content={INFO_HINTS.strategies.builtinStrategies} />
          </div>
          <div className="space-y-1">
            {strategiesQuery.data?.map((strategy) => (
              <button
                key={strategy.kind}
                type="button"
                onClick={() => selectStrategy(strategy.kind)}
                className={`w-full rounded-md px-3 py-2.5 text-left transition-colors focus:outline-none focus:ring-2 focus:ring-ring ${
                  strategy.kind === selectedKind
                    ? "bg-card text-foreground"
                    : "text-foreground hover:bg-muted/50"
                }`}
              >
                <span className="block text-sm font-medium">{strategy.name}</span>
                <span
                  className={`mt-0.5 block text-xs ${
                    strategy.kind === selectedKind ? "text-muted-foreground" : "text-muted-foreground"
                  }`}
                >
                  {strategy.supports_backtest
                    ? tl({ zh: "可用于回测", en: "Backtest-capable" })
                    : tl({ zh: "实时时钟策略", en: "Realtime clock strategy" })}
                </span>
              </button>
            ))}
          </div>
        </aside>

        <section className="min-w-0 p-5 sm:p-6">
          <div className="mb-5 flex flex-wrap items-start justify-between gap-3 border-b border-border pb-5">
            <div>
              <div className="flex items-center gap-1">
                <h2 className="text-lg font-semibold text-foreground">{definition.name}</h2>
                <InfoHint content={INFO_HINTS.strategies.backtestCapability} />
              </div>
              <p className="mt-1 max-w-2xl text-sm leading-6 text-muted-foreground">
                {definition.description}
              </p>
            </div>
            <code className="rounded bg-secondary px-2 py-1 text-xs text-muted-foreground">
              {definition.kind}
            </code>
          </div>

          <div className="mb-5">
            <HintLabel
              htmlFor="preset-name"
              hint={INFO_HINTS.strategies.presetName}
              labelClassName="text-sm font-medium text-foreground"
            >
              {tl({ zh: "预设名称", en: "Preset name" })} <span className="text-destructive">*</span>
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
              placeholder={tl({ zh: "例如：沪深 300 中期趋势", en: "e.g. CSI 300 mid-term trend" })}
              aria-invalid={Boolean(fieldErrors.name)}
              className={`w-full rounded-md border px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary/30 ${
                fieldErrors.name ? "border-destructive" : "border-input focus:border-primary"
              }`}
            />
            {fieldErrors.name && (
              <p className="mt-1 text-xs text-destructive">{fieldErrors.name}</p>
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

          {definition.supports_backtest && (
            <FactorSelectionForm
              value={selection}
              onChange={(next) => {
                setSelection(next);
                setNotice("");
                savePreset.reset();
              }}
            />
          )}

          {savePreset.error && (
            <p className="mt-4 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {(savePreset.error as Error).message}
            </p>
          )}
          {notice && (
            <p className="mt-4 rounded-md bg-success/10 px-3 py-2 text-sm text-success">
              {notice}
            </p>
          )}

          <div className="mt-6 flex flex-wrap items-center gap-2 border-t border-border pt-5">
            <button
              type="button"
              onClick={submit}
              disabled={savePreset.isPending}
              className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
            >
              <Save size={16} aria-hidden="true" />
              {savePreset.isPending
                ? tl({ zh: "保存中…", en: "Saving…" })
                : selectedPresetId === null
                  ? tl({ zh: "保存新预设", en: "Save new preset" })
                  : tl({ zh: "更新预设", en: "Update preset" })}
            </button>
            {selectedPresetId !== null && (
              <button
                type="button"
                onClick={() => resetEditor()}
                className="inline-flex items-center gap-2 rounded-md border border-input px-4 py-2 text-sm font-medium text-foreground hover:bg-background"
              >
                <Plus size={16} aria-hidden="true" />
                {tl({ zh: "另存新预设", en: "Save as new preset" })}
              </button>
            )}
            {definition.supports_backtest && (
              <button
                type="button"
                onClick={openBacktest}
                className="ml-auto inline-flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium text-primary hover:bg-accent"
              >
                <FlaskConical size={16} aria-hidden="true" />
                {tl({ zh: "带入回测", en: "Load into backtest" })}
                <ArrowRight size={15} aria-hidden="true" />
              </button>
            )}
          </div>
        </section>

        <aside className="border-t border-border bg-background p-4 lg:border-l lg:border-t-0">
          <div className="mb-3 flex items-center justify-between">
            <div className="flex items-center gap-1">
              <h2 className="text-sm font-semibold text-foreground">
                {tl({ zh: "已保存预设", en: "Saved presets" })}
              </h2>
              <InfoHint content={INFO_HINTS.strategies.savedPresets} />
            </div>
            <span className="text-xs text-muted-foreground">
              {tl({
                zh: `${presetsQuery.data?.length ?? 0} 个`,
                en: `${presetsQuery.data?.length ?? 0}`,
              })}
            </span>
          </div>

          {presetsQuery.isPending && <PresetListSkeleton />}
          {presetsQuery.error && (
            <p className="rounded-md bg-destructive/10 p-3 text-xs text-destructive">
              {(presetsQuery.error as Error).message}
            </p>
          )}
          {presetsQuery.data?.length === 0 && (
            <div className="rounded-lg border border-dashed border-input p-4 text-center">
              <p className="text-sm font-medium text-foreground">
                {tl({ zh: "还没有策略预设", en: "No strategy presets yet" })}
              </p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                {tl({
                  zh: "在左侧选择策略，填写参数后保存；以后可以直接载入回测。",
                  en: "Pick a strategy on the left, fill in the parameters and save; you can then load it directly into a backtest.",
                })}
              </p>
            </div>
          )}

          <div className="space-y-2">
            {presetsQuery.data?.map((preset) => (
              <div
                key={preset.id}
                className={`rounded-lg border bg-card p-3 ${
                  preset.id === selectedPresetId ? "border-primary" : "border-border"
                }`}
              >
                <button
                  type="button"
                  onClick={() => loadPreset(preset.id)}
                  className="w-full text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <span className="block truncate text-sm font-medium text-foreground">
                    {preset.name}
                  </span>
                  <span className="mt-1 block text-xs text-muted-foreground">
                    {strategiesQuery.data?.find((item) => item.kind === preset.strategy)?.name ??
                      preset.strategy}
                  </span>
                </button>

                {deleteConfirmId === preset.id ? (
                  <div className="mt-3 flex items-center justify-between gap-2 border-t border-border pt-2">
                    <span className="text-xs text-destructive">
                      {tl({ zh: "确认删除？", en: "Confirm deletion?" })}
                    </span>
                    <div className="flex gap-2">
                      <button
                        type="button"
                        onClick={() => setDeleteConfirmId(null)}
                        className="text-xs text-muted-foreground hover:text-foreground"
                      >
                        {tl({ zh: "取消", en: "Cancel" })}
                      </button>
                      <button
                        type="button"
                        onClick={() => deletePreset.mutate(preset.id)}
                        disabled={deletePreset.isPending}
                        className="text-xs font-medium text-destructive hover:text-destructive"
                      >
                        {tl({ zh: "删除", en: "Delete" })}
                      </button>
                    </div>
                  </div>
                ) : (
                  <button
                    type="button"
                    onClick={() => setDeleteConfirmId(preset.id)}
                    className="mt-2 inline-flex items-center gap-1 text-xs text-muted-foreground/70 hover:text-destructive"
                  >
                    <Trash2 size={13} aria-hidden="true" />
                    {tl({ zh: "删除", en: "Delete" })}
                  </button>
                )}
              </div>
            ))}
          </div>
        </aside>
      </div>

      <p className="mt-4 text-xs leading-5 text-muted-foreground">
        {tl({
          zh: "安全边界：本页面不上传、导入或执行 Python 代码，也不会修改正在运行的实盘策略。 在线策略代码能力不在本阶段范围内。",
          en: "Safety boundary: this page never uploads, imports, or executes Python code, nor modifies running live strategies. Online strategy code capability is out of scope for this phase.",
        })}
      </p>
    </div>
  );
}

function StrategiesSkeleton() {
  const { tl } = useT();
  return (
    <div
      className="mx-auto max-w-7xl animate-pulse"
      aria-label={tl({ zh: "正在加载策略配置", en: "Loading strategy configuration" })}
    >
      <div className="h-8 w-36 rounded bg-muted" />
      <div className="mt-3 h-4 w-full max-w-xl rounded bg-muted" />
      <div className="mt-6 h-[30rem] rounded-lg bg-muted" />
    </div>
  );
}

function PresetListSkeleton() {
  const { tl } = useT();
  return (
    <div
      className="space-y-2 animate-pulse"
      aria-label={tl({ zh: "正在加载策略预设", en: "Loading strategy presets" })}
    >
      <div className="h-16 rounded-lg bg-muted" />
      <div className="h-16 rounded-lg bg-muted" />
    </div>
  );
}
