import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { LegacyResearchDataRedirect } from "./App";
import { navGroups } from "@/components/layout/nav-config";

function DataLanding() {
  const { search } = useLocation();
  return <div data-testid="data-page">{search || "(no query)"}</div>;
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

describe("导航配置(#512:研究组首位「数据基座」)", () => {
  it("「研究」组第一项是 /data,且使用 end 精确匹配", () => {
    const research = navGroups[0];
    expect(research.items[0].to).toBe("/data");
    expect(research.items[0].end).toBe(true);
    expect(research.items[0].label.zh).toBe("数据基座");
  });

  it("旧「数据与标的」入口已从高级研究工具移除,避免重复入口", () => {
    const tos = navGroups.flatMap((group) => group.items.map((item) => item.to));
    expect(tos).not.toContain("/research/data");
    expect(tos.filter((to) => to === "/data")).toHaveLength(1);
  });
});
