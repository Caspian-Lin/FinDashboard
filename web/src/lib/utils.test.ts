import { describe, expect, it } from "vitest";

import { formatPercent } from "./utils";

/**
 * issue #349:coverage_pct 曾以字符串("1")下发导致前端显示误导,
 * formatPercent 需兼容 number / string 输入,非法输入显示占位符。
 */
describe("formatPercent", () => {
  it("格式化数值输入", () => {
    expect(formatPercent(1, 1)).toBe("+100.0%");
    expect(formatPercent(0.9833, 1)).toBe("+98.3%");
    expect(formatPercent(0)).toBe("0.00%");
    expect(formatPercent(-0.05, 2)).toBe("-5.00%");
  });

  it("归一字符串输入(issue #349)", () => {
    expect(formatPercent("1", 1)).toBe("+100.0%");
    expect(formatPercent("0.9833", 1)).toBe("+98.3%");
    expect(formatPercent(" 0.5 ", 0)).toBe("+50%");
  });

  it("非法与缺失输入显示占位符", () => {
    expect(formatPercent(null)).toBe("—");
    expect(formatPercent(undefined)).toBe("—");
    expect(formatPercent(NaN)).toBe("—");
    expect(formatPercent("")).toBe("—");
    expect(formatPercent("   ")).toBe("—");
    expect(formatPercent("abc")).toBe("—");
  });
});
