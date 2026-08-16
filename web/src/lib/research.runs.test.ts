import { describe, expect, it, vi, beforeEach } from "vitest";
import { researchRunApi } from "./research";

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
