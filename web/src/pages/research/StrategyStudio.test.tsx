import type { ReactElement } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { describe, expect, it, vi } from "vitest";
import StrategyStudio from "./StrategyStudio";

vi.mock("@/lib/research", () => ({
  strategySpecApi: {
    registry: vi.fn(() =>
      Promise.resolve({
        strategies: [
          {
            kind: "ma_cross",
            name: "均线交叉",
            description: "test",
            issue: 29,
            asset_classes: ["equity"],
            supports_short: false,
            supports_no_code_template: true,
            produces_target_weights: true,
            can_execute_on_publish: false,
          },
        ],
        feature_sources: [],
        operators: [],
        lifecycle_stages: [],
        publication_starts_run: false,
        accepts_python: false,
      }),
    ),
    list: vi.fn(() => Promise.resolve([])),
    template: vi.fn(() =>
      Promise.resolve({
        strategy_id: "ma_cross",
        version: 1,
        spec: {},
        checksum: "abc",
        published: false,
        created_at: "",
        created_by: "",
      }),
    ),
    history: vi.fn(() => Promise.resolve([])),
    validate: vi.fn(),
    createDraft: vi.fn(),
    supersede: vi.fn(),
    publish: vi.fn(),
    rollback: vi.fn(),
    version: vi.fn(),
    diff: vi.fn(),
  },
  datasetApi: {
    releases: vi.fn(() => Promise.resolve([])),
    manifests: vi.fn(() => Promise.resolve([])),
    instruments: vi.fn(() => Promise.resolve([])),
    releaseDetail: vi.fn(),
    instrumentDetail: vi.fn(),
    lifecycle: vi.fn(),
  },
}));

vi.mock("@tanstack/react-query", async () => {
  const actual = await vi.importActual("@tanstack/react-query");
  return { ...actual };
});

function renderWithProviders(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TooltipProvider>{ui}</TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("StrategyStudio 安全边界", () => {
  it("渲染后显示安全边界提示（不支持 Python）", async () => {
    renderWithProviders(<StrategyStudio />);
    await waitFor(() => {
      expect(screen.getByText(/不支持 Python/)).toBeInTheDocument();
    });
  });

  it("不存在 file input 或 python textarea", async () => {
    const { container } = renderWithProviders(<StrategyStudio />);
    await screen.findByText("均线交叉");
    expect(container.querySelector('input[type="file"]')).toBeNull();
    container.querySelectorAll("textarea").forEach((ta) => {
      const label = (
        ta.getAttribute("aria-label") ??
        ta.getAttribute("name") ??
        ta.getAttribute("placeholder") ??
        ""
      ).toLowerCase();
      expect(label).not.toContain("python");
    });
  });

  it("渲染后显示策略类型列表", async () => {
    renderWithProviders(<StrategyStudio />);
    expect(await screen.findByText("均线交叉")).toBeInTheDocument();
    expect(screen.getByText("ma_cross")).toBeInTheDocument();
  });

  it("初始状态不存在 '运行' 或 '执行' 按钮（保存不启动运行）", async () => {
    renderWithProviders(<StrategyStudio />);
    await screen.findByText("均线交叉");
    expect(
      screen.queryByRole("button", { name: /运行|执行/ }),
    ).not.toBeInTheDocument();
  });
});
