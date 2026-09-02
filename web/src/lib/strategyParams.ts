import {
  ApiError,
  type StrategyInfo,
  type StrategyParamValue,
} from "./api";

export type StrategyParams = Record<string, StrategyParamValue>;
export type StrategyFieldErrors = Record<string, string>;

export function defaultStrategyParams(definition: StrategyInfo): StrategyParams {
  return Object.fromEntries(
    definition.params.map((param) => [
      param.name,
      param.default ?? (param.nullable ? null : ""),
    ]),
  );
}

export function normalizeStrategyParams(
  definition: StrategyInfo,
  values: StrategyParams,
): StrategyParams {
  return Object.fromEntries(
    definition.params.map((param) => {
      const value = values[param.name];
      if (value === "" && param.nullable) return [param.name, null];
      if (value === "" || value === null || param.type === "boolean") {
        return [param.name, value];
      }
      if (param.type === "integer") return [param.name, Number.parseInt(String(value), 10)];
      if (param.type === "number") return [param.name, Number(value)];
      return [param.name, value];
    }),
  );
}

export function validateStrategyParams(
  definition: StrategyInfo,
  values: StrategyParams,
  lang: "zh" | "en" = "zh",
): StrategyFieldErrors {
  const errors: StrategyFieldErrors = {};
  const msg = {
    required: lang === "en" ? "This field is required" : "此项为必填",
    invalidNumber: lang === "en" ? "Enter a valid number" : "请输入有效数字",
    notInteger: lang === "en" ? "Enter an integer" : "请输入整数",
    min: lang === "en" ? `Cannot be less than {v}` : `不能小于 {v}`,
    max: lang === "en" ? `Cannot be greater than {v}` : `不能大于 {v}`,
    exclusiveMin: lang === "en" ? `Must be greater than {v}` : `必须大于 {v}`,
    exclusiveMax: lang === "en" ? `Must be less than {v}` : `必须小于 {v}`,
    minLength: lang === "en" ? `Enter at least {v} characters` : `至少输入 {v} 个字符`,
    maxLength: lang === "en" ? `Enter at most {v} characters` : `最多输入 {v} 个字符`,
  };
  const fmt = (tpl: string, v: string | number) => tpl.replace("{v}", String(v));

  for (const param of definition.params) {
    if (param.ui_hidden) continue;
    const value = values[param.name];
    const empty = value === "" || value === null || value === undefined;
    if (param.required && empty) {
      errors[param.name] = msg.required;
      continue;
    }
    if (empty) continue;

    if (param.type === "integer" || param.type === "number") {
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) {
        errors[param.name] = msg.invalidNumber;
        continue;
      }
      if (param.type === "integer" && !Number.isInteger(numeric)) {
        errors[param.name] = msg.notInteger;
      } else if (param.minimum !== null && numeric < param.minimum) {
        errors[param.name] = fmt(msg.min, param.minimum);
      } else if (param.maximum !== null && numeric > param.maximum) {
        errors[param.name] = fmt(msg.max, param.maximum);
      } else if (
        param.exclusive_minimum !== null &&
        numeric <= param.exclusive_minimum
      ) {
        errors[param.name] = fmt(msg.exclusiveMin, param.exclusive_minimum);
      } else if (
        param.exclusive_maximum !== null &&
        numeric >= param.exclusive_maximum
      ) {
        errors[param.name] = fmt(msg.exclusiveMax, param.exclusive_maximum);
      }
    }

    if (typeof value === "string") {
      if (param.min_length !== null && value.length < param.min_length) {
        errors[param.name] = fmt(msg.minLength, param.min_length);
      } else if (param.max_length !== null && value.length > param.max_length) {
        errors[param.name] = fmt(msg.maxLength, param.max_length);
      }
    }
  }

  return errors;
}

export function strategyFieldErrorsFrom(error: unknown): StrategyFieldErrors {
  if (!(error instanceof ApiError) || !Array.isArray(error.detail)) return {};
  const result: StrategyFieldErrors = {};
  for (const item of error.detail) {
    if (typeof item !== "object" || item === null || !("loc" in item) || !("msg" in item)) {
      continue;
    }
    const loc = Array.isArray(item.loc) ? item.loc.map(String) : [];
    const paramsIndex = loc.indexOf("params");
    const field = paramsIndex >= 0 ? loc[paramsIndex + 1] : undefined;
    if (field) result[field] = String(item.msg);
  }
  return result;
}
