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
  instrumentSummary: vi.fn(),
  etfSummary: vi.fn(),
  etfMetadata: vi.fn(),
  updateEtfClassification: vi.fn(),
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
        symbol: "000001.SZ",
        period: "1d",
        adjust: "qfq",
        bar_count: 242,
        first_date: "2024-01-02",
        last_date: "2024-12-31",
        source: "tushare",
      },
    ],
    total: 1,
    limit: 50,
    offset: 0,
  });
  datasetApiMock.cachedDataSelection.mockResolvedValue({
    items: [
      {
        symbol: "000001.SZ",
        period: "1d",
        adjust: "qfq",
        bar_count: 242,
        first_date: "2024-01-02",
        last_date: "2024-12-31",
        source: "tushare",
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
  datasetApiMock.instrumentSummary.mockResolvedValue({
    total: 0,
    active_total: 0,
    active_etf_total: 0,
    by_status: {},
    by_market: {},
    by_instrument_type: {},
  });
  datasetApiMock.etfSummary.mockResolvedValue({
    total: 0,
    auto_adopted: 0,
    needs_review: 0,
    manually_confirmed: 0,
    manually_overridden: 0,
    missing_metadata: 0,
  });
  datasetApiMock.etfMetadata.mockResolvedValue(null);
  datasetApiMock.updateEtfClassification.mockResolvedValue({
    code: "159001.SZ",
    fund_code: "159001",
    execution_profile: "money_market_etf",
    category: "money_market",
    underlying_asset_class: "cash",
    underlying_market: "domestic",
    strategy_type: "index",
    underlying_index: null,
    management_fee_rate: null,
    custody_fee_rate: null,
    tracking_error: null,
    inception_date: null,
    listing_date: null,
    delisting_date: null,
    iopv_available: false,
    allows_t_plus_0: true,
    dividend_policy: "cash",
  });
  datasetApiMock.createRelease.mockResolvedValue({
    release_id: "daily-bars-20260731-v1",
    dataset_name: "a_share_daily_bars",
    source: "tushare",
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
        release_kind: "a_share_tushare",
        source: "tushare",
        symbols: ["000001.SZ"],
        start_date: "2024-01-02",
        end_date: "2024-12-31",
        adjustment: "qfq",
        required_capabilities: ["stock"],
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
          symbol: "000001.SZ",
          period: "1d",
          adjust: "qfq",
          bar_count: 100,
          first_date: "2020-01-02",
          last_date: "2021-12-31",
          source: "tushare",
        },
      ],
      total: 2,
      limit: 50,
      offset: 0,
    });
    datasetApiMock.cachedDataSelection.mockResolvedValue({
      items: [
        {
          symbol: "000001.SZ",
          period: "1d",
          adjust: "qfq",
          bar_count: 100,
          first_date: "2020-01-02",
          last_date: "2021-12-31",
          source: "tushare",
        },
        {
          symbol: "600519.SH",
          period: "1d",
          adjust: "qfq",
          bar_count: 100,
          first_date: "2022-01-04",
          last_date: "2024-12-31",
          source: "tushare",
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

  it("发布缺少 ETF 分类时可原地补齐并重试", async () => {
    const user = userEvent.setup();
    datasetApiMock.cachedData.mockResolvedValue({
      items: [
        {
          symbol: "159001.SZ",
          period: "1d",
          adjust: "qfq",
          bar_count: 242,
          first_date: "2024-01-02",
          last_date: "2024-12-31",
          source: "akshare",
        },
        {
          symbol: "000001.SZ",
          period: "1d",
          adjust: "qfq",
          bar_count: 242,
          first_date: "2024-01-02",
          last_date: "2024-12-31",
          source: "tushare",
        },
      ],
      total: 2,
      limit: 50,
      offset: 0,
    });
    datasetApiMock.createRelease
      .mockRejectedValueOnce(
        new Error("数据质量门未通过: 159001.SZ: ETF 缺少分类元数据"),
      )
      .mockResolvedValueOnce({
        release_id: "daily-bars-20260731-v1",
        dataset_name: "multi_asset_daily_bars",
        source: "akshare",
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
        release_checksum: "b".repeat(64),
      });

    renderWithProviders(<ResearchData />);
    await user.click(screen.getByRole("tab", { name: "数据发布" }));
    await user.click(screen.getByRole("button", { name: "创建数据发布" }));
    await user.click(screen.getByLabelText("发布类型"));
    await user.click(screen.getByRole("option", { name: "多资产混合来源" }));
    const symbols = await screen.findAllByRole("checkbox");
    await user.click(symbols[0]);
    await user.click(symbols[1]);
    await user.click(screen.getByRole("button", { name: "冻结并发布" }));

    expect(
      await screen.findByText("需要补齐 ETF 元数据"),
    ).toBeInTheDocument();
    await user.click(screen.getByLabelText("执行档位"));
    await user.click(screen.getByRole("option", { name: "货币 ETF" }));
    await user.click(screen.getByRole("button", { name: "补齐元数据" }));

    await waitFor(() =>
      expect(datasetApiMock.updateEtfClassification).toHaveBeenCalledWith(
        "159001.SZ",
        expect.objectContaining({
          execution_profile: "money_market_etf",
          underlying_index: null,
        }),
      ),
    );
    await user.click(
      await screen.findByRole("button", { name: "重新校验并发布" }),
    );
    expect(await screen.findByText("数据发布成功")).toBeInTheDocument();
    expect(datasetApiMock.createRelease).toHaveBeenCalledTimes(2);
  });
});
