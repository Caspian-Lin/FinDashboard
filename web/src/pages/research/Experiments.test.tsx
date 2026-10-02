import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import Experiments from "./Experiments";
import { experimentApi, type ValidationExperimentDetail } from "@/lib/research";

vi.mock("@/lib/research", async () => {
  const actual = await vi.importActual<typeof import("@/lib/research")>("@/lib/research");
  return { ...actual, experimentApi: { list: vi.fn(), get: vi.fn(), reject: vi.fn(), delete: vi.fn() } };
});
beforeEach(() => vi.resetAllMocks());

describe("OOS 流程与结论分开呈现", () => {
  it.each([
    ["not_supported", "OOS：假设未获支持"],
    ["inconclusive", "OOS：证据不足"],
    [undefined, "OOS：结论未知"],
  ] as const)("列表和精确实验详情保留 %s", async (outcome, label) => {
    const experiment: ValidationExperimentDetail = {
      experiment_id: "VE-frozen",
      hypothesis: "冻结的小盘反转假设",
      status: "validated_oos",
      oos_outcome: outcome,
      version_stamp: { code_commit: "frozen-commit" },
      plan: { train_start: "2015-01-01", train_end: "2020-12-31", test_start: "2021-01-01", test_end: "2025-12-31" },
      thresholds: { min_annual_return: 0.15, max_drawdown: 0.10 },
      robustness: {},
      strategy_params_space: {},
      created_at: "2026-10-03T00:00:00Z",
      trials: [],
    };
    vi.mocked(experimentApi.list).mockResolvedValue([experiment]);
    vi.mocked(experimentApi.get).mockResolvedValue(experiment);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/research/experiments?experiment=VE-frozen"]}>
          <Experiments />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getAllByText(label)).toHaveLength(2));
    expect(screen.getAllByText("OOS 流程完成")).toHaveLength(2);
    expect(screen.queryByText("策略验证通过")).not.toBeInTheDocument();
    expect(experimentApi.get).toHaveBeenCalledWith("VE-frozen");
    fireEvent.click(screen.getByRole("button", { name: "取消选择" }));
    await waitFor(() => expect(screen.getAllByText(label)).toHaveLength(1));
  });
});
