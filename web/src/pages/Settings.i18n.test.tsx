import type { ReactElement } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider } from "@/components/ui/theme-provider";
import { LanguageProvider } from "@/i18n";
import { api, ApiError, fetchJSON } from "@/lib/api";
import Settings from "./Settings";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchJSON: vi.fn(),
    api: { getConfig: vi.fn(), updateConfig: vi.fn(), getDataSourceStatus: vi.fn(), getTushareQuota: vi.fn() },
  };
});

const config = {
  revision: "a".repeat(64),
  data_provider: "yfinance",
  sync_enabled: false,
  sync_time: "06:00",
  download_enabled: false,
  download_time: "18:00",
  download_lookback_days: 5,
  download_markets: ["a_share"],
  download_types: ["stock"],
};

function renderWithProviders(ui: ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <LanguageProvider><ThemeProvider>{ui}</ThemeProvider></LanguageProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.getConfig).mockResolvedValue(config);
  vi.mocked(api.getDataSourceStatus).mockResolvedValue({
    provider: "yfinance", tushare_configured: false, sandbox_enabled: false,
  });
  vi.mocked(fetchJSON).mockImplementation((path) =>
    Promise.resolve(path === "/research-edition/health" ? { status: "ready" } : { items: [], total: 0 }),
  );
});

afterEach(() => {
  localStorage.removeItem("finboard-lang");
  localStorage.removeItem("findashboard-theme");
});

describe("设置与首次研究", () => {
  it("旧键恢复语言和主题，切换即时生效并持续保存", async () => {
    localStorage.setItem("findashboard-theme", "light");
    const user = userEvent.setup();
    renderWithProviders(<Settings />);

    expect(await screen.findByText("数据源")).toBeInTheDocument();
    expect(screen.getByLabelText("界面主题")).toHaveValue("light");
    await user.selectOptions(screen.getByLabelText("界面语言"), "en");
    expect(screen.getByText("Data Source")).toBeInTheDocument();
    expect(screen.getByText("Scheduled Tasks")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Theme"), "dark");
    expect(document.documentElement).toHaveClass("dark");
    expect(localStorage.getItem("finboard-lang")).toBe("en");
    expect(localStorage.getItem("findashboard-theme")).toBe("dark");
  });

  it("保留编辑时版本，保存冲突时重读，下一次保存使用新版本", async () => {
    const user = userEvent.setup();
    const fresh = { ...config, sync_time: "15:35", revision: "b".repeat(64) };
    vi.mocked(api.updateConfig)
      .mockRejectedValueOnce(new ApiError(409, { code: "schedule_conflict" }, "Conflict"))
      .mockResolvedValueOnce({ ...fresh, sync_enabled: true, revision: "c".repeat(64) });
    renderWithProviders(<Settings />);
    const toggle = await screen.findByRole("switch", { name: "标的池同步开关" });
    const save = screen.getByRole("button", { name: "保存配置" });

    await user.click(toggle);
    await user.click(save);
    expect(api.updateConfig).toHaveBeenCalledWith(expect.objectContaining({ sync_enabled: true }), config.revision);
    expect(await screen.findByText("配置已由其他会话更新")).toBeInTheDocument();
    expect(save).toBeDisabled();

    vi.mocked(api.getConfig).mockResolvedValue(fresh);
    await user.click(screen.getByRole("button", { name: "重读服务器配置" }));
    expect(await screen.findByDisplayValue("15:35")).toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: "标的池同步开关" }));
    await user.click(save);
    expect(api.updateConfig).toHaveBeenLastCalledWith(expect.objectContaining({ sync_enabled: true }), fresh.revision);
    expect(await screen.findByText("调度偏好已保存到本机")).toBeInTheDocument();
  });

  it("空研究实例按 CLI 方式导入，刷新后从 Web 进入首轮研究", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Settings research />);
    expect(await screen.findByText(/当前没有可用发布/)).toBeInTheDocument();
    expect(screen.getByText(/import --bundle/)).toBeInTheDocument();
    expect(screen.getByText(/source-configure/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看连接与权限" })).toHaveAttribute("href", "/research/agent-connect");

    vi.mocked(fetchJSON).mockImplementation((path) =>
      Promise.resolve(path === "/research-edition/health" ? { status: "ready" } : { items: [{ status: "stale", release: { release_id: "synthetic-starter-v1" } }], total: 1 }),
    );
    await user.click(screen.getByRole("button", { name: "刷新诊断" }));
    expect(await screen.findByRole("link", { name: "打开研究课题" })).toHaveAttribute("href", "/research/topics");
    expect(screen.getByRole("link", { name: "打开策略 Studio" })).toHaveAttribute("href", "/research/strategy");
  });

  it("配置无权限和服务不可用独立提示，偏好仍可修改", async () => {
    vi.mocked(api.getConfig).mockRejectedValue(new ApiError(403, null, "Forbidden"));
    vi.mocked(api.getDataSourceStatus).mockRejectedValue(new ApiError(503, null, "Unavailable"));
    renderWithProviders(<Settings />);
    expect(await screen.findByText("缺少配置权限")).toBeInTheDocument();
    expect(screen.getByText("服务不可用")).toBeInTheDocument();
    expect(screen.getByLabelText("界面语言")).toBeEnabled();
  });
});
