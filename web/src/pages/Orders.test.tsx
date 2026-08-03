import type { ReactElement } from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import Orders from "./Orders";

vi.mock("../lib/api", () => ({
  api: {
    getOrders: vi.fn(() =>
      Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 }),
    ),
    cancelOrder: vi.fn(() => Promise.resolve()),
    placeOrder: vi.fn(() => Promise.resolve({})),
  },
}));

function renderWithProviders(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>,
  );
}

describe("Orders 二次确认安全机制", () => {
  it("渲染后显示 '实盘受控区域' 警告标记", () => {
    renderWithProviders(<Orders />);
    expect(screen.getByText("实盘受控区域")).toBeInTheDocument();
  });

  it("点击 '手工下单' 后显示下单表单", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Orders />);
    await user.click(screen.getByRole("button", { name: "手工下单" }));
    expect(
      screen.getByPlaceholderText("标的 (如 510300.SH)"),
    ).toBeInTheDocument();
  });

  it("填写表单并提交后显示确认弹窗（'确认下单'）", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Orders />);
    await user.click(screen.getByRole("button", { name: "手工下单" }));
    await user.type(
      screen.getByPlaceholderText("标的 (如 510300.SH)"),
      "510300.SH",
    );
    await user.click(screen.getByRole("button", { name: "提交" }));
    expect(
      await screen.findByRole("heading", { name: "确认下单" }),
    ).toBeInTheDocument();
  });

  it("提交表单后不直接调用 placeOrder（需要先确认）", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Orders />);
    await user.click(screen.getByRole("button", { name: "手工下单" }));
    await user.type(
      screen.getByPlaceholderText("标的 (如 510300.SH)"),
      "510300.SH",
    );
    await user.click(screen.getByRole("button", { name: "提交" }));
    await screen.findByRole("heading", { name: "确认下单" });
    expect(vi.mocked(api.placeOrder)).not.toHaveBeenCalled();
  });

  it("确认弹窗中显示标的 / 方向 / 数量信息", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Orders />);
    await user.click(screen.getByRole("button", { name: "手工下单" }));
    await user.type(
      screen.getByPlaceholderText("标的 (如 510300.SH)"),
      "510300.SH",
    );
    await user.click(screen.getByRole("button", { name: "提交" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("510300.SH")).toBeInTheDocument();
    expect(within(dialog).getByText("买入")).toBeInTheDocument();
    expect(within(dialog).getByText("100")).toBeInTheDocument();
  });
});
