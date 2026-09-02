import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { LanguageProvider, useT, useLanguage, readStoredLanguage, DOCUMENT_TITLES } from "./index";

function Probe() {
  const { t, tl } = useT();
  const { lang, setLanguage } = useLanguage();
  return (
    <div>
      <span data-testid="t">{t("settings.title")}</span>
      <span data-testid="tl">{tl({ zh: "持仓", en: "Positions" })}</span>
      <span data-testid="interp">{t("jobs.totalCount", { count: 7 })}</span>
      <span data-testid="missing">{t("no.such.key")}</span>
      <span data-testid="lang">{lang}</span>
      <button type="button" onClick={() => setLanguage(lang === "zh" ? "en" : "zh")}>
        switch
      </button>
    </div>
  );
}

describe("i18n 基础设施(issue #284)", () => {
  it("默认中文:未包裹 Provider 时 useT 也可独立工作", () => {
    localStorage.removeItem("finboard-lang");
    render(<Probe />);
    expect(screen.getByTestId("t")).toHaveTextContent("设置");
    expect(screen.getByTestId("tl")).toHaveTextContent("持仓");
    expect(screen.getByTestId("lang")).toHaveTextContent("zh");
  });

  it("t() 插值与缺 key 回退路径本身", () => {
    localStorage.removeItem("finboard-lang");
    render(<Probe />);
    expect(screen.getByTestId("interp")).toHaveTextContent("共 7 条");
    expect(screen.getByTestId("missing")).toHaveTextContent("no.such.key");
  });

  it("切到英文后取词即时生效,并持久化到 localStorage 与 <html lang>/<title>", async () => {
    localStorage.removeItem("finboard-lang");
    const user = userEvent.setup();
    render(
      <LanguageProvider>
        <Probe />
      </LanguageProvider>,
    );
    expect(screen.getByTestId("t")).toHaveTextContent("设置");
    expect(document.documentElement.lang).toBe("zh-CN");
    expect(document.title).toBe(DOCUMENT_TITLES.zh);

    await user.click(screen.getByRole("button", { name: "switch" }));

    expect(screen.getByTestId("t")).toHaveTextContent("Settings");
    expect(screen.getByTestId("tl")).toHaveTextContent("Positions");
    expect(screen.getByTestId("lang")).toHaveTextContent("en");
    expect(document.documentElement.lang).toBe("en");
    expect(document.title).toBe(DOCUMENT_TITLES.en);
    expect(localStorage.getItem("finboard-lang")).toBe("en");
    localStorage.removeItem("finboard-lang");
  });

  it("localStorage 已存 en 时初始即为英文", () => {
    localStorage.setItem("finboard-lang", "en");
    expect(readStoredLanguage()).toBe("en");
    render(
      <LanguageProvider>
        <Probe />
      </LanguageProvider>,
    );
    expect(screen.getByTestId("t")).toHaveTextContent("Settings");
    localStorage.removeItem("finboard-lang");
  });
});
