import {
  type StrategyInfo,
  type StrategyParamInfo,
} from "../lib/api";
import type {
  StrategyFieldErrors,
  StrategyParams,
} from "../lib/strategyParams";

function inputBounds(param: StrategyParamInfo) {
  return {
    min: param.minimum ?? param.exclusive_minimum ?? undefined,
    max: param.maximum ?? param.exclusive_maximum ?? undefined,
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
  const visibleParams = definition.params.filter((param) => !param.ui_hidden);

  if (visibleParams.length === 0) {
    return (
      <div className="rounded-lg bg-slate-50 px-4 py-3 text-sm text-slate-600">
        此策略没有可配置参数。
      </div>
    );
  }

  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {visibleParams.map((param) => {
        const id = `strategy-param-${param.name}`;
        const error = errors[param.name];
        const commonClass = `w-full rounded-md border bg-white px-3 py-2 text-sm text-slate-900 outline-none transition-colors focus:ring-2 focus:ring-blue-200 disabled:bg-slate-100 ${
          error ? "border-red-400 focus:border-red-500" : "border-slate-300 focus:border-blue-500"
        }`;

        return (
          <div key={param.name}>
            <label htmlFor={id} className="mb-1 block text-sm font-medium text-slate-700">
              {param.label}
              {param.required && <span className="ml-1 text-red-600" aria-hidden="true">*</span>}
            </label>

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
                {param.nullable && <option value="">留空</option>}
                {param.enum.map((option) => (
                  <option key={String(option)} value={String(option)}>
                    {String(option)}
                  </option>
                ))}
              </select>
            ) : param.type === "boolean" ? (
              <label className="flex min-h-10 items-center gap-2 text-sm text-slate-700">
                <input
                  id={id}
                  type="checkbox"
                  checked={Boolean(values[param.name])}
                  onChange={(event) =>
                    onChange({ ...values, [param.name]: event.target.checked })
                  }
                  disabled={disabled}
                  className="size-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                />
                启用
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
              className={`mt-1 text-xs ${error ? "text-red-600" : "text-slate-500"}`}
            >
              {error ?? param.description}
            </p>
          </div>
        );
      })}
    </div>
  );
}
