import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Backtest from "../pages/Backtest";
import Data from "../pages/Data";
import Settings from "../pages/Settings";
import Strategies from "../pages/Strategies";

const apiMock = vi.hoisted(() => ({
  getStrategies: vi.fn(),
  getStrategyPresets: vi.fn(),
  getStrategyPreset: vi.fn(),
  createStrategyPreset: vi.fn(),
  updateStrategyPreset: vi.fn(),
  deleteStrategyPreset: vi.fn(),
  getBacktestHistory: vi.fn(),
  getBacktestHistoryDetail: vi.fn(),
  deleteBacktestHistory: vi.fn(),
  runBacktest: vi.fn(),
  getInstruments: vi.fn(),
  getInstrumentCodes: vi.fn(),
  getWatchlists: vi.fn(),
  getWatchlist: vi.fn(),
  createWatchlist: vi.fn(),
  addWatchlistSymbols: vi.fn(),
  getDataStatusPage: vi.fn(),
  searchInstruments: vi.fn(),
  getJob: vi.fn(),
  listJobs: vi.fn(),
  syncUniverse: vi.fn(),
  fetchData: vi.fn(),
  startBulkDownload: vi.fn(),
  getConfig: vi.fn(),
  updateConfig: vi.fn(),
  getLlmConfig: vi.fn(),
  updateLlmConfig: vi.fn(),
}));

const defaultFactorSelection = vi.hoisted(() => ({
  enabled: false,
  source: "tushare",
  factor_version: "v1",
  max_symbols: 20,
  ranking_factor: "market_cap",
  ranking_scope: "global",
  ranking_ascending: false,
  max_per_industry: null,
  min_listing_days: 60,
  exclude_st: true,
  exclude_suspended: true,
  momentum_lookback: 20,
  min_market_cap: null,
  max_market_cap: null,
  min_pb: null,
  max_pb: null,
  min_turnover_rate: null,
  max_turnover_rate: null,
  min_momentum: null,
  min_roe: null,
  min_gross_profit_margin: null,
  min_revenue_yoy: null,
  dataset_versions: {},
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, ...apiMock },
    DEFAULT_FACTOR_SELECTION: defaultFactorSelection,
  };
});

const strategy = {
  kind: "ma_cross",
  name: "均线交叉",
  description: "使用短期与长期均线交叉生成信号。",
  supports_backtest: true,
  params: [
    {
      name: "short_window",
      label: "短期均线",
      type: "integer",
      default: 5,
      required: true,
      description: "短期均线窗口。",
      enum: null,
      minimum: 2,
      maximum: 120,
      exclusive_minimum: null,
      exclusive_maximum: null,
      min_length: null,
      max_length: null,
      nullable: false,
      ui_hidden: false,
    },
  ],
};

function renderPage(page: React.ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>{page}</QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.getStrategies.mockResolvedValue([strategy]);
  apiMock.getStrategyPresets.mockResolvedValue([]);
  apiMock.getBacktestHistory.mockResolvedValue([]);
  apiMock.getInstruments.mockResolvedValue({
    items: [],
    total: 0,
    limit: 200,
    offset: 0,
  });
  apiMock.getInstrumentCodes.mockResolvedValue([]);
  apiMock.getDataStatusPage.mockResolvedValue({
    items: [],
    total: 0,
    limit: 200,
    offset: 0,
  });
  apiMock.getJob.mockResolvedValue({
    job_id: "BJ-IDLE",
    kind: "bulk_download",
    queue: "data",
    status: "succeeded",
    priority: 0,
    payload: {},
    payload_checksum: "x",
    idempotency_key: "x",
    progress_total: 0,
    progress_done: 0,
    phase: null,
    result_ref: null,
    error_code: null,
    error_summary: null,
    attempt: 0,
    max_attempts: 3,
    worker_id: null,
    heartbeat_at: null,
    lease_until: null,
    requested_by: "test",
    created_at: "2026-08-01T00:00:00+00:00",
    started_at: null,
    finished_at: "2026-08-01T00:00:01+00:00",
    updated_at: "2026-08-01T00:00:01+00:00",
  });
  apiMock.getConfig.mockResolvedValue({
    sync_enabled: true,
    sync_time: "08:00",
    download_enabled: true,
    download_time: "18:00",
    download_lookback_days: 5,
    download_markets: ["a_share"],
    download_types: ["stock", "etf"],
    data_provider: "akshare",
  });
  apiMock.getLlmConfig.mockResolvedValue({
    provider: "fake",
    base_url: "",
    api_key: "",
    api_key_set: false,
    model: "gpt-4o-mini",
    timeout_seconds: 30,
    max_retries: 3,
  });
});

describe("目标页面 InfoHint 接入", () => {
  it("回测页展示资金与费用说明", async () => {
    const user = userEvent.setup();
    renderPage(<Backtest />);

    const trigger = await screen.findByRole("button", {
      name: "查看“初始资金”说明",
    });
    await user.click(trigger);
    expect(screen.getByRole("tooltip")).toHaveTextContent("纸面账户");
  });

  it("数据页展示标的代码与缓存复权说明", async () => {
    const user = userEvent.setup();
    renderPage(<Data />);

    const symbolHint = await screen.findByRole("button", {
      name: "查看“证券标的代码”说明",
    });
    await user.click(symbolHint);
    expect(screen.getByRole("tooltip")).toHaveTextContent("代码.交易所");

    await user.click(
      screen.getByRole("button", { name: "查看“批量行情数据源”说明" }),
    );
    expect(screen.getByRole("tooltip")).toHaveTextContent("系统默认配置");

    await user.click(
      screen.getByRole("button", { name: "查看“缓存行情”说明" }),
    );
    expect(screen.getByRole("tooltip")).toHaveTextContent("qfq");
  });

  it("数据页批量拉取提交后展示统一任务队列进度条", async () => {
    const user = userEvent.setup();
    // startBulkDownload 现在返回 JobOut(#144);随后 Data.tsx 轮询 getJob。
    apiMock.startBulkDownload.mockResolvedValue({
      job_id: "BJ-PROGRESS",
      kind: "bulk_download",
      queue: "data",
      status: "queued",
      priority: 0,
      payload: {},
      payload_checksum: "x",
      idempotency_key: "x",
      progress_total: 3,
      progress_done: 2,
      phase: "fetching",
      result_ref: null,
      error_code: null,
      error_summary: null,
      attempt: 0,
      max_attempts: 3,
      worker_id: null,
      heartbeat_at: null,
      lease_until: null,
      requested_by: "test",
      created_at: "2026-08-03T01:00:00+00:00",
      started_at: "2026-08-03T01:00:01+00:00",
      finished_at: null,
      updated_at: "2026-08-03T01:00:01+00:00",
    });
    apiMock.getJob.mockResolvedValue({
      job_id: "BJ-PROGRESS",
      kind: "bulk_download",
      queue: "data",
      status: "running",
      priority: 0,
      payload: {},
      payload_checksum: "x",
      idempotency_key: "x",
      progress_total: 3,
      progress_done: 2,
      phase: "fetching",
      result_ref: null,
      error_code: null,
      error_summary: null,
      attempt: 0,
      max_attempts: 3,
      worker_id: null,
      heartbeat_at: null,
      lease_until: null,
      requested_by: "test",
      created_at: "2026-08-03T01:00:00+00:00",
      started_at: "2026-08-03T01:00:01+00:00",
      finished_at: null,
      updated_at: "2026-08-03T01:00:01+00:00",
    });

    renderPage(<Data />);

    const startButton = await screen.findByRole("button", {
      name: "开始批量拉取",
    });
    await user.click(startButton);

    // 进度条出现,显示 2 / 3。
    const progressbar = await screen.findByRole("progressbar");
    expect(progressbar).toHaveAttribute("aria-valuenow", "2");
    expect(progressbar).toHaveAttribute("aria-valuemax", "3");
    expect(screen.getByText("进度: 2 / 3")).toBeInTheDocument();
  });

  it("设置页展示数据源与定时任务说明", async () => {
    const user = userEvent.setup();
    renderPage(<Settings />);

    const trigger = await screen.findByRole("button", {
      name: "查看“行情数据源”说明",
    });
    await user.click(trigger);
    expect(screen.getByRole("tooltip")).toHaveTextContent(
      "FINBOARD_DATA_PROVIDER",
    );
  });

  it("策略配置页展示预设安全边界", async () => {
    const user = userEvent.setup();
    renderPage(<Strategies />);

    const trigger = await screen.findByRole("button", {
      name: "查看“策略预设”说明",
    });
    await user.click(trigger);
    expect(screen.getByRole("tooltip")).toHaveTextContent("不会启动策略");
  });
});
