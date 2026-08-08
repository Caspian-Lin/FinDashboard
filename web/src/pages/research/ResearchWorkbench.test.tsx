import type { ReactElement } from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { describe, expect, it, vi, beforeEach } from "vitest";
import ResearchWorkbench from "./ResearchWorkbench";
import { ApiError } from "@/lib/api";
import type {
  ConversationOut,
  OpenCodeStatusOut,
  OpenCodeAccessOut,
  AgentEventOut,
} from "@/lib/opencode";

/* ---------- 测试夹具 ---------- */

const ACTIVE_CONV: ConversationOut = {
  conversation_id: "CONV-20260109-aaa1111122223333",
  opencode_session_id: "sess-active-001",
  agent_run_id: null,
  title: "因子假设探索",
  status: "active",
  agent_name: "finboard-researcher",
  model_ref: "gpt-4o",
  last_event_seq: 5,
  created_at: "2026-08-09T00:00:00Z",
  updated_at: "2026-08-09T01:00:00Z",
};

const COMPLETED_CONV: ConversationOut = {
  ...ACTIVE_CONV,
  conversation_id: "CONV-20260109-bbb1111122224444",
  opencode_session_id: "sess-done-002",
  title: "已完成会话",
  status: "completed",
};

const INTERRUPTED_CONV: ConversationOut = {
  ...ACTIVE_CONV,
  conversation_id: "CONV-20260109-ccc1111122225555",
  opencode_session_id: "sess-interrupted-003",
  title: "中断会话",
  status: "interrupted",
};

const STATUS_ON: OpenCodeStatusOut = {
  running: true,
  managed: true,
  pid: 12345,
  base_url: "http://127.0.0.1:4097",
  healthy: true,
  version: "1.18.15",
  started_at: "2026-08-09T00:00:00Z",
};

const ACCESS_OUT: OpenCodeAccessOut = {
  conversation_id: ACTIVE_CONV.conversation_id,
  opencode_session_id: ACTIVE_CONV.opencode_session_id,
  web_url: "http://127.0.0.1:4097",
  username: "opencode",
  password: "s3cret-pw-abc",
  agent_name: "finboard-researcher",
};

const EVENTS: AgentEventOut[] = [
  {
    seq: 1,
    type: "message",
    role: "assistant",
    payload: { text: "我来帮你分析这个因子。" },
    timestamp: "2026-08-09T00:30:00Z",
  },
  {
    seq: 2,
    type: "tool.call",
    role: null,
    payload: { name: "finboard.research_run.list" },
    timestamp: "2026-08-09T00:31:00Z",
  },
  {
    seq: 3,
    type: "tool.result",
    role: null,
    payload: { text: "找到 3 个研究运行。" },
    timestamp: "2026-08-09T00:31:30Z",
  },
];

/* ---------- API mock ---------- */

const mocks = vi.hoisted(() => ({
  status: vi.fn(),
  health: vi.fn(),
  access: vi.fn(),
  list: vi.fn(),
  get: vi.fn(),
  create: vi.fn(),
  history: vi.fn(),
  interrupt: vi.fn(),
  abort: vi.fn(),
}));

vi.mock("@/lib/opencode", () => ({
  conversationApi: {
    list: (...a: unknown[]) => mocks.list(...a),
    get: (...a: unknown[]) => mocks.get(...a),
    create: (...a: unknown[]) => mocks.create(...a),
    history: (...a: unknown[]) => mocks.history(...a),
    interrupt: (...a: unknown[]) => mocks.interrupt(...a),
    abort: (...a: unknown[]) => mocks.abort(...a),
  },
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
  mocks.list.mockResolvedValue([ACTIVE_CONV, COMPLETED_CONV, INTERRUPTED_CONV]);
  mocks.access.mockResolvedValue(ACCESS_OUT);
  mocks.history.mockResolvedValue(EVENTS);
  mocks.create.mockResolvedValue(ACTIVE_CONV);
  mocks.interrupt.mockResolvedValue(undefined);
  mocks.abort.mockResolvedValue(undefined);
});

/* ============================================================ */
/* 测试用例                                                      */
/* ============================================================ */

describe("ResearchWorkbench 降级开关", () => {
  it("网关 503(未启用)时显示降级 Alert 与跳转链接,不渲染 iframe", async () => {
    mocks.status.mockRejectedValue(new ApiError(503, "opencode web gateway disabled", "disabled"));
    renderWithProviders(<ResearchWorkbench />);
    await waitFor(() => {
      expect(screen.getByText(/OpenCode Web 工作台未启用/)).toBeInTheDocument();
    });
    expect(screen.getByText(/前往 AI 助手/)).toBeInTheDocument();
    // iframe 不应渲染
    expect(document.querySelector("iframe")).toBeNull();
    // 会话列表不应被请求
    expect(mocks.list).not.toHaveBeenCalled();
  });

  it("网关启用时显示研究边界提示与控制面", async () => {
    renderWithProviders(<ResearchWorkbench />);
    await waitFor(() => {
      expect(screen.getByText(/研究边界/)).toBeInTheDocument();
    });
    expect(screen.getByText("OpenCode Web")).toBeInTheDocument();
    expect(mocks.status).toHaveBeenCalled();
  });
});

describe("ResearchWorkbench 会话绑定", () => {
  it("只展示后端返回的会话列表,不存在手动输入 session_id 的入口", async () => {
    renderWithProviders(<ResearchWorkbench />);
    await waitFor(() => {
      expect(screen.getByText("因子假设探索")).toBeInTheDocument();
    });
    expect(screen.getByText("已完成会话")).toBeInTheDocument();
    expect(screen.getByText("中断会话")).toBeInTheDocument();
    // 不存在手动输入 session_id / opencode_session_id 的文本框
    const inputs = document.querySelectorAll("input[type='text'], input:not([type])");
    inputs.forEach((i) => {
      const ph = (i.getAttribute("placeholder") ?? i.getAttribute("name") ?? "").toLowerCase();
      expect(ph).not.toContain("session");
    });
  });

  it("选中 ACTIVE 会话后调用 access 签发凭证并嵌入 iframe", async () => {
    const { container } = renderWithProviders(<ResearchWorkbench />);
    await screen.findByText("因子假设探索");
    fireEvent.click(screen.getByText("因子假设探索"));
    // 等待 iframe 渲染(access 数据就绪后)
    const iframe = await waitFor(() => {
      const el = container.querySelector("iframe") as HTMLIFrameElement | null;
      expect(el).not.toBeNull();
      return el!;
    });
    expect(mocks.access).toHaveBeenCalledWith(ACTIVE_CONV.conversation_id);
    // iframe src 应指向隔离 OpenCode Web 实例,且携带凭证
    expect(iframe.src).toContain("127.0.0.1:4097");
    expect(iframe.src).toContain(encodeURIComponent("opencode"));
    expect(iframe.src).toContain(encodeURIComponent("s3cret-pw-abc"));
    // conversation_id 作为审计查询参数
    expect(iframe.src).toContain(`conv=${ACTIVE_CONV.conversation_id}`);
  });

  it("凭证(密码)不出现在可见 DOM 文本中", async () => {
    const { container } = renderWithProviders(<ResearchWorkbench />);
    await screen.findByText("因子假设探索");
    fireEvent.click(screen.getByText("因子假设探索"));
    await waitFor(() => expect(container.querySelector("iframe")).not.toBeNull());
    // 明文密码不应作为文本节点渲染
    expect(screen.queryByText("s3cret-pw-abc")).toBeNull();
  });

  it("非 ACTIVE 会话(如 completed)不可打开工作台,提示状态", async () => {
    renderWithProviders(<ResearchWorkbench />);
    await screen.findByText("已完成会话");
    fireEvent.click(screen.getByText("已完成会话"));
    await waitFor(() => {
      expect(screen.getByText(/会话状态:completed/)).toBeInTheDocument();
    });
    // 不应签发凭证
    expect(mocks.access).not.toHaveBeenCalled();
    expect(document.querySelector("iframe")).toBeNull();
  });
});

describe("ResearchWorkbench 未授权 / 跨用户访问", () => {
  it("access 返回 403 时显示未授权错误,不渲染 iframe", async () => {
    mocks.access.mockRejectedValue(
      new ApiError(403, "conversation not authorized for opencode access", "forbidden"),
    );
    renderWithProviders(<ResearchWorkbench />);
    await screen.findByText("因子假设探索");
    fireEvent.click(screen.getByText("因子假设探索"));
    await waitFor(() => {
      expect(screen.getByText(/工作台访问未授权/)).toBeInTheDocument();
    });
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("access 返回 403 且消息不含 403 时显示通用错误", async () => {
    mocks.access.mockRejectedValue(new ApiError(500, "boom", "boom"));
    renderWithProviders(<ResearchWorkbench />);
    await screen.findByText("因子假设探索");
    fireEvent.click(screen.getByText("因子假设探索"));
    await waitFor(() => {
      expect(screen.getByText(/工作台访问未授权/)).toBeInTheDocument();
      expect(screen.getByText(/boom/)).toBeInTheDocument();
    });
    expect(document.querySelector("iframe")).toBeNull();
  });
});

describe("ResearchWorkbench 关键事件摘要", () => {
  it("事件摘要显示 message/tool 类型,不展示 token 级 delta", async () => {
    mocks.history.mockResolvedValue([
      ...EVENTS,
      {
        seq: 99,
        type: "message.part", // token 级增量 —— 不在后端 KEY_EVENT_TYPES,前端过滤
        role: "assistant",
        payload: { text: "token-delta-should-not-appear" },
        timestamp: "2026-08-09T00:32:00Z",
      },
    ]);
    const { container } = renderWithProviders(<ResearchWorkbench />);
    await screen.findByText("因子假设探索");
    fireEvent.click(screen.getByText("因子假设探索"));
    // 等待 iframe 渲染(意味着 access + WorkbenchPane 已挂载)
    await waitFor(() => expect(container.querySelector("iframe")).not.toBeNull());
    // 展开事件摘要折叠面板
    const trigger = screen.getByText(/关键事件摘要/);
    fireEvent.click(trigger);
    // message / tool 类型的摘要应展示
    await waitFor(() => {
      expect(screen.getByText(/我来帮你分析这个因子/)).toBeInTheDocument();
      expect(screen.getByText(/找到 3 个研究运行/)).toBeInTheDocument();
    });
    // token 级 delta 被过滤,不展示
    expect(screen.queryByText("token-delta-should-not-appear")).toBeNull();
  });

  it("无关键事件时展示空提示,说明 token 级增量不落库", async () => {
    mocks.history.mockResolvedValue([]);
    const { container } = renderWithProviders(<ResearchWorkbench />);
    await screen.findByText("因子假设探索");
    fireEvent.click(screen.getByText("因子假设探索"));
    await waitFor(() => expect(container.querySelector("iframe")).not.toBeNull());
    const trigger = screen.getByText(/关键事件摘要/);
    fireEvent.click(trigger);
    await waitFor(() => {
      expect(screen.getByText(/token 级增量不落库/)).toBeInTheDocument();
    });
  });
});

describe("ResearchWorkbench 中断 / 中止", () => {
  it("点击中断按钮调用 interrupt API", async () => {
    const { container } = renderWithProviders(<ResearchWorkbench />);
    await screen.findByText("因子假设探索");
    fireEvent.click(screen.getByText("因子假设探索"));
    await waitFor(() => expect(container.querySelector("iframe")).not.toBeNull());
    const interruptBtn = screen.getByTestId("wb-interrupt");
    fireEvent.click(interruptBtn);
    await waitFor(() => {
      expect(mocks.interrupt).toHaveBeenCalledWith(ACTIVE_CONV.conversation_id);
    });
  });

  it("点击中止按钮调用 abort API", async () => {
    const { container } = renderWithProviders(<ResearchWorkbench />);
    await screen.findByText("因子假设探索");
    fireEvent.click(screen.getByText("因子假设探索"));
    await waitFor(() => expect(container.querySelector("iframe")).not.toBeNull());
    const abortBtn = screen.getByTestId("wb-abort");
    fireEvent.click(abortBtn);
    await waitFor(() => {
      expect(mocks.abort).toHaveBeenCalledWith(ACTIVE_CONV.conversation_id);
    });
  });
});

describe("ResearchWorkbench 创建会话", () => {
  it("点击新建会话调用 create API 并选中", async () => {
    const { container } = renderWithProviders(<ResearchWorkbench />);
    await screen.findByText("因子假设探索");
    const createBtn = screen.getByRole("button", { name: /新建研究会话/ });
    fireEvent.click(createBtn);
    await waitFor(() => {
      expect(mocks.create).toHaveBeenCalled();
    });
    // 创建后应选中并签发凭证,渲染 iframe
    await waitFor(() => {
      expect(container.querySelector("iframe")).not.toBeNull();
      expect(mocks.access).toHaveBeenCalledWith(ACTIVE_CONV.conversation_id);
    });
  });

  it("新建会话区域明确标注不启动 ResearchRun/回测/模拟盘", async () => {
    renderWithProviders(<ResearchWorkbench />);
    await screen.findByText("因子假设探索");
    expect(
      screen.getByText(/创建研究会话不会启动 ResearchRun、回测或模拟盘/),
    ).toBeInTheDocument();
  });
});
