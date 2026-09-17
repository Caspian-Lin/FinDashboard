import type { ReactElement } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Account, type Health } from "../lib/api";
import Dashboard from "./Dashboard";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      health: vi.fn(),
      getAccount: vi.fn(),
      getOrders: vi.fn(),
    },
  };
});

const HEALTH: Health = {
  status: "ok",
  kernel_ready: true,
  kill_switch_level: "off",
  broker_connected: true,
  broker_kind: "mock",
};

const ACCOUNT: Account = {
  account_id: "SIM-0001",
  broker_kind: "mock",
  total_asset: "100000",
  cash: "80000",
  frozen_cash: "0",
  margin_used: "0",
  updated_at: "2026-09-17T10:00:00+08:00",
};

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
  vi.mocked(api.health).mockResolvedValue(HEALTH);
  vi.mocked(api.getOrders).mockResolvedValue({
    items: [],
    total: 0,
    limit: 500,
    offset: 0,
  });
});

describe("仪表盘账户区错误态", () => {
  it("account 失败时展示具名错误与重试按钮,不再只有 —", async () => {
    vi.mocked(api.getAccount).mockRejectedValue(
      new Error("HTTP 500: Internal Server Error"),
    );
    renderWithProviders(<Dashboard />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("账户数据加载失败");
    expect(alert).toHaveTextContent("HTTP 500: Internal Server Error");
    expect(within(alert).getByRole("button", { name: "重试" })).toBeInTheDocument();

    // account 失败:券商卡回退 health.broker_kind,账户卡保持 —
    expect(screen.getByText("mock")).toBeInTheDocument();
    const accountCard = screen
      .getByText("账户")
      .closest("div.rounded-lg") as HTMLElement;
    expect(within(accountCard).getByText("—")).toBeInTheDocument();
  });

  it("点击重试后重新请求 account,成功后错误提示消失并展示金额", async () => {
    const getAccount = vi.mocked(api.getAccount);
    getAccount
      .mockRejectedValueOnce(new Error("HTTP 500: Internal Server Error"))
      .mockResolvedValueOnce(ACCOUNT);
    renderWithProviders(<Dashboard />);

    const alert = await screen.findByRole("alert");
    await userEvent.click(within(alert).getByRole("button", { name: "重试" }));

    await waitFor(() => expect(getAccount).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("¥100,000")).toBeInTheDocument();
    expect(screen.getByText("SIM-0001")).toBeInTheDocument();
  });

  it("account 成功时直接展示金额与账户号,无错误提示", async () => {
    vi.mocked(api.getAccount).mockResolvedValue(ACCOUNT);
    renderWithProviders(<Dashboard />);

    expect(await screen.findByText("¥100,000")).toBeInTheDocument();
    expect(screen.getByText("¥80,000")).toBeInTheDocument();
    expect(screen.getByText("SIM-0001")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
