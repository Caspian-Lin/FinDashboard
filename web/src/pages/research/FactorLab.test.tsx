import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { beforeEach, describe, expect, it, vi } from "vitest";
import FactorLab from "./FactorLab";

const factorLabApiMock = vi.hoisted(() => ({
  catalog: vi.fn(),
  predefined: vi.fn(),
  createFeatureSnapshot: vi.fn(),
  startFeatureSnapshotJob: vi.fn(),
  features: vi.fn(),
  featureDetail: vi.fn(),
  signals: vi.fn(),
  signalDetail: vi.fn(),
  createFactorExperiment: vi.fn(),
  listFactorExperiments: vi.fn(),
  factorExperimentDetail: vi.fn(),
  syncValidation: vi.fn(),
}));

vi.mock("@/lib/research", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/research")>();
  return {
    ...actual,
    factorLabApi: factorLabApiMock,
  };
});

const mockPredefined = [
  {
    name: "return_21d",
    title: "return_21d = close / close[-21] - 1(21 根 bar 区间收益,动量)",
    family: "momentum",
    direction: "higher",
    signal_eligible: true,
    data_dependencies: ["bars.close"],
    window: 21,
    min_history_bars: null,
    cross_section: false,
  },
  {
    name: "log_mktcap",
    title: "对数总市值(风险暴露定位,不可直接生成信号)",
    family: "size",
    direction: "lower",
    signal_eligible: false,
    data_dependencies: ["daily_metrics.total_mv"],
    window: null,
    min_history_bars: null,
    cross_section: false,
  },
  {
    name: "alpha101_9",
    title: "Alpha#9:超长公式片段(条件 + 时序算子,截面排名)",
    family: "alpha101",
    direction: "higher",
    signal_eligible: true,
    data_dependencies: ["bars.open", "bars.close"],
    window: 5,
    min_history_bars: 239,
    cross_section: true,
  },
];

function renderWithProviders(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TooltipProvider>{ui}</TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openPredefinedTab() {
  const user = userEvent.setup();
  renderWithProviders(<FactorLab />);
  await user.click(screen.getByRole("tab", { name: "平台因子" }));
  await screen.findByText("p_return_21d");
  return user;
}

beforeEach(() => {
  vi.clearAllMocks();
  factorLabApiMock.catalog.mockResolvedValue([]);
  factorLabApiMock.predefined.mockResolvedValue(mockPredefined);
});

describe("FactorLab 平台因子页签(#427)", () => {
  it("渲染平台因子目录行:名称带 p_ 前缀 / 族 / 方向 / 可信号徽标", async () => {
    await openPredefinedTab();

    expect(screen.getByText("p_return_21d")).toBeInTheDocument();
    expect(screen.getByText("p_log_mktcap")).toBeInTheDocument();
    expect(screen.getByText("p_alpha101_9")).toBeInTheDocument();
    expect(screen.getByText(/共 3 个平台预置因子 · 显示 3 个/)).toBeInTheDocument();
    // signal_eligible=true 的两条带徽标,size 族显示「仅暴露」。
    expect(screen.getAllByText("可做信号")).toHaveLength(2);
    expect(screen.getByText("仅暴露")).toBeInTheDocument();
    expect(screen.getAllByText("值高看多")).toHaveLength(2);
    expect(screen.getByText("值低看多")).toBeInTheDocument();
    // 消费路径提示:构建走 factor_series_build,目录只读。
    expect(
      screen.getByText("平台预置因子是「公式即代码」的可信因子"),
    ).toBeInTheDocument();
  });

  it("展示窗口与覆盖起点、数据依赖", async () => {
    await openPredefinedTab();

    expect(screen.getByText("21 日")).toBeInTheDocument();
    expect(screen.getByText("≥239 根历史")).toBeInTheDocument();
    expect(screen.getByText("bars.open · bars.close")).toBeInTheDocument();
    // window=null 的因子显示占位符。
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("过滤框按名称子串过滤", async () => {
    const user = await openPredefinedTab();

    await user.type(screen.getByLabelText("过滤平台因子"), "log_mktcap");
    expect(screen.queryByText("p_return_21d")).not.toBeInTheDocument();
    expect(screen.queryByText("p_alpha101_9")).not.toBeInTheDocument();
    expect(screen.getByText("p_log_mktcap")).toBeInTheDocument();
    expect(screen.getByText(/显示 1 个/)).toBeInTheDocument();
  });

  it("过滤框按说明子串过滤且无匹配时显示空态", async () => {
    const user = await openPredefinedTab();

    await user.type(screen.getByLabelText("过滤平台因子"), "总市值");
    expect(screen.getByText("p_log_mktcap")).toBeInTheDocument();
    expect(screen.queryByText("p_return_21d")).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("过滤平台因子"), "zzz_无匹配");
    expect(screen.getByText("没有匹配的平台因子")).toBeInTheDocument();
    expect(
      screen.getByText(/清空过滤条件查看全部 3 个因子/),
    ).toBeInTheDocument();
  });
});
