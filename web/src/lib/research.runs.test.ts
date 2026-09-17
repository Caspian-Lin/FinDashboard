import { describe, expect, it, vi, beforeEach } from "vitest";
import { researchRunApi, runningRunLabel } from "./research";
import type { ResearchRunStatus } from "./research";

/* #157:researchRunApi.list 必须把 status 多值参数拼进请求(服务端过滤),
   否则报告页只能在前端对最多 limit 条数据过滤。 */

const fetchJSON = vi.hoisted(() => vi.fn());
vi.mock("./api", () => ({
  fetchJSON: (...a: unknown[]) => fetchJSON(...a),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe("researchRunApi.list", () => {
  it("把 status 数组拼接为重复查询参数", async () => {
    fetchJSON.mockResolvedValue([]);
    await researchRunApi.list({ status: ["completed"], limit: 100 });
    expect(fetchJSON).toHaveBeenCalledWith(
      "/research/runs?status=completed&limit=100",
    );
  });

  it("多个 status 逐个 append", async () => {
    fetchJSON.mockResolvedValue([]);
    await researchRunApi.list({ status: ["queued", "running"] });
    expect(fetchJSON).toHaveBeenCalledWith(
      "/research/runs?status=queued&status=running",
    );
  });

  it("不传参数时无查询串", async () => {
    fetchJSON.mockResolvedValue([]);
    await researchRunApi.list();
    expect(fetchJSON).toHaveBeenCalledWith("/research/runs");
  });
});

/* #484:运行列表「活着的证据」文案——now 显式注入,不依赖当前墙钟。 */
describe("runningRunLabel", () => {
  const now = new Date("2026-09-18T12:00:00+08:00").getTime();
  const startedAt = (offsetMinutes: number) =>
    new Date(now - offsetMinutes * 60_000).toISOString();
  const running = (startedAtValue: string | undefined) => ({
    status: "running" as ResearchRunStatus,
    started_at: startedAtValue,
  });

  it("非 running 状态一律返回 null", () => {
    for (const status of [
      "queued",
      "completed",
      "failed",
      "cancelled",
      "interrupted",
      "rejected",
    ] as ResearchRunStatus[]) {
      expect(runningRunLabel({ status, started_at: startedAt(120) }, now)).toBeNull();
    }
  });

  it("running 且无 started_at → 排队中提示(不含时长)", () => {
    expect(runningRunLabel(running(undefined), now)).toEqual({
      zh: "排队中(尚未启动)",
      en: "Queued (not started)",
    });
  });

  it("不足 1 分 → 不到 1 分;未来/负偏差按 0 收敛", () => {
    expect(runningRunLabel(running(startedAt(0)), now)).toEqual({
      zh: "已运行 不到 1 分",
      en: "Running <1m",
    });
    expect(runningRunLabel(running(startedAt(-30)), now)).toEqual({
      zh: "已运行 不到 1 分",
      en: "Running <1m",
    });
  });

  it("1 分 ~ 1 小时 → 只显示分钟(向下取整)", () => {
    expect(runningRunLabel(running(startedAt(1)), now)).toEqual({
      zh: "已运行 1 分",
      en: "Running 1m",
    });
    expect(runningRunLabel(running(startedAt(59)), now)).toEqual({
      zh: "已运行 59 分",
      en: "Running 59m",
    });
  });

  it("1 小时以上 → 小时 + 分", () => {
    expect(runningRunLabel(running(startedAt(60)), now)).toEqual({
      zh: "已运行 1 小时 0 分",
      en: "Running 1h 0m",
    });
    expect(runningRunLabel(running(startedAt(134)), now)).toEqual({
      zh: "已运行 2 小时 14 分",
      en: "Running 2h 14m",
    });
  });

  it("跨天 → 天 + 小时", () => {
    expect(runningRunLabel(running(startedAt(24 * 60)), now)).toEqual({
      zh: "已运行 1 天 0 小时",
      en: "Running 1d 0h",
    });
    expect(runningRunLabel(running(startedAt(3 * 24 * 60 + 5 * 60)), now)).toEqual({
      zh: "已运行 3 天 5 小时",
      en: "Running 3d 5h",
    });
  });

  it("started_at 非法 → null(不渲染该行)", () => {
    expect(runningRunLabel(running("not-a-date"), now)).toBeNull();
  });
});
