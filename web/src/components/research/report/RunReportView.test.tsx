import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RunResultSummary, runVersion, shortDate } from "./RunReportView";
import type { ResearchRunSummary } from "@/lib/research";

/* issue #484:运行列表 API 把版本冻结在 manifest.strategy_version(顶层
   strategy_version 恒为 null),此前只读 manifest.strategy_spec_version 导致
   报告页与报告选择器一律显示 v—。 */

function makeRun(overrides: Partial<ResearchRunSummary>): ResearchRunSummary {
  return {
    run_id: "RR-a2f0ad6f035d7eada1538227",
    strategy_id: "sc-mr-composite-v1",
    status: "completed",
    strategy_kind: "multi_factor",
    requested_by: "human",
    created_at: "2026-09-17T16:29:19.519965+08:00",
    ...overrides,
  };
}

describe("runVersion", () => {
  it("顶层 strategy_version 优先", () => {
    const run = makeRun({
      strategy_version: 7,
      manifest: { strategy_version: 1, strategy_spec_version: 3 },
    });
    expect(runVersion(run)).toBe(7);
  });

  it("顶层缺失时读 manifest.strategy_version(真实载荷形态)", () => {
    const run = makeRun({
      strategy_version: null as unknown as undefined,
      manifest: { strategy_version: 1 },
    });
    expect(runVersion(run)).toBe(1);
  });

  it("manifest.strategy_version 缺失时回退 strategy_spec_version", () => {
    const run = makeRun({ manifest: { strategy_spec_version: 3 } });
    expect(runVersion(run)).toBe(3);
  });

  it("四处均缺失返回 null", () => {
    expect(runVersion(makeRun({}))).toBeNull();
    expect(runVersion(makeRun({ manifest: {} }))).toBeNull();
  });
});

describe("shortDate", () => {
  it("格式化为月/日,非法输入原样返回", () => {
    expect(shortDate("2026-09-17T16:29:19+08:00")).toBe("09/17");
    expect(shortDate("not-a-date")).toBe("not-a-date");
  });
});

describe("Run result evidence", () => {
  it("keeps an absent benchmark distinct from a zero return", () => {
    const run = makeRun({ result: { strategy_return: 0, benchmark_return: null } });
    render(<RunResultSummary detail={{ ...run, manifest: {} }} />);

    const summary = within(screen.getByRole("region", { name: "结果概览" }));
    expect(summary.getByText("0.00%")).toBeInTheDocument();
    expect(summary.getByText("缺少基准证据，暂不可比")).toBeInTheDocument();
    expect(summary.getByText(/缺少部分结果证据/)).toBeInTheDocument();
  });

  it("shows the frozen fee policy, benchmark and actual rejection details", () => {
    const run = makeRun({
      status: "interrupted",
      error_summary: "归档中的工件缺少成交快照",
      result: { strategy_return: 0.03, benchmark_return: 0.02, excess_return: 0.01, commission_paid: "10.20" },
    });
    render(<RunResultSummary detail={{ ...run, manifest: {
      parameters: {
        fee_policy_version: "explicit_overrides_v1",
        research_window: { decision_start: "2024-01-02", decision_end: "2024-02-23" },
      },
      benchmark_config: { symbol: "000001.SH" },
    } }} />);

    expect(screen.getByText("显式费用覆盖")).toBeInTheDocument();
    expect(screen.getByText(/000001.SH.*2.00%/)).toBeInTheDocument();
    expect(screen.getByText("+1.00%")).toBeInTheDocument();
    expect(screen.getByText("2024-01-02 – 2024-02-23")).toBeInTheDocument();
    expect(screen.getByText("归档中的工件缺少成交快照")).toBeInTheDocument();
  });
});
