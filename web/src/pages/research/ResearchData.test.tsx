import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ResearchData from "./ResearchData";

const datasetApiMock = vi.hoisted(() => ({
  releases: vi.fn(),
  manifests: vi.fn(),
  cachedData: vi.fn(),
  cachedDataSelection: vi.fn(),
  createRelease: vi.fn(),
  instruments: vi.fn(),
  lifecycle: vi.fn(),
  releaseDetail: vi.fn(),
  instrumentDetail: vi.fn(),
}));

vi.mock("@/lib/research", () => ({
  datasetApi: datasetApiMock,
}));

vi.mock("@/pages/Data", () => ({
  default: () => <div>行情拉取面板</div>,
}));

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

beforeEach(() => {
  vi.clearAllMocks();
  datasetApiMock.releases.mockResolvedValue([]);
  datasetApiMock.manifests.mockResolvedValue([]);
  datasetApiMock.cachedData.mockResolvedValue({
    items: [
      {
        symbol: "510300.SH",
        period: "1d",
        adjust: "qfq",
        bar_count: 242,
        first_date: "2024-01-02",
        last_date: "2024-12-31",
      },
    ],
    total: 1,
    limit: 50,
    offset: 0,
  });
  datasetApiMock.cachedDataSelection.mockResolvedValue({
    items: [
      {
        symbol: "510300.SH",
        period: "1d",
        adjust: "qfq",
        bar_count: 242,
        first_date: "2024-01-02",
        last_date: "2024-12-31",
      },
    ],
    total: 1,
    first_date: "2024-01-02",
    last_date: "2024-12-31",
  });
  datasetApiMock.instruments.mockResolvedValue({
    items: [],
    total: 0,
    limit: 200,
    offset: 0,
  });
  datasetApiMock.createRelease.mockResolvedValue({
    release_id: "daily-bars-20260731-v1",
    dataset_name: "multi_asset_daily_bars",
    source: "yfinance",
    version: "2026-07-31-v1",
    schema_version: "v1",
    start_date: "2024-01-02",
    end_date: "2024-12-31",
    period: "1d",
    adjustment: "qfq",
    symbol_count: 1,
    row_count: 242,
    coverage_pct: 100,
    capabilities: [],
    quality_status: "passed",
    known_limitations: [],
    published_at: "2026-07-31T00:00:00Z",
    release_checksum: "a".repeat(64),
  });
});

describe("ResearchData 数据发布闭环", () => {
  it("从缓存选择范围并创建不可变发布", async () => {
    const user = userEvent.setup();
    const { container } = renderWithProviders(<ResearchData />);

    await user.click(screen.getByRole("tab", { name: "数据发布" }));
    await user.click(
      screen.getByRole("button", { name: "创建数据发布" }),
    );

    const symbol = await screen.findByRole("checkbox");
    await user.click(symbol);
    expect(screen.getByLabelText("开始日期")).toHaveValue("2024-01-02");
    expect(screen.getByLabelText("结束日期")).toHaveValue("2024-12-31");

    await user.click(screen.getByRole("button", { name: "冻结并发布" }));

    await waitFor(() => expect(datasetApiMock.createRelease).toHaveBeenCalledOnce());
    expect(datasetApiMock.createRelease).toHaveBeenCalledWith(
      expect.objectContaining({
        symbols: ["510300.SH"],
        start_date: "2024-01-02",
        end_date: "2024-12-31",
        adjustment: "qfq",
        required_capabilities: [],
      }),
    );
    expect(await screen.findByText("数据发布成功")).toBeInTheDocument();
    expect(container.querySelector('input[type="file"]')).toBeNull();
    expect(container.querySelector("textarea")).toBeNull();
  });

  it("解释缓存与标的元数据数量不一致", async () => {
    const user = userEvent.setup();
    datasetApiMock.cachedData.mockResolvedValue({
      items: [],
      total: 6809,
      limit: 1,
      offset: 0,
    });
    datasetApiMock.instruments.mockResolvedValue({
      items: [
        {
          code: "000002.SZ",
          name: "B",
          market: "a_share",
          instrument_type: "stock",
          status: "active",
        },
      ],
      total: 1,
      limit: 200,
      offset: 0,
    });

    renderWithProviders(<ResearchData />);
    await user.click(screen.getByRole("tab", { name: "标的元数据" }));

    expect(
      await screen.findByText("元数据尚未覆盖现有行情缓存"),
    ).toBeInTheDocument();
    expect(screen.getByText(/6809 条行情缓存/)).toBeInTheDocument();
  });

  it("一键选择筛选结果并使用整体生命周期范围", async () => {
    const user = userEvent.setup();
    datasetApiMock.cachedData.mockResolvedValue({
      items: [
        {
          symbol: "510300.SH",
          period: "1d",
          adjust: "qfq",
          bar_count: 100,
          first_date: "2020-01-02",
          last_date: "2021-12-31",
        },
      ],
      total: 2,
      limit: 50,
      offset: 0,
    });
    datasetApiMock.cachedDataSelection.mockResolvedValue({
      items: [
        {
          symbol: "510300.SH",
          period: "1d",
          adjust: "qfq",
          bar_count: 100,
          first_date: "2020-01-02",
          last_date: "2021-12-31",
        },
        {
          symbol: "600519.SH",
          period: "1d",
          adjust: "qfq",
          bar_count: 100,
          first_date: "2022-01-04",
          last_date: "2024-12-31",
        },
      ],
      total: 2,
      first_date: "2020-01-02",
      last_date: "2024-12-31",
    });

    renderWithProviders(<ResearchData />);
    await user.click(screen.getByRole("tab", { name: "数据发布" }));
    await user.click(screen.getByRole("button", { name: "创建数据发布" }));
    await user.click(
      await screen.findByRole("button", {
        name: "全选筛选结果（2）",
      }),
    );

    await waitFor(() =>
      expect(datasetApiMock.cachedDataSelection).toHaveBeenCalledWith({
        q: undefined,
        period: "1d",
        adjust: "qfq",
      }),
    );
    expect(screen.getByText("已选 2 只")).toBeInTheDocument();
    expect(screen.getByLabelText("开始日期")).toHaveValue("2020-01-02");
    expect(screen.getByLabelText("结束日期")).toHaveValue("2024-12-31");
  });
});
