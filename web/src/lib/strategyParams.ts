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
): StrategyFieldErrors {
  const errors: StrategyFieldErrors = {};

  for (const param of definition.params) {
    if (param.ui_hidden) continue;
    const value = values[param.name];
    const empty = value === "" || value === null || value === undefined;
    if (param.required && empty) {
      errors[param.name] = "此项为必填";
      continue;
    }
    if (empty) continue;

    if (param.type === "integer" || param.type === "number") {
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) {
        errors[param.name] = "请输入有效数字";
        continue;
      }
      if (param.type === "integer" && !Number.isInteger(numeric)) {
        errors[param.name] = "请输入整数";
      } else if (param.minimum !== null && numeric < param.minimum) {
        errors[param.name] = `不能小于 ${param.minimum}`;
      } else if (param.maximum !== null && numeric > param.maximum) {
        errors[param.name] = `不能大于 ${param.maximum}`;
      } else if (
        param.exclusive_minimum !== null &&
        numeric <= param.exclusive_minimum
      ) {
        errors[param.name] = `必须大于 ${param.exclusive_minimum}`;
      } else if (
        param.exclusive_maximum !== null &&
        numeric >= param.exclusive_maximum
      ) {
        errors[param.name] = `必须小于 ${param.exclusive_maximum}`;
      }
    }

    if (typeof value === "string") {
      if (param.min_length !== null && value.length < param.min_length) {
        errors[param.name] = `至少输入 ${param.min_length} 个字符`;
      } else if (param.max_length !== null && value.length > param.max_length) {
        errors[param.name] = `最多输入 ${param.max_length} 个字符`;
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
