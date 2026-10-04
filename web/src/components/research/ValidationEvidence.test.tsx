import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { ValidationEvidence } from "./ValidationEvidence";

describe("压力计划与执行状态", () => {
  it("不把旧passed、未执行或unsupported显示为通过", () => {
    render(<MemoryRouter><ValidationEvidence trials={[{
      trial_id: "trial-frozen", parameters: {}, status: "passed", created_at: "2026-10-04",
      robustness_probes: ["planned", "unsupported", "failed", undefined, "completed"].map((status, i) => ({
        probe_kind: "cost", label: `probe-${i}`, passed: true, total_return: .10, max_drawdown: .20, detail: { status },
      })),
    }]} /></MemoryRouter>);
    expect(screen.getByText("计划 · 尚未执行")).toBeInTheDocument();
    expect(screen.getByText("能力不支持")).toBeInTheDocument();
    expect(screen.getByText("执行失败")).toBeInTheDocument();
    expect(screen.getByText("历史记录 · 执行未核验")).toBeInTheDocument();
    expect(screen.getAllByText("已执行 · 门槛通过")).toHaveLength(1);
    expect(screen.getAllByText("10.00% / 20.00%")).toHaveLength(1);
  });
});
