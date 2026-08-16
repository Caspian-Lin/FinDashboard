import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Button } from "./button";
import { Card, CardContent } from "./card";
import { Input, Textarea } from "./input";
import { StatCard } from "./stat-card";
import { ThemeProvider, useTheme } from "./theme-provider";

/** 主题 token 契约:浅色切换后 html class 与 localStorage 同步(issue #162)。 */
function ThemeProbe() {
  const { theme, toggleTheme } = useTheme();
  return (
    <button type="button" onClick={toggleTheme}>
      theme:{theme}
    </button>
  );
}

describe("主题 token 契约", () => {
  it("默认深色(html 无 light class),切换后写入 localStorage 与 html class", async () => {
    localStorage.clear();
    const user = userEvent.setup();
    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );
    expect(screen.getByText("theme:dark")).toBeInTheDocument();
    expect(document.documentElement.classList.contains("light")).toBe(false);
    await user.click(screen.getByRole("button"));
    expect(screen.getByText("theme:light")).toBeInTheDocument();
    expect(document.documentElement.classList.contains("light")).toBe(true);
    expect(localStorage.getItem("findashboard-theme")).toBe("light");
    localStorage.clear();
  });

  it("localStorage 为 light 时初始即为浅色", () => {
    localStorage.setItem("findashboard-theme", "light");
    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );
    expect(screen.getByText("theme:light")).toBeInTheDocument();
    localStorage.clear();
  });
});

/** 无阴影契约:共享组件默认不携带 box-shadow 类(issue #162)。 */
function assertNoShadowClass(element: HTMLElement, label: string) {
  expect(element.className, `${label} 不应携带 shadow 类`).not.toMatch(/shadow/);
}

describe("共享组件无阴影契约", () => {
  it("Button / Card / Input / Textarea / StatCard 均无 shadow 类", () => {
    const { container } = render(
      <div>
        <Button>默认按钮</Button>
        <Card>
          <CardContent>卡片内容</CardContent>
        </Card>
        <Input defaultValue="输入" />
        <Textarea defaultValue="多行" />
        <StatCard label="指标" value="1.0" />
      </div>,
    );
    const buttons = container.querySelectorAll("button");
    expect(buttons.length).toBeGreaterThan(0);
    buttons.forEach((b) => assertNoShadowClass(b, "Button"));
    const card = container.querySelector("[data-slot='card']") ?? container.querySelector("div.border-border.bg-card");
    if (card) assertNoShadowClass(card as HTMLElement, "Card");
    const inputs = container.querySelectorAll("input");
    inputs.forEach((i) => assertNoShadowClass(i, "Input"));
    const textareas = container.querySelectorAll("textarea");
    textareas.forEach((t) => assertNoShadowClass(t, "Textarea"));
    const statCard = Array.from(container.querySelectorAll("div")).find((el) =>
      el.className.includes("text-2xl"),
    )?.parentElement;
    if (statCard) assertNoShadowClass(statCard, "StatCard");
  });
});
