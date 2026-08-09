import type { ReactElement } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { describe, expect, it, vi, beforeEach } from "vitest";
import ResearchWorkbench from "./ResearchWorkbench";
import { ApiError } from "@/lib/api";
import type { OpenCodeStatusOut, OpenCodeAccessOut } from "@/lib/opencode";

/* ---------- 测试夹具 ---------- */

const STATUS_ON: OpenCodeStatusOut = {
  running: true,
  managed: true,
  container_id: "abc123def456",
  base_url: "http://127.0.0.1:4097",
  healthy: true,
  version: "1.18.15",
  started_at: "2026-08-09T00:00:00Z",
};

const ACCESS_OUT: OpenCodeAccessOut = {
  web_url: "http://127.0.0.1:4097",
  username: "opencode",
  password: "s3cret-pw-abc",
  agent_name: "finboard-researcher",
};

/* ---------- API mock ---------- */

const mocks = vi.hoisted(() => ({
  status: vi.fn(),
  health: vi.fn(),
  access: vi.fn(),
}));

vi.mock("@/lib/opencode", () => ({
  opencodeGatewayApi: {
    status: (...a: unknown[]) => mocks.status(...a),
    health: (...a: unknown[]) => mocks.health(...a),
    access: (...a: unknown[]) => mocks.access(...a),
  },
}));

function renderWithProviders(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TooltipProvider>{ui}</TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.status.mockResolvedValue(STATUS_ON);
  mocks.access.mockResolvedValue(ACCESS_OUT);
});

/* ============================================================ */
/* 测试用例                                                      */
/* ============================================================ */

describe("ResearchWorkbench 降级开关", () => {
  it("网关 503(未启用)时工作台 Tab 显示降级提示,审批中心 Tab 仍可用", async () => {
    mocks.status.mockRejectedValue(
      new ApiError(503, "opencode web gateway disabled", "disabled"),
    );
    renderWithProviders(<ResearchWorkbench />);
    await waitFor(() => {
      expect(screen.getByText(/OpenCode Web 网关未启用/)).toBeInTheDocument();
    });
    expect(screen.getAllByText(/审批中心/).length).toBeGreaterThanOrEqual(1);
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("网关启用时显示研究边界提示与状态 banner", async () => {
    renderWithProviders(<ResearchWorkbench />);
    await waitFor(() => {
      expect(screen.getByText(/研究边界/)).toBeInTheDocument();
    });
    expect(screen.getByText("OpenCode Web")).toBeInTheDocument();
    expect(mocks.status).toHaveBeenCalled();
  });
});

describe("ResearchWorkbench iframe 直连", () => {
  it("网关启用后自动签发凭证并嵌入 iframe", async () => {
    const { container } = renderWithProviders(<ResearchWorkbench />);
    // access() 无参数 —— #121 重构后不再绑定 conversation
    await waitFor(() => expect(mocks.access).toHaveBeenCalledWith());
    const iframe = await waitFor(() => {
      const el = container.querySelector("iframe") as HTMLIFrameElement | null;
      expect(el).not.toBeNull();
      return el!;
    });
    // iframe src 应指向隔离 OpenCode Web 实例,且携带凭证
    expect(iframe.src).toContain("127.0.0.1:4097");
    expect(iframe.src).toContain(encodeURIComponent("opencode"));
    expect(iframe.src).toContain(encodeURIComponent("s3cret-pw-abc"));
    // 强制传 directory=/workspace(避免 SPA localStorage 缓存的宿主机路径导致 ENOENT)
    expect(iframe.src).toContain("directory=%2Fworkspace");
  });

  it("凭证(密码)不出现在可见 DOM 文本中", async () => {
    const { container } = renderWithProviders(<ResearchWorkbench />);
    await waitFor(() => expect(container.querySelector("iframe")).not.toBeNull());
    expect(screen.queryByText("s3cret-pw-abc")).toBeNull();
  });

  it("access 失败时显示错误,不渲染 iframe", async () => {
    mocks.access.mockRejectedValue(new ApiError(500, "boom", "boom"));
    renderWithProviders(<ResearchWorkbench />);
    await waitFor(() => {
      expect(screen.getByText(/工作台访问未授权/)).toBeInTheDocument();
      expect(screen.getByText(/boom/)).toBeInTheDocument();
    });
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("状态 banner 展示容器 ID 与版本", async () => {
    renderWithProviders(<ResearchWorkbench />);
    await waitFor(() => {
      expect(screen.getByText("abc123def456")).toBeInTheDocument();
      expect(screen.getByText(/v1\.18\.15/)).toBeInTheDocument();
    });
  });
});
