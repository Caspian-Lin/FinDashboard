import type { ReactElement } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { LanguageProvider } from "@/i18n";
import Settings from "./Settings";

vi.mock("../lib/api", () => ({
  api: {
    getConfig: vi.fn(() =>
      Promise.resolve({
        data_provider: "yfinance",
        sync_enabled: false,
        sync_time: "06:00",
        download_enabled: false,
        download_time: "18:00",
        download_lookback_days: 5,
        download_markets: ["a_share"],
        download_types: ["stock"],
      }),
    ),
    updateConfig: vi.fn((body: unknown) => Promise.resolve(body)),
  },
}));

function renderWithProviders(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <LanguageProvider>{ui}</LanguageProvider>
    </QueryClientProvider>,
  );
}

describe("设置页语言切换(issue #284)", () => {
  it("默认显示中文,切换到 English 后页面文案即时变为英文并持久化", async () => {
    localStorage.removeItem("finboard-lang");
    const user = userEvent.setup();
    renderWithProviders(<Settings />);

    expect(await screen.findByText("数据源")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("界面语言"), "en");

    expect(screen.getByText("Data Source")).toBeInTheDocument();
    expect(screen.getByText("Scheduled Tasks")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save Configuration" })).toBeInTheDocument();
    expect(localStorage.getItem("finboard-lang")).toBe("en");
    localStorage.removeItem("finboard-lang");
  });
});
