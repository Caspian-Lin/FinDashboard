import type { ReactElement } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { AppShell } from "./AppShell";
import { ThemeProvider } from "@/components/ui/theme-provider";

vi.mock("@/lib/ws", () => ({
  useWebSocket: () => false,
}));

vi.mock("@/lib/api", () => ({
  api: {
    health: vi.fn(() =>
      Promise.resolve({
        status: "ok",
        kernel_ready: true,
        kill_switch_level: "off",
      }),
    ),
  },
}));

function renderWithProviders(ui: ReactElement, path: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

describe("AppShell 布局与标题层级(#163)", () => {
  it("顶栏不渲染 h1;页面内容区的 h1 是唯一主标题", () => {
    renderWithProviders(
      <AppShell>
        <h1>页面标题</h1>
      </AppShell>,
      "/orders",
    );
    const headings = screen.getAllByRole("heading", { level: 1 });
    expect(headings).toHaveLength(1);
    expect(headings[0]).toHaveTextContent("页面标题");
  });

  it("受控区域页面顶栏显示「受控区域」标记", async () => {
    renderWithProviders(
      <AppShell>
        <h1>订单</h1>
      </AppShell>,
      "/orders",
    );
    expect(await screen.findByText("受控区域")).toBeInTheDocument();
  });

  it("折叠按钮切换侧栏宽度并带可访问名称", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <AppShell>
        <h1>仪表盘</h1>
      </AppShell>,
      "/",
    );
    const toggle = screen.getByRole("button", { name: "折叠侧栏" });
    await user.click(toggle);
    expect(screen.getByRole("button", { name: "展开侧栏" })).toBeInTheDocument();
  });
});
