import type { ReactElement } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type BacktestHistoryItem } from "../lib/api";
import Backtest from "./Backtest";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      getStrategies: vi.fn(),
      getInstruments: vi.fn(),
      getBacktestHistory: vi.fn(),
      deleteBacktestHistory: vi.fn(),
    },
  };
});

// 同参数重复运行:字段几乎一致,仅创建时刻与成交/夏普可区分
function makeHistory(id: number, createdAt: string): BacktestHistoryItem {
  return {
    id,
    strategy: "ma_cross",
    symbols: Array.from({ length: 47 }, (_, i) => `0000${i}.SZ`),
    start: "2018-01-01",
    end: "2026-07-31",
    capital: "100000",
    adjust: "qfq",
    metrics: { total_return: 0, trade_count: 2, sharpe_ratio: 1.234 },
    factor_version: null,
    created_at: createdAt,
  };
}

function renderWithProviders(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getStrategies).mockResolvedValue([]);
  vi.mocked(api.getInstruments).mockResolvedValue({
    items: [],
    total: 0,
    limit: 200,
    offset: 0,
  });
  vi.mocked(api.getBacktestHistory).mockResolvedValue([
    makeHistory(1, "2026-09-17T10:05:00"),
    makeHistory(2, "2026-09-17T23:36:00"),
  ]);
  vi.mocked(api.deleteBacktestHistory).mockResolvedValue({
    ok: true,
    status: 204,
  } as Response);
});

describe("回测历史行内区分度", () => {
  it("展示成交笔数、夏普与创建时刻,同参数记录可区分", async () => {
    renderWithProviders(<Backtest />);

    expect(await screen.findAllByText("成交 2 笔 · 夏普 1.23")).toHaveLength(2);
    expect(screen.getByText("09-17 10:05")).toBeInTheDocument();
    expect(screen.getByText("09-17 23:36")).toBeInTheDocument();
  });
});

describe("回测历史删除防护", () => {
  it("点击删除只打开二次确认,取消不发请求", async () => {
    renderWithProviders(<Backtest />);
    await screen.findAllByText("成交 2 笔 · 夏普 1.23");

    await userEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("确认删除回测记录")).toBeInTheDocument();
    expect(api.deleteBacktestHistory).not.toHaveBeenCalled();

    await userEvent.click(
      within(dialog).getByRole("button", { name: "取消" }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.deleteBacktestHistory).not.toHaveBeenCalled();
  });

  it("确认后按该条 id 发送一次删除请求", async () => {
    renderWithProviders(<Backtest />);
    await screen.findAllByText("成交 2 笔 · 夏普 1.23");

    await userEvent.click(screen.getAllByRole("button", { name: "删除" })[1]);
    const dialog = screen.getByRole("dialog");
    await userEvent.click(
      within(dialog).getByRole("button", { name: "确认删除" }),
    );

    await waitFor(() =>
      expect(api.deleteBacktestHistory).toHaveBeenCalledTimes(1),
    );
    expect(api.deleteBacktestHistory).toHaveBeenCalledWith(2);
  });
});
