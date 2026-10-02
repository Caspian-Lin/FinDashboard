import type { ReactElement } from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import StrategyExplanation from "@/components/research/StrategyExplanation";
import ResearchTopics from "./ResearchTopics";
import { workspaceApi } from "@/lib/research-workspace";

vi.mock("@/lib/research-workspace", async () => {
  const actual = await vi.importActual<typeof import("@/lib/research-workspace")>(
    "@/lib/research-workspace",
  );
  return {
    ...actual,
    workspaceApi: {
      explain: vi.fn(),
      decisions: vi.fn(),
      topics: vi.fn(),
      topic: vi.fn(),
      entries: vi.fn(),
      memories: vi.fn(),
      create: vi.fn(),
      update: vi.fn(),
      append: vi.fn(),
    },
  };
});
vi.mock("@/lib/api", () => ({ fetchJSON: vi.fn(() => Promise.resolve({ status: "missing" })) }));
function show(ui: ReactElement, route = "/research/topics") {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => vi.resetAllMocks());
describe("研究解释与课题的可核验界面", () => {
  it("按精确运行解释并有界请求决策，不把目标当成交", async () => {
    vi.mocked(workspaceApi.explain).mockResolvedValue({
      provenance: { version: 13 },
      spec: { name: "动量标签", universe: {}, signal_rules: {} },
      factors: [
        {
          name: "p_return_5d",
          node_id: "ret",
          sources: [],
          parameters: null,
          formula: "close / close[-5] - 1",
          unit: "比例",
          raw_direction: "higher",
          effective_direction: { score: "原值越低,输出分数越高" },
          evidence: "matched",
        },
      ],
      effective_policies: {},
      overrides: {},
      schedule: {},
      gaps: ["旧参数缺证据"],
      warnings: ["目标权重不等于实际成交"],
      mechanism_hypotheses: [],
    });
    vi.mocked(workspaceApi.decisions).mockImplementation(async (_id, params) => ({
      items: params?.symbol
        ? [
            {
              stage: "orders",
              item: { symbol: "A", status: "rejected", reject_reason: "volume_limit" },
            },
          ]
        : [{ decision_id: "D-1", business_date: "2026-09-22" }],
      has_more: false,
      offset: 0,
      limit: 20,
    }));
    show(<StrategyExplanation runId="RR-frozen" />);
    expect(await screen.findByText("目标权重不等于实际成交")).toBeInTheDocument();
    expect(screen.getByText(/本分支偏好较低原值/)).toBeInTheDocument();
    expect(screen.getByText("旧参数缺证据")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("决策日期"), { target: { value: "2026-09-22" } });
    fireEvent.change(screen.getByLabelText("标的代码"), { target: { value: "A" } });
    fireEvent.click(screen.getByRole("button", { name: "查看决策" }));
    expect(await screen.findByText("volume_limit")).toBeInTheDocument();
    expect(workspaceApi.explain).toHaveBeenCalledWith("RR-frozen", undefined, undefined);
    expect(workspaceApi.decisions).toHaveBeenCalledWith("RR-frozen", {
      business_date: "2026-09-22",
      symbol: "A",
      limit: "100",
      offset: "0",
    });
  });
  it("旧记忆保留纠正状态和无法验证的引用", async () => {
    vi.mocked(workspaceApi.topics).mockResolvedValue({
      items: [],
      has_more: false,
      offset: 0,
      limit: 20,
    });
    vi.mocked(workspaceApi.memories).mockResolvedValue({
      items: [
        {
          memory_id: "RM-old",
          memory_type: "note",
          excerpt: "历史故障，当前已修复",
          content_length: 10,
          created_by: "agent:mcp",
          created_at: "2026-09-01",
          confirmed_by: null,
          supersedes_id: null,
          status: "forgotten",
          source_refs: [{ kind: "unknown", ref_id: "" }],
          unverifiable_refs: true,
        },
      ],
      has_more: false,
      offset: 0,
      limit: 20,
    });
    show(<ResearchTopics />, "/research/topics?tab=memories");
    expect(await screen.findByText(/RM-old · 已纠正/)).toBeInTheDocument();
    expect(screen.getByText(/历史故障，当前已修复/)).toBeInTheDocument();
    expect(screen.getByText(/无法验证的来源/)).toBeInTheDocument();
    expect(screen.getByText(/未确认解释/)).toBeInTheDocument();
    await waitFor(() => expect(workspaceApi.memories).toHaveBeenCalledWith(0));
  });
});
