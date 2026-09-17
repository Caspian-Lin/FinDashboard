import { afterEach, describe, expect, it, vi } from "vitest";
import { formatJobElapsed, parseJobPhase } from "./jobs";

describe("parseJobPhase(#308 两形态回归)", () => {
  it("加载期帧:research_run:decision_load k/N", () => {
    expect(parseJobPhase("research_run:decision_load 4/36")).toEqual({
      stage: "decision_load",
      load: { done: 4, total: 36 },
    });
  });

  it("决策级帧:research_run:<stage>#n@日期", () => {
    expect(parseJobPhase("research_run:signals#2@2024-01-03")).toEqual({
      stage: "signals",
      decision: { index: 2, date: "2024-01-03" },
    });
  });

  it("纯 stage 帧原样透出;非 research_run 格式与空值返回 null", () => {
    expect(parseJobPhase("research_run:report")).toEqual({ stage: "report" });
    expect(parseJobPhase("fetch")).toBeNull();
    expect(parseJobPhase(null)).toBeNull();
    expect(parseJobPhase("")).toBeNull();
  });
});

describe("formatJobElapsed(issue #442)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("elapsed 计算随系统时间推进(vi.useFakeTimers + setSystemTime)", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-11T04:00:00Z"));
    expect(formatJobElapsed("2026-09-11T03:59:00Z")).toBe("1 分 0 秒");
    vi.setSystemTime(new Date("2026-09-11T04:02:05Z"));
    expect(formatJobElapsed("2026-09-11T03:59:00Z")).toBe("3 分 5 秒");
    vi.setSystemTime(new Date("2026-09-11T05:03:06Z"));
    expect(formatJobElapsed("2026-09-11T03:59:00Z")).toBe("1 小时 4 分 6 秒");
  });

  it("endMs 显式传参:终态时长定格于 finished_at,不随墙钟增长", () => {
    const end = new Date("2026-09-11T04:00:30Z").getTime();
    expect(formatJobElapsed("2026-09-11T04:00:00Z", "zh", end)).toBe("30 秒");
    expect(formatJobElapsed("2026-09-11T04:00:00Z", "en", end)).toBe("30s");
    // 显式 endMs 之后再推系统时间,结果不变。
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-11T08:00:00Z"));
    expect(formatJobElapsed("2026-09-11T04:00:00Z", "zh", end)).toBe("30 秒");
  });

  it("跨语言:zh「X 小时 Y 分 Z 秒」/ en「Xh Ym Zs」,不足一小时省略小时段", () => {
    const end = new Date("2026-09-11T06:02:03Z").getTime();
    expect(formatJobElapsed("2026-09-11T04:00:00Z", "zh", end)).toBe("2 小时 2 分 3 秒");
    expect(formatJobElapsed("2026-09-11T06:00:00Z", "zh", end)).toBe("2 分 3 秒");
    expect(formatJobElapsed("2026-09-11T06:02:00Z", "zh", end)).toBe("3 秒");
    expect(formatJobElapsed("2026-09-11T04:00:00Z", "en", end)).toBe("2h 2m 3s");
    expect(formatJobElapsed("2026-09-11T06:00:00Z", "en", end)).toBe("2m 3s");
    expect(formatJobElapsed("2026-09-11T06:02:00Z", "en", end)).toBe("3s");
  });

  it("started_at 晚于 endMs(时钟回拨)钳制为 0", () => {
    const end = new Date("2026-09-11T04:00:00Z").getTime();
    expect(formatJobElapsed("2026-09-11T05:00:00Z", "zh", end)).toBe("0 秒");
    expect(formatJobElapsed("2026-09-11T05:00:00Z", "en", end)).toBe("0s");
  });

  it("缺失 / 非法 started_at 返回 null(组件侧显示 —)", () => {
    expect(formatJobElapsed(null)).toBeNull();
    expect(formatJobElapsed(undefined)).toBeNull();
    expect(formatJobElapsed("")).toBeNull();
    expect(formatJobElapsed("not-a-date")).toBeNull();
  });
});
