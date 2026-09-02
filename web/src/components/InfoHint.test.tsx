import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import InfoHint from "./InfoHint";

const content = {
  title: { zh: "滑点", en: "Slippage" },
  description: { zh: "模拟信号价格与成交价格之间的不利偏差。", en: "Adverse deviation between simulated signal price and fill price." },
  detail: { zh: "1 bps = 0.01%。", en: "1 bps = 0.01%." },
};

afterEach(() => {
  vi.useRealTimers();
});

describe("InfoHint", () => {
  it("通过 hover 展示说明，并在离开后关闭", () => {
    vi.useFakeTimers();
    render(<InfoHint content={content} />);
    const trigger = screen.getByRole("button", { name: "查看“滑点”说明" });

    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.pointerEnter(trigger);

    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent(content.description.zh);
    expect(tooltip).toHaveTextContent(content.detail.zh);
    expect(tooltip.parentElement).toBe(document.body);
    expect(trigger).toHaveAttribute("aria-describedby", tooltip.id);

    fireEvent.pointerLeave(trigger);
    act(() => vi.advanceTimersByTime(100));
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("支持键盘焦点、Enter、Space 与 Escape", async () => {
    const user = userEvent.setup();
    render(<InfoHint content={content} />);
    const trigger = screen.getByRole("button", { name: "查看“滑点”说明" });

    await user.tab();
    expect(trigger).toHaveFocus();
    expect(screen.getByRole("tooltip")).toBeInTheDocument();

    await user.keyboard("{Enter}");
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    await user.keyboard(" ");
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
  });

  it("支持 click/touch 切换并可点击外部关闭", async () => {
    const user = userEvent.setup();
    render(
      <div>
        <InfoHint content={content} />
        <button type="button">外部操作</button>
      </div>,
    );
    const trigger = screen.getByRole("button", { name: "查看“滑点”说明" });

    await user.click(trigger);
    expect(screen.getByRole("tooltip")).toBeInTheDocument();

    await user.click(trigger);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

    await user.click(trigger);
    fireEvent.pointerDown(screen.getByRole("button", { name: "外部操作" }));
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("可以安全渲染长说明并在卸载时清理延时任务", () => {
    vi.useFakeTimers();
    const longContent = {
      title: { zh: "长说明", en: "Long description" },
      description: { zh: "说明".repeat(300), en: "text".repeat(300) },
    };
    const { unmount } = render(<InfoHint content={longContent} />);
    const trigger = screen.getByRole("button", { name: "查看“长说明”说明" });

    fireEvent.pointerEnter(trigger);
    expect(screen.getByRole("tooltip")).toHaveTextContent(longContent.description.zh);
    fireEvent.pointerLeave(trigger);
    unmount();

    expect(() => vi.runAllTimers()).not.toThrow();
  });
});
