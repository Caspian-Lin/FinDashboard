import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input, Textarea } from "@/components/ui/input";
import { ErrorState, LoadingState } from "@/components/ui/states";
import { useT, type LocalizedText } from "@/i18n";
import { researchRunApi } from "@/lib/research";
import { topicActionApi, workspaceApi, type Topic } from "@/lib/research-workspace";

const dateFields: { key: string; label: LocalizedText }[] = [
  { key: "train_start", label: { zh: "训练开始", en: "Training start" } },
  { key: "train_end", label: { zh: "训练结束", en: "Training end" } },
  { key: "validation_start", label: { zh: "验证开始", en: "Validation start" } },
  { key: "validation_end", label: { zh: "验证结束", en: "Validation end" } },
  { key: "test_start", label: { zh: "最终测试开始", en: "Final test start" } },
  { key: "test_end", label: { zh: "最终测试结束", en: "Final test end" } },
];

const selectStyle = "h-11 w-full rounded-md border border-input bg-background px-3 text-sm";

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function readableFields(values: Record<string, unknown>) {
  return Object.entries(values).filter(([, value]) => ["number", "string", "boolean"].includes(typeof value))
    .map(([field, value]) => `${field} ${String(value)}`).join(" · ");
}

export default function FormalExperimentDialog({
  topic,
  onClose,
  onCreated,
}: {
  topic?: Topic;
  onClose: () => void;
  onCreated: (id: string, topicId: string) => void;
}) {
  const { tl } = useT();
  const queryClient = useQueryClient();
  const [topicId, setTopicId] = useState("");
  const [runId, setRunId] = useState("");
  const [hypothesis, setHypothesis] = useState("");
  const [dates, setDates] = useState<Record<string, string>>({});
  const [usedWindows, setUsedWindows] = useState<{ start: string; end: string }[]>([]);
  const [declaredWindows, setDeclaredWindows] = useState(false);
  const [trainDays, setTrainDays] = useState(126);
  const [testDays, setTestDays] = useState(21);
  const [stepDays, setStepDays] = useState(21);
  const [mode, setMode] = useState("rolling");
  const [operationId] = useState(() => `web-formal-${globalThis.crypto.randomUUID?.() ?? Date.now()}`);
  const topics = useQuery({
    queryKey: ["formal-experiment-topics"],
    queryFn: () => workspaceApi.topics(0, "active"),
    enabled: !topic,
  });
  const activeTopics = topics.data?.items ?? [];
  const selectedTopic = topic ?? activeTopics.find((item) => item.topic_id === topicId);
  const bases = useQuery({
    queryKey: ["formal-experiment-baselines"],
    queryFn: () => researchRunApi.list({ status: ["completed"], strategy_kind: "multi_factor", limit: 100 }),
  });
  const baseline = useQuery({
    queryKey: ["reports", "run-detail", runId],
    queryFn: () => researchRunApi.get(runId),
    enabled: Boolean(runId),
  });
  const manifest = baseline.data?.manifest;
  const bench = record(manifest?.benchmark_config);
  const benchmark = record(bench.overrides).symbol ?? bench.symbol;
  const requestBody = {
    hypothesis: hypothesis.trim(),
    version_stamp: {
      matching_model_version: "v2",
      asset_rules_version: "v3",
      strategy_kind: "multi_factor",
      selection_config: {
        validation_trial_runner: { kind: "research_spec", baseline_run_id: runId, used_windows: usedWindows },
      },
    },
    plan: {
      ...dates,
      mode,
      train_window_days: trainDays,
      test_window_days: testDays,
      step_days: stepDays,
      trial_budget: 1,
      benchmark_symbol: benchmark,
    },
  };
  const validDates = dateFields.every(({ key }, index) => dates[key] && (index === 0 || dates[dateFields[index - 1].key] <= dates[key]))
    && dates.train_end < dates.validation_start && dates.validation_end < dates.test_start;
  const goal = selectedTopic?.goal?.version;
  const valid = selectedTopic?.status === "active" && goal && runId && typeof benchmark === "string"
    && hypothesis.trim().length >= 10 && validDates && declaredWindows
    && usedWindows.every(({ start, end }) => start && end && start <= end)
    && [trainDays, testDays, stepDays].every((days) => Number.isInteger(days) && days > 0);
  const currentPreviewInput = JSON.stringify([selectedTopic?.topic_id, goal, requestBody]);
  const preview = useMutation({
    mutationFn: ({ activeTopic, goalVersion, payload }: {
      activeTopic: string;
      goalVersion: string;
      payload: Record<string, unknown>;
      requestKey: string;
    }) => topicActionApi.previewValidation(activeTopic, goalVersion, payload),
  });
  const ready = preview.variables?.requestKey === currentPreviewInput ? preview.data : undefined;
  const save = useMutation({
    mutationFn: () => topicActionApi.act(selectedTopic!.topic_id, {
      action: "validation_experiment.create",
      goal_version: goal!,
      operation_id: operationId,
      payload: { ...requestBody, expected_preview_checksum: ready!.preview_checksum },
    }),
    onSuccess: (receipt) => {
      void queryClient.invalidateQueries({ queryKey: ["validation-experiments"] });
      void queryClient.invalidateQueries({ queryKey: ["topic-artifacts", selectedTopic?.topic_id] });
      onCreated(receipt.entity_id, selectedTopic!.topic_id);
    },
  });
  const loading = bases.isLoading || (!topic && topics.isLoading);
  const queryError = bases.error ?? topics.error ?? baseline.error;
  return (
    <Dialog open onOpenChange={(open) => { if (!open && !save.isPending) onClose(); }}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{tl({ zh: "正式规格验证", en: "Formal Strategy Validation" })}</DialogTitle>
          <DialogDescription>{tl({
            zh: "从已完成的研究运行冻结同一策略版本、发布和费用。预检通过后登记实验，后续在课题内单独确认一次性揭盲。",
            en: "Freeze the strategy version, releases, and fees from a completed run. Register after preflight, then separately confirm the one-time holdout in the topic.",
          })}</DialogDescription>
        </DialogHeader>
        <form className="space-y-4" onSubmit={(event) => {
          event.preventDefault();
          if (valid && ready && !save.error && !save.isPending && !preview.isPending) save.mutate();
        }}>
          {loading && <LoadingState rows={2} />}
          {queryError && <ErrorState message={queryError.message} onRetry={() => {
            if (bases.isError) void bases.refetch();
            if (topics.isError) void topics.refetch();
            if (baseline.isError) void baseline.refetch();
          }} />}
          {!topic && (
            <label className="grid gap-1 text-sm">
              {tl({ zh: "进行中的课题", en: "Active topic" })}
              <select className={selectStyle} value={topicId} onChange={(event) => setTopicId(event.target.value)}>
                <option value="">{tl({ zh: "选择课题", en: "Choose a topic" })}</option>
                {activeTopics.map((item) => <option value={item.topic_id} key={item.topic_id}>{item.title}</option>)}
              </select>
            </label>
          )}
          {!topic && topics.isSuccess && !activeTopics.length && (
            <Link className="text-sm text-primary underline" to="/research/topics">{tl({ zh: "先建题并激活", en: "Create and activate a topic first" })}</Link>
          )}
          <label className="grid gap-1 text-sm">
            {tl({ zh: "已完成的基线运行", en: "Completed baseline run" })}
            <select className={selectStyle} value={runId} onChange={(event) => setRunId(event.target.value)}>
              <option value="">{tl({ zh: "选择基线运行", en: "Choose a baseline run" })}</option>
              {bases.data?.map((item) => (
                <option value={item.run_id} key={item.run_id}>
                  {item.strategy_id} · v{item.strategy_version ?? String(item.manifest?.strategy_version ?? "—")} · {item.created_at.slice(0, 10)} · {item.run_id.slice(0, 12)}
                </option>
              ))}
            </select>
          </label>
          {bases.isSuccess && !bases.data.length && (
            <Link className="text-sm text-primary underline" to="/research/runs">{tl({ zh: "先完成基线运行", en: "Complete a baseline run first" })}</Link>
          )}
          <label className="grid gap-1 text-sm">
            {tl({ zh: "研究假设", en: "Research hypothesis" })}
            <Textarea value={hypothesis} onChange={(event) => setHypothesis(event.target.value)} rows={2} minLength={10} maxLength={2000} />
          </label>
          <fieldset className="grid gap-3 sm:grid-cols-2">
            <legend className="text-sm font-medium">{tl({ zh: "验证窗口", en: "Validation windows" })}</legend>
            {dateFields.map(({ key, label }) => (
              <label className="grid gap-1 text-sm" key={key}>
                {tl(label)}
                <Input type="date" value={dates[key] ?? ""} onChange={(event) => setDates({ ...dates, [key]: event.target.value })} />
              </label>
            ))}
          </fieldset>
          <details className="space-y-2 rounded-md border p-3">
            <summary className="cursor-pointer font-medium">{tl({ zh: "高级窗口设置", en: "Advanced window settings" })}</summary>
            <label className="grid gap-1 text-sm">
              {tl({ zh: "滚动方式", en: "Window mode" })}
              <select className={selectStyle} value={mode} onChange={(event) => setMode(event.target.value)}>
                <option value="rolling">{tl({ zh: "滚动", en: "Rolling" })}</option>
                <option value="expanding">{tl({ zh: "扩展", en: "Expanding" })}</option>
              </select>
            </label>
            <div className="grid gap-2 sm:grid-cols-3">
              {([{ value: trainDays, setter: setTrainDays, label: { zh: "训练窗口天数", en: "Training window days" } },
                { value: testDays, setter: setTestDays, label: { zh: "验证窗口天数", en: "Validation window days" } },
                { value: stepDays, setter: setStepDays, label: { zh: "滚动步长天数", en: "Step days" } }]).map((field) => (
                <label key={field.label.en} className="grid gap-1 text-sm">{tl(field.label)}
                  <Input type="number" min={1} value={field.value || ""} onChange={(event) => field.setter(Number(event.target.value))} />
                </label>
              ))}
            </div>
          </details>
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">{tl({ zh: "已查看的日期区间", en: "Previously reviewed windows" })}</legend>
            {usedWindows.map((window, index) => (
              <div key={index} className="grid items-end gap-2 sm:grid-cols-[1fr_1fr_auto]">
                {(["start", "end"] as const).map((key) => (
                  <label className="grid gap-1 text-sm" key={key}>
                    {key === "start" ? tl({ zh: "已用区间开始", en: "Used window start" }) : tl({ zh: "已用区间结束", en: "Used window end" })}
                    <Input type="date" value={window[key]} onChange={(event) => {
                      setUsedWindows(usedWindows.map((item, idx) => idx === index ? { ...item, [key]: event.target.value } : item));
                    }} />
                  </label>
                ))}
                <Button type="button" variant="outline" onClick={() => setUsedWindows(usedWindows.filter((_, itemIndex) => itemIndex !== index))}>
                  {tl({ zh: "移除区间", en: "Remove window" })}
                </Button>
              </div>
            ))}
            <Button type="button" variant="outline" onClick={() => setUsedWindows([...usedWindows, { start: "", end: "" }])}>
              {tl({ zh: "添加已用区间", en: "Add a used window" })}
            </Button>
            <label className="flex items-start gap-2 text-sm">
              <input type="checkbox" className="mt-1" checked={declaredWindows} onChange={(event) => setDeclaredWindows(event.target.checked)} />
              {tl({ zh: "已列出所有额外查看、调参或评估过的区间", en: "I listed every additional window used for inspection, tuning, or evaluation." })}
            </label>
          </fieldset>
          {ready && (
            <section className="space-y-2 rounded-md border p-3 text-sm" aria-label={tl({ zh: "生效预检", en: "Effective preflight" })}>
              <h3 className="font-medium">{tl({ zh: "生效预检", en: "Effective preflight" })}</h3>
              <p>v{ready.strategy_version} · {String(ready.plan.benchmark_symbol ?? "—")} · {String(ready.plan.test_start)} – {String(ready.plan.test_end)}</p>
              <ul className="space-y-1">{ready.dataset_releases.map((release) => (
                <li key={release.artifact_id}>
                  <Link className="break-all text-primary underline" to={`/data?release=${encodeURIComponent(release.artifact_id)}`}>{release.artifact_id}</Link>
                </li>
              ))}</ul>
              <p>{tl({ zh: "费用口径", en: "Fee policy" })} · {ready.fee_policy_version || tl({ zh: "历史资产规则", en: "Legacy asset rules" })}</p>
              <details className="rounded border p-2">
                <summary className="cursor-pointer">{tl({ zh: "门槛和覆盖参数", en: "Thresholds and overrides" })}</summary>
                <p className="break-words">{readableFields(ready.thresholds)}</p>
                <p className="break-words">{readableFields(record(ready.fee_config.overrides))}</p>
                <p className="break-words">{readableFields(record(ready.portfolio_config.overrides))}</p>
              </details>
            </section>
          )}
          {(preview.error || save.error) && (
            <div className="break-words text-sm text-destructive" role="alert">
              <p className="font-medium">{preview.error
                ? tl({ zh: "正式验证预检失败", en: "Formal validation preflight failed" })
                : tl({ zh: "实验登记失败，请重新预检", en: "Could not register the experiment. Preview it again." })}</p>
              <p>{(preview.error ?? save.error)?.message}</p>
            </div>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={save.isPending}>{tl({ zh: "取消", en: "Cancel" })}</Button>
            <Button type="button" variant="outline" disabled={!valid || preview.isPending || save.isPending} onClick={() => {
              save.reset();
              preview.mutate({ activeTopic: selectedTopic!.topic_id, goalVersion: goal!, payload: requestBody, requestKey: currentPreviewInput });
            }}>
              {preview.isPending ? tl({ zh: "预检中", en: "Checking" }) : tl({ zh: "预检生效输入", en: "Preview Effective Inputs" })}
            </Button>
            <Button type="submit" disabled={!valid || !ready || preview.isPending || save.isPending || Boolean(save.error)}>
              {save.isPending ? tl({ zh: "登记中", en: "Registering" }) : tl({ zh: "登记实验", en: "Register Experiment" })}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
