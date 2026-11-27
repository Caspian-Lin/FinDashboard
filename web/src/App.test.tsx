import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import App, { LegacyResearchDataRedirect, LegacyStrategyPresetsRedirect } from "./App";
import { availableResearchTools, primaryNavigation } from "@/components/layout/nav-config";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@/components/ui/theme-provider";
import { LanguageProvider } from "@/i18n";

const mocks = vi.hoisted(() => ({ researchEdition: false, liveMount: vi.fn(), health: vi.fn() }));
vi.mock("@/lib/edition", () => ({ get RESEARCH_EDITION() { return mocks.researchEdition; } }));
vi.mock("@/lib/ws", () => ({ useWebSocket: () => false }));
vi.mock("@/lib/api", () => ({ api: { health: mocks.health } }));
vi.mock("@/pages/Dashboard", () => ({
  default: () => { mocks.liveMount(); return <h1>实盘仪表盘</h1>; },
}));
vi.mock("@/pages/Orders", () => ({
  default: () => { mocks.liveMount(); return <h1>订单</h1>; },
}));
vi.mock("@/pages/research/ResearchHome", () => ({
  default: ({ workspace }: { workspace?: boolean }) => <h1>{workspace ? "工作总览" : "研究总览"}</h1>,
}));

afterEach(() => {
  mocks.researchEdition = false;
  mocks.liveMount.mockClear();
  mocks.health.mockClear();
  localStorage.clear();
});

function DataLanding() {
  const { search, hash } = useLocation();
  return <div data-testid="data-page">{search + hash || "(no query)"}</div>;
}

function renderLegacy(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/research/data" element={<LegacyResearchDataRedirect />} />
        <Route path="/data" element={<DataLanding />} />
        <Route path="*" element={<div data-testid="not-found">404</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("/research/data 旧深链跳转(#512)", () => {
  it("旧页签映射为数据集 tab 并落到 /data(不 404)", () => {
    renderLegacy("/research/data?tab=metrics");
    expect(screen.getByTestId("data-page")).toHaveTextContent(
      "?tab=datasets&dataset=daily_metrics",
    );
    expect(screen.queryByTestId("not-found")).toBeNull();
  });

  it("release 深链参数保全并直接打开发布详情", () => {
    renderLegacy("/research/data?release=DR-777");
    expect(screen.getByTestId("data-page")).toHaveTextContent("?tab=datasets&release=DR-777");
  });

  it("旧页签 + release 同时保留", () => {
    renderLegacy("/research/data?tab=financial&release=DR-9");
    expect(screen.getByTestId("data-page")).toHaveTextContent(
      "?tab=datasets&dataset=financial_indicators&release=DR-9",
    );
  });

  it("无参数时落 /data 概览", () => {
    renderLegacy("/research/data");
    expect(screen.getByTestId("data-page")).toHaveTextContent("(no query)");
  });
});

describe("工作台导航与部署能力 (#521)", () => {
  function renderApp(path: string) {
    mocks.health.mockResolvedValue({ kernel_ready: true, kill_switch_level: "off" });
    return render(
      <ThemeProvider>
        <LanguageProvider>
          <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
            <MemoryRouter initialEntries={[path]}><App /></MemoryRouter>
          </QueryClientProvider>
        </LanguageProvider>
      </ThemeProvider>,
    );
  }

  it("完整与研究版各有明确的一级入口", () => {
    expect(primaryNavigation(false).map((item) => item.to)).toEqual([
      "/research/topics", "/data", "/jobs", "/live", "/settings",
    ]);
    expect(primaryNavigation(true).map((item) => item.to)).toEqual([
      "/research/topics", "/data", "/jobs", "/settings",
    ]);
    expect(availableResearchTools(false).some((tool) => tool.to === "/research/simulation")).toBe(true);
    expect(availableResearchTools(true).some((tool) => tool.to === "/research/simulation")).toBe(false);
  });

  it("完整版以工作总览为根，/live 打开实盘仪表盘", async () => {
    const user = userEvent.setup();
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "工作总览" })).toBeInTheDocument();
    expect(mocks.liveMount).not.toHaveBeenCalled();
    await user.click(screen.getByRole("link", { name: /^实盘$/ }));
    expect(await screen.findByRole("heading", { name: "实盘仪表盘" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "订单" })).toHaveAttribute("href", "/orders");
  });

  it("研究版根路由打开研究总览，导航无实盘动作", async () => {
    mocks.researchEdition = true;
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "研究总览" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^实盘$/ })).not.toBeInTheDocument();
    expect(mocks.health).not.toHaveBeenCalled();
  });

  it.each(["/live", "/positions", "/orders?status=active", "/fills", "/control"])(
    "研究版直达 %s 时显示受控回退，且不挂载实盘应用",
    async (path) => {
      mocks.researchEdition = true;
      renderApp(path);
      expect(await screen.findByRole("heading", { name: "实盘功能需要完整版" })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "打开研究总览" })).toHaveAttribute("href", "/research");
      expect(mocks.liveMount).not.toHaveBeenCalled();
      expect(mocks.health).not.toHaveBeenCalled();
    },
  );

  it("策略预设旧 URL 跳到回测并保全查询和锚点", () => {
    render(
      <MemoryRouter initialEntries={["/strategies?preset=23#library"]}>
        <Routes>
          <Route path="/strategies" element={<LegacyStrategyPresetsRedirect />} />
          <Route path="/backtest" element={<DataLanding />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("data-page")).toHaveTextContent("?preset=23#library");
  });

  it("英文界面提供有效的跳到正文链接和一级导航", async () => {
    mocks.researchEdition = true;
    localStorage.setItem("finboard-lang", "en");
    renderApp("/");
    const skip = screen.getByRole("link", { name: "Skip to main content" });
    expect(skip).toHaveAttribute("href", "#main-content");
    expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
    expect(screen.getByRole("link", { name: "Tasks & audit" })).toHaveAttribute("href", "/jobs");
  });
});
