import type { ReactElement } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type KillSwitch } from "../lib/api";
import Control from "./Control";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      getKillSwitch: vi.fn(),
      activateKillSwitch: vi.fn(),
      getAuditLogs: vi.fn(),
      triggerReconcile: vi.fn(),
    },
  };
});

const KS_OFF: KillSwitch = {
  level: "off",
  allows_new_orders: true,
  allows_reduce_only: true,
};

const LEVEL_BUTTONS = ["恢复正常", "暂停新单", "仅减仓", "撤全部", "全局停止"];

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
  vi.mocked(api.getKillSwitch).mockResolvedValue(KS_OFF);
  vi.mocked(api.getAuditLogs).mockResolvedValue({
    items: [],
    total: 0,
    limit: 50,
    offset: 0,
  });
  vi.mocked(api.triggerReconcile).mockResolvedValue({
    ok: true,
    summary: "核对完成",
  });
});

describe("Kill Switch 确认门", () => {
  it("五档按钮点击后只打开确认弹窗,不调用 mutation", async () => {
    renderWithProviders(<Control />);
    await screen.findByText("off");
    for (const name of LEVEL_BUTTONS) {
      await userEvent.click(screen.getByRole("button", { name }));
      expect(screen.getByRole("dialog")).toBeInTheDocument();
      await userEvent.click(
        within(screen.getByRole("dialog")).getByRole("button", { name: "取消" }),
      );
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    }
    expect(api.activateKillSwitch).not.toHaveBeenCalled();
  });

  it("确认弹窗展示当前状态、目标状态、影响范围、恢复提示与原因输入", async () => {
    renderWithProviders(<Control />);
    await screen.findByText("off");
    await userEvent.click(screen.getByRole("button", { name: "全局停止" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("当前状态")).toBeInTheDocument();
    expect(within(dialog).getByText("off")).toBeInTheDocument();
    expect(within(dialog).getByText("目标状态")).toBeInTheDocument();
    expect(within(dialog).getByText("全局停止")).toBeInTheDocument();
    expect(within(dialog).getByText(/立即停止一切交易行为/)).toBeInTheDocument();
    expect(within(dialog).getByText(/不可自动恢复/)).toBeInTheDocument();
    expect(within(dialog).getByLabelText("操作原因")).toBeInTheDocument();
    expect(api.activateKillSwitch).not.toHaveBeenCalled();
  });

  it("取消、X 关闭与 Escape 关闭均不产生状态变更请求", async () => {
    renderWithProviders(<Control />);
    await screen.findByText("off");

    // 取消
    await userEvent.click(screen.getByRole("button", { name: "暂停新单" }));
    await userEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "取消" }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.activateKillSwitch).not.toHaveBeenCalled();

    // X 关闭
    await userEvent.click(screen.getByRole("button", { name: "仅减仓" }));
    await userEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "关闭" }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.activateKillSwitch).not.toHaveBeenCalled();

    // Escape
    await userEvent.click(screen.getByRole("button", { name: "撤全部" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(api.activateKillSwitch).not.toHaveBeenCalled();
  });

  it("确认后调用一次 mutation,成功后关闭弹窗并播报状态", async () => {
    const activate = vi.mocked(api.activateKillSwitch).mockResolvedValue(KS_OFF);
    renderWithProviders(<Control />);
    await screen.findByText("off");

    await userEvent.click(screen.getByRole("button", { name: "暂停新单" }));
    const dialog = screen.getByRole("dialog");
    const reasonInput = within(dialog).getByLabelText("操作原因");
    await userEvent.clear(reasonInput);
    await userEvent.type(reasonInput, "人工演练");
    await userEvent.click(within(dialog).getByRole("button", { name: "确认暂停新单" }));

    await waitFor(() => expect(activate).toHaveBeenCalledTimes(1));
    expect(activate).toHaveBeenCalledWith("no_new_orders", "人工演练");
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        /已切换至「暂停新单」/,
      ),
    );
  });

  it("键盘可确认操作(焦点在确认按钮上按 Enter)", async () => {
    const activate = vi.mocked(api.activateKillSwitch).mockResolvedValue(KS_OFF);
    renderWithProviders(<Control />);
    await screen.findByText("off");

    await userEvent.click(screen.getByRole("button", { name: "恢复正常" }));
    const dialog = screen.getByRole("dialog");
    const confirmButton = within(dialog).getByRole("button", {
      name: "确认恢复正常",
    });
    confirmButton.focus();
    await userEvent.keyboard("{Enter}");

    await waitFor(() => expect(activate).toHaveBeenCalledTimes(1));
    expect(activate).toHaveBeenCalledWith("off", "manual");
  });

  it("取消后焦点回到触发按钮", async () => {
    renderWithProviders(<Control />);
    await screen.findByText("off");

    const trigger = screen.getByRole("button", { name: "暂停新单" });
    // 真实浏览器中 mousedown 会聚焦按钮;jsdom 不模拟该默认行为,需显式聚焦
    trigger.focus();
    await userEvent.click(trigger);
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "取消" }));

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });
});

describe("Kill Switch 重复提交保护", () => {
  it("pending 期间禁用全部档位与确认按钮,只发送一次请求,Escape 不关闭", async () => {
    let resolveActivate: (v: KillSwitch) => void = () => {};
    const activate = vi.mocked(api.activateKillSwitch).mockImplementation(
      () =>
        new Promise<KillSwitch>((resolve) => {
          resolveActivate = resolve;
        }),
    );
    renderWithProviders(<Control />);
    await screen.findByText("off");

    await userEvent.click(screen.getByRole("button", { name: "暂停新单" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "确认暂停新单" }));
    await waitFor(() => expect(activate).toHaveBeenCalledTimes(1));

    // 页面档位按钮全部禁用,无法再打开其他确认流程
    // (modal 弹窗对页面设置 aria-hidden,需 hidden:true 才能查询)
    for (const name of LEVEL_BUTTONS) {
      expect(
        screen.getByRole("button", { name, hidden: true }),
      ).toBeDisabled();
    }
    // 弹窗内确认按钮禁用并显示进行中,取消按钮禁用
    expect(
      within(dialog).getByRole("button", { name: "切换中..." }),
    ).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "取消" })).toBeDisabled();
    // 状态播报
    expect(screen.getByRole("status", { hidden: true })).toHaveTextContent(
      /正在切换 Kill Switch 至「暂停新单」/,
    );

    // 尝试点击其他档位与 Escape —— 均不产生第二次请求
    // (禁用按钮 pointer-events: none,浏览器不会触发点击,userEvent 同样拒绝交互)
    await userEvent.keyboard("{Escape}");
    expect(activate).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    resolveActivate(KS_OFF);
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(activate).toHaveBeenCalledTimes(1);
  });

  it("网络失败显示错误,不自动重发,保留显式重试路径", async () => {
    const activate = vi.mocked(api.activateKillSwitch)
      .mockRejectedValueOnce(new Error("网络错误"))
      .mockResolvedValueOnce(KS_OFF);
    renderWithProviders(<Control />);
    await screen.findByText("off");

    await userEvent.click(screen.getByRole("button", { name: "仅减仓" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "确认仅减仓" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("网络错误");
    expect(activate).toHaveBeenCalledTimes(1);
    // 弹窗保持打开,等待用户决定;不静默重发
    await waitFor(() =>
      expect(within(dialog).getByRole("button", { name: "确认仅减仓" })).toBeEnabled(),
    );
    expect(activate).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // 显式重试:再次点击确认
    await userEvent.click(within(dialog).getByRole("button", { name: "确认仅减仓" }));
    await waitFor(() => expect(activate).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
  });

  it("后端拒绝时展示拒绝原因,不产生重复请求", async () => {
    const activate = vi.mocked(api.activateKillSwitch).mockRejectedValue(
      new Error("校验失败:reason 无效"),
    );
    renderWithProviders(<Control />);
    await screen.findByText("off");

    await userEvent.click(screen.getByRole("button", { name: "撤全部" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "确认撤全部" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("校验失败:reason 无效");
    expect(activate).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await waitFor(() =>
      expect(within(dialog).getByRole("button", { name: "确认撤全部" })).toBeEnabled(),
    );
    expect(activate).toHaveBeenCalledTimes(1);
  });
});
