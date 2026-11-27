import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import AgentConnect from "./AgentConnect";
import { LegacyWorkbenchRedirect } from "./LegacyWorkbenchRedirect";
import { navGroups } from "@/components/layout/nav-config";

function renderPage(initialEntry = "/research/agent-connect") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/research/agent-connect" element={<AgentConnect />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("AgentConnect 外部 Agent 接入页(#520)", () => {
  it("展示 MCP 端点、凭据预设与外部 Harness 文档入口", () => {
    renderPage();
    expect(screen.getByText("Agent 接入")).toBeInTheDocument();
    expect(screen.getByText(/http:\/\/127\.0\.0\.1:8765\/mcp/)).toBeInTheDocument();
    expect(screen.getByText("readonly")).toBeInTheDocument();
    expect(screen.getByText("topic-writer")).toBeInTheDocument();
    expect(screen.getByText("data-operator")).toBeInTheDocument();
    expect(screen.getByText("docs/agent-integration.md")).toBeInTheDocument();
    expect(screen.getByText(".agents/skills/finboard-research/")).toBeInTheDocument();
    expect(screen.getByText(/内置研究工作台已退役/)).toBeInTheDocument();
  });

  it("保留 ?topic= 交接上下文并给出续接调用", () => {
    renderPage("/research/agent-connect?topic=RT-abc123");
    expect(screen.getAllByText(/RT-abc123/).length).toBeGreaterThan(0);
    expect(
      screen.getByText('finboard_topic_read(topic_id="RT-abc123", view="context")'),
    ).toBeInTheDocument();
    const back = screen.getByRole("link", { name: /回到课题/ });
    expect(back).toHaveAttribute("href", "/research/topics?topic=RT-abc123");
  });

  it("无 topic 时降级为通用交接说明,不伪造课题链接", () => {
    renderPage();
    expect(screen.getByText(/没有携带课题上下文/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /回到课题/ })).toBeNull();
  });

  it("说明命名卷不会被代码删除,并给出只读备份命令", () => {
    renderPage();
    expect(screen.getByText(/不会删除/)).toBeInTheDocument();
    expect(screen.getByText(/opencode-data:\/d:ro/)).toBeInTheDocument();
    expect(screen.getByText(/opencode-config:\/d:ro/)).toBeInTheDocument();
    expect(screen.getByText(/FINBOARD_OPENCODE_\*/)).toBeInTheDocument();
  });
});

describe("旧研究工作台路由迁移(#520)", () => {
  it("导航与首页入口只保留 /research/agent-connect", () => {
    const targets = navGroups.flatMap((group) => group.items.map((item) => item.to));
    expect(targets).toContain("/research/agent-connect");
    expect(targets).not.toContain("/research/workbench");
  });

  it("旧 /research/workbench 跳转保留 topic 查询参数", async () => {
    render(
      <MemoryRouter initialEntries={["/research/workbench?topic=RT-abc123"]}>
        <Routes>
          <Route path="/research/workbench" element={<LegacyWorkbenchRedirect />} />
          <Route path="/research/agent-connect" element={<AgentConnect />} />
        </Routes>
      </MemoryRouter>,
    );
    const back = await screen.findByRole("link", { name: /回到课题/ });
    expect(back).toHaveAttribute("href", "/research/topics?topic=RT-abc123");
  });
});
