import { describe, expect, it } from "vitest";
import { oosLabel, runSchedule } from "./research-semantics";
describe("research execution and hypothesis semantics", () => {
  it("reads a real custom manifest during execution and summarizes its calendar", () => {
    const s = runSchedule({
      manifest: {
        parameters: { decision_schedule: { kind: "custom", dates: ["2015-02-06", "2026-08-14"] } },
      },
      result: null,
    });
    expect(s).toMatchObject({
      mode: "multi_period",
      count: 2,
      first: "2015-02-06",
      last: "2026-08-14",
      frequency: "custom",
    });
  });
  it.each(["daily", "weekly", "monthly", "quarterly"])(
    "supports %s and its legacy shape",
    (kind) => {
      expect(runSchedule({ manifest: { parameters: { decision_schedule: { kind } } } }).mode).toBe(
        "multi_period",
      );
      expect(runSchedule({ manifest: { parameters: { rebalance_frequency: kind } } }).mode).toBe(
        "multi_period",
      );
    },
  );
  it("prefers reported mode but exposes conflicting declarations", () => {
    const manifest = {
      parameters: { decision_schedule: { kind: "weekly" }, rebalance_frequency: "monthly" },
    };
    expect(runSchedule({ manifest }).mode).toBe("unknown");
    expect(runSchedule({ manifest, result: { execution_mode: "multi_period" } })).toMatchObject({
      mode: "multi_period",
      conflict: true,
    });
  });
  it("keeps unknown/invalid legacy inputs unknown", () => {
    expect(runSchedule({ manifest: {} }).mode).toBe("unknown");
    expect(
      runSchedule({
        manifest: { parameters: { decision_schedule: { kind: "weekly", dates: ["2026-01-01"] } } },
      }).mode,
    ).toBe("unknown");
    expect(
      runSchedule({
        manifest: { parameters: { decision_schedule: { kind: "weekly", unknown: true } } },
      }).mode,
    ).toBe("unknown");
    expect(
      runSchedule({
        manifest: { parameters: { decision_schedule: { kind: "custom", dates: ["2026-99-99"] } } },
      }).mode,
    ).toBe("unknown");
    expect(
      runSchedule({ manifest: { parameters: { decision_schedule: { kind: "hourly" } } } }).mode,
    ).toBe("unknown");
  });
  it("does not mistake OOS completion or missing fields for support", () => {
    expect(oosLabel("not_supported")).toContain("未获支持");
    expect(oosLabel("inconclusive")).toContain("证据不足");
    expect(oosLabel(undefined)).toContain("未知");
  });
});
