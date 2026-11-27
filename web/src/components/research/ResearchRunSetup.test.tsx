import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import ResearchRunSetup from "./ResearchRunSetup";
import { ApiError } from "@/lib/api";
import { researchRunApi, strategySpecApi } from "@/lib/research";

vi.mock("@/lib/research", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/research")>();
  return {
    ...original,
    researchRunApi: { ...original.researchRunApi, preview: vi.fn(), factorSeriesOptions: vi.fn() },
    strategySpecApi: { ...original.strategySpecApi, list: vi.fn() },
  };
});

const strategy = {
  strategy_id: "published_fixture",
  version: 3,
  published: true,
  status: "published",
  name: "季度组合",
  checksum: "spec",
  created_at: "2026-11-01T00:00:00Z",
  spec: {
    name: "季度组合",
    feature_graph: { nodes: [] },
    validation_plan: { dataset_release_ids: ["DR-bars"], test_start: "2025-01-01", test_end: "2025-12-30", benchmark_symbol: "000300.SH" },
    portfolio_policy: { max_risk_contribution: 0.35 },
    execution_model: { commission_rate: 0.0003 },
  },
};
const receipt = {
  run_id: "RR-fixture",
  manifest_checksum: "a".repeat(64),
  manifest: {
    strategy_spec: strategy.spec,
    strategy_version: 3,
    benchmark_config: { symbol: "000300.SH" },
  },
};

function renderForm(start = vi.fn(), error?: Error) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ResearchRunSetup onStart={start} onCancel={vi.fn()} pending={false} error={error} onRepreview={vi.fn()} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return start;
}

afterEach(() => vi.clearAllMocks());

describe("研究运行配置及预检", () => {
  it("选择发布版本后先预检，确认后使用同一冻结摘要排队", async () => {
    const user = userEvent.setup();
    vi.mocked(strategySpecApi.list).mockResolvedValue([strategy]);
    vi.mocked(researchRunApi.preview).mockResolvedValue(receipt);
    const start = renderForm();
    const submit = screen.getByRole("button", { name: "启动研究运行" });
    expect(submit).toBeDisabled();
    await screen.findByRole("option", { name: "季度组合 · v3" });
    await user.selectOptions(screen.getByLabelText("已发布策略版本"), "published_fixture@3");
    await user.click(screen.getByRole("button", { name: "预览生效输入" }));
    expect(await screen.findByText("预检通过")).toBeInTheDocument();
    expect(researchRunApi.preview).toHaveBeenCalledWith(expect.objectContaining({
      strategy_id: "published_fixture",
      strategy_version: 3,
      dataset_release_ids: ["DR-bars"],
    }), expect.anything());
    expect(start).not.toHaveBeenCalled();
    await user.click(submit);
    expect(start).toHaveBeenCalledWith(expect.objectContaining({
      strategy_version: 3,
      expected_manifest_checksum: receipt.manifest_checksum,
    }));
    await user.clear(screen.getByLabelText("初始资金（元）"));
    await user.type(screen.getByLabelText("初始资金（元）"), "200000");
    expect(submit).toBeDisabled();
    expect(screen.queryByText("预检通过")).not.toBeInTheDocument();
  });

  it("命名的预检拒绝保留可重试入口并禁止启动", async () => {
    const user = userEvent.setup();
    vi.mocked(strategySpecApi.list).mockResolvedValue([strategy]);
    vi.mocked(researchRunApi.preview).mockRejectedValue(new ApiError(422, {}, "数据发布不可用"));
    const start = renderForm();
    await screen.findByRole("option", { name: "季度组合 · v3" });
    await user.selectOptions(screen.getByLabelText("已发布策略版本"), "published_fixture@3");
    const preview = screen.getByRole("button", { name: "预览生效输入" });
    await user.click(preview);
    expect(await screen.findByRole("alert")).toHaveTextContent("数据发布不可用");
    expect(preview).toBeEnabled();
    expect(screen.getByRole("button", { name: "启动研究运行" })).toBeDisabled();
    expect(start).not.toHaveBeenCalled();
  });
});