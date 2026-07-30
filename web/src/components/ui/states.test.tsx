import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { EmptyState, LoadingState, ErrorState, BlockedState } from "./states";

describe("EmptyState", () => {
  it("渲染 title 和 description", () => {
    render(<EmptyState title="无数据" description="暂无订单记录" />);
    expect(screen.getByText("无数据")).toBeInTheDocument();
    expect(screen.getByText("暂无订单记录")).toBeInTheDocument();
  });

  it("渲染 action（传入的 button）", () => {
    render(
      <EmptyState
        title="无数据"
        action={<button type="button">添加订单</button>}
      />,
    );
    expect(
      screen.getByRole("button", { name: "添加订单" }),
    ).toBeInTheDocument();
  });
});

describe("LoadingState", () => {
  it("渲染指定行数的 skeleton", () => {
    const rows = 4;
    const { container } = render(<LoadingState rows={rows} />);
    expect(container.querySelectorAll(".animate-pulse")).toHaveLength(rows * 3);
  });

  it("默认渲染 3 行 skeleton", () => {
    const { container } = render(<LoadingState />);
    expect(container.querySelectorAll(".animate-pulse")).toHaveLength(3 * 3);
  });
});

describe("ErrorState", () => {
  it("渲染错误标题和重试按钮", () => {
    render(<ErrorState title="加载失败" onRetry={() => {}} />);
    expect(screen.getByText("加载失败")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });

  it("点击重试按钮触发 onRetry 回调", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    render(<ErrorState onRetry={onRetry} />);
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("未提供 onRetry 时不渲染重试按钮", () => {
    render(<ErrorState title="加载失败" />);
    expect(
      screen.queryByRole("button", { name: "重试" }),
    ).not.toBeInTheDocument();
  });
});

describe("BlockedState", () => {
  it("渲染 title 和 description", () => {
    render(<BlockedState title="功能受限" description="当前权限不足" />);
    expect(screen.getByText("功能受限")).toBeInTheDocument();
    expect(screen.getByText("当前权限不足")).toBeInTheDocument();
  });
});
