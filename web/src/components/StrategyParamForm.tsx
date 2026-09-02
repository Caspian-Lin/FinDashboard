import {
  type StrategyInfo,
  type StrategyParamInfo,
} from "../lib/api";
import type { InfoHintDefinition } from "../lib/infoHints";
import type {
  StrategyFieldErrors,
  StrategyParams,
} from "../lib/strategyParams";
import { HintLabel } from "./InfoHint";
import { useT, type Language } from "@/i18n";

function inputBounds(param: StrategyParamInfo) {
  return {
    min: param.minimum ?? param.exclusive_minimum ?? undefined,
    max: param.maximum ?? param.exclusive_maximum ?? undefined,
  };
}

function paramHint(param: StrategyParamInfo, lang: Language): InfoHintDefinition {
  const zh = lang === "zh";
  const constraints: string[] = [];
  if (param.enum) constraints.push(zh ? `可选值：${param.enum.join("、")}` : `Allowed: ${param.enum.join(", ")}`);
  if (param.minimum !== null) constraints.push(zh ? `最小值 ${param.minimum}` : `Minimum ${param.minimum}`);
  if (param.exclusive_minimum !== null) {
    constraints.push(zh ? `必须大于 ${param.exclusive_minimum}` : `Must be greater than ${param.exclusive_minimum}`);
  }
  if (param.maximum !== null) constraints.push(zh ? `最大值 ${param.maximum}` : `Maximum ${param.maximum}`);
  if (param.exclusive_maximum !== null) {
    constraints.push(zh ? `必须小于 ${param.exclusive_maximum}` : `Must be less than ${param.exclusive_maximum}`);
  }
  if (param.min_length !== null) constraints.push(zh ? `至少 ${param.min_length} 个字符` : `At least ${param.min_length} characters`);
  if (param.max_length !== null) constraints.push(zh ? `最多 ${param.max_length} 个字符` : `At most ${param.max_length} characters`);
  constraints.push(param.required ? (zh ? "必填" : "Required") : zh ? "可选" : "Optional");

  return {
    title: { zh: param.label, en: param.label },
    description: {
      zh: param.description || "该字段由策略参数 schema 定义并在后端校验。",
      en: param.description || "This field is defined by the strategy parameter schema and validated on the backend.",
    },
    detail: { zh: constraints.join("；"), en: constraints.join("; ") },
  };
}

export default function StrategyParamForm({
  definition,
  values,
  onChange,
  errors = {},
  disabled = false,
}: {
  definition: StrategyInfo;
  values: StrategyParams;
  onChange: (values: StrategyParams) => void;
  errors?: StrategyFieldErrors;
  disabled?: boolean;
}) {
  const { t, lang } = useT();
  const visibleParams = definition.params.filter((param) => !param.ui_hidden);

  if (visibleParams.length === 0) {
    return (
      <div className="rounded-lg bg-muted/50 px-4 py-3 text-sm text-muted-foreground">
        {t("strategyParamForm.noParams")}
      </div>
    );
  }

  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {visibleParams.map((param) => {
        const id = `strategy-param-${param.name}`;
        const error = errors[param.name];
        const commonClass = `w-full rounded-md border bg-transparent px-3 py-2 text-sm text-foreground outline-none transition-colors focus:ring-2 focus:ring-primary/30 disabled:bg-secondary ${
          error ? "border-destructive focus:border-destructive" : "border-border focus:border-ring"
        }`;

        return (
          <div key={param.name}>
            <HintLabel
              htmlFor={id}
              hint={paramHint(param, lang)}
              labelClassName="text-sm font-medium text-foreground"
            >
              {param.label}
              {param.required && <span className="ml-1 text-destructive" aria-hidden="true">*</span>}
            </HintLabel>

            {param.enum ? (
              <select
                id={id}
                value={String(values[param.name] ?? "")}
                onChange={(event) =>
                  onChange({ ...values, [param.name]: event.target.value })
                }
                disabled={disabled}
                aria-invalid={Boolean(error)}
                aria-describedby={`${id}-description`}
                className={commonClass}
              >
                {param.nullable && <option value="">{t("strategyParamForm.leaveEmpty")}</option>}
                {param.enum.map((option) => (
                  <option key={String(option)} value={String(option)}>
                    {String(option)}
                  </option>
                ))}
              </select>
            ) : param.type === "boolean" ? (
              <label className="flex min-h-10 items-center gap-2 text-sm text-foreground">
                <input
                  id={id}
                  type="checkbox"
                  checked={Boolean(values[param.name])}
                  onChange={(event) =>
                    onChange({ ...values, [param.name]: event.target.checked })
                  }
                  disabled={disabled}
                  className="size-4 rounded border-border text-primary focus:ring-ring"
                />
                {t("strategyParamForm.enable")}
              </label>
            ) : (
              <input
                id={id}
                type={param.type === "integer" || param.type === "number" ? "number" : "text"}
                step={param.type === "integer" ? 1 : "any"}
                {...inputBounds(param)}
                value={String(values[param.name] ?? "")}
                onChange={(event) =>
                  onChange({ ...values, [param.name]: event.target.value })
                }
                disabled={disabled}
                required={param.required}
                aria-invalid={Boolean(error)}
                aria-describedby={`${id}-description`}
                className={commonClass}
              />
            )}

            <p
              id={`${id}-description`}
              className={`mt-1 text-xs ${error ? "text-destructive" : "text-muted-foreground"}`}
            >
              {error ?? param.description}
            </p>
          </div>
        );
      })}
    </div>
  );
}
