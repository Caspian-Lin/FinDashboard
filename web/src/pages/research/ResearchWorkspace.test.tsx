import type { ReactElement } from "react";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import StrategyExplanation from "@/components/research/StrategyExplanation";
import ResearchTopics from "./ResearchTopics";
import {
  workspaceApi,
  type Topic,
  type TopicContext,
} from "@/lib/research-workspace";
import { fetchJSON } from "@/lib/api";

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
      context: vi.fn(),
      branches: vi.fn(),
      refsCheck: vi.fn(),
      memories: vi.fn(),
      create: vi.fn(),
      createDraft: vi.fn(),
      update: vi.fn(),
      updateDraft: vi.fn(),
      activate: vi.fn(),
      transition: vi.fn(),
      append: vi.fn(),
    },
  };
});
vi.mock("@/lib/api", () => ({ fetchJSON: vi.fn(() => Promise.resolve({ status: "missing" })) }));
function show(ui: ReactElement, route = "/research/topics") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const result = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, client };
}
beforeEach(() => {
  vi.resetAllMocks();
  const empty = { items: [], has_more: false, offset: 0, limit: 20 };
  vi.mocked(workspaceApi.topics).mockResolvedValue(empty);
  vi.mocked(workspaceApi.memories).mockResolvedValue(empty);
  // Source probes read `status`; the topic data card reads the coverage shape.
  vi.mocked(fetchJSON).mockResolvedValue({
    status: "missing",
    topic_id: "RT-draft",
    releases: [],
    coverage: {
      release_count: 0,
      dataset_count: 0,
      min_coverage_pct: null,
      missing_sessions: 0,
      symbols_with_gaps: 0,
      denominators: [],
      resolved_by: "refs",
      warnings: [],
    },
    gaps: [],
    last_sync: null,
    next_step: "no_refs",
  });
});
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
    expect(await screen.findByText("RM-old")).toBeInTheDocument();
    expect(screen.getByText("已纠正/移除")).toBeInTheDocument();
    expect(screen.getByText("2026-09-01（时区未记录）")).toHaveAttribute("datetime", "2026-09-01");
    expect(screen.getByText(/历史故障，当前已修复/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("关联证据（1）"));
    expect(await screen.findByText(/无法验证的来源/)).toBeInTheDocument();
    expect(screen.getByText(/未确认解释/)).toBeInTheDocument();
    await waitFor(() => expect(workspaceApi.memories).toHaveBeenCalledWith(0));
  });
  it("完整记忆按 Markdown 阅读，显示北京时间与原始时间，不重复加载列表", async () => {
    const iso = "2026-10-02T16:35:07.174174Z";
    vi.mocked(fetchJSON).mockResolvedValue({
      memory_id: "RM-readable",
      memory_type: "insight",
      content:
        "【可信度复核记录】\n## 仍缺的证据\n- **同源 OOS**\n- 执行压力\n\n| 证据 | 状态 |\n| --- | --- |\n| 压力 | 待核查 |\n\n![外部图片](https://example.com/private-tracking.png)\n\n<script>bad()</script>",
      status: "active",
      source_refs: [],
      tags: ["research-round"],
      created_by: "agent:mcp",
      created_at: iso,
      updated_at: iso,
      confirmed_by: null,
      confirmed_at: null,
      supersedes_id: "RM-old",
      conversation_id: null,
    });
    show(<ResearchTopics />, "/research/topics?memory=RM-readable");
    expect(await screen.findByRole("heading", { name: "可信度复核记录" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "仍缺的证据" })).toBeInTheDocument();
    expect(screen.getByText("同源 OOS").tagName).toBe("STRONG");
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    const time = screen.getByText("2026/10/03 00:35:07（北京时间）");
    expect(time).toHaveAttribute("datetime", iso);
    expect(time).toHaveAttribute("title", iso);
    expect(screen.getByRole("link", { name: "查看被纠正的记忆：RM-old" })).toBeInTheDocument();
    expect(workspaceApi.memories).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "返回记忆列表" }));
    await waitFor(() => expect(workspaceApi.memories).toHaveBeenCalledWith(0));
  });

  const emptyPage = { items: [], has_more: false, offset: 0, limit: 20 };
  const draftTopic: Topic = {
    topic_id: "RT-draft",
    revision: 1,
    title: "先存问题",
    question: "哪些证据未核验?",
    goal: null,
    status: "draft",
    conclusion: "unknown",
    summary: "",
    open_questions: [],
    next_step: "",
    created_by: "user:api",
    created_at: "2026-11-09T08:00:00Z",
    updated_at: "2026-11-09T08:00:00Z",
  };
  const activeTopic: Topic = {
    ...draftTopic,
    revision: 2,
    status: "active",
    goal: {
      version: "v1",
      criteria: "年化15%",
      source: { kind: "document", ref_id: "ROADMAP.md" },
    },
  };
  function contextPayload(overrides: Partial<TopicContext> = {}): TopicContext {
    return {
      topic: {
        topic_id: "RT-draft",
        revision: 2,
        title: "先存问题",
        question: "哪些证据未核验?",
        status: "paused",
        conclusion: "unknown",
        summary: "",
        updated_at: "2026-11-09T08:00:00Z",
      },
      goal: activeTopic.goal,
      lifecycle: {
        since: "2026-11-09T08:00:00Z",
        last: {
          from: "active",
          to: "paused",
          reason: "等待数据",
          actor: "agent:mcp",
          action: "pause",
          at: "2026-11-09T09:00:00Z",
        },
      },
      open_questions: ["同源OOS仍缺"],
      next_step: "补OOS",
      recent_rounds: [
        {
          entry_id: "RE-1",
          branch: "main",
          outcome: "failed",
          objective: "核对冻结输入",
          conclusion_excerpt: "结论摘录",
          next_step: "下一步",
          created_at: "2026-11-09T08:00:00Z",
          created_by: "agent:mcp",
          evidence_level: "agent_interpretation",
        },
      ],
      active_links: [],
      dangling_refs: [
        {
          ref: { kind: "research_run", ref_id: "RR-missing" },
          status: "missing",
          entry_ids: ["RE-1"],
        },
      ],
      evidence_levels: {
        topic: "working_summary",
        rounds: "agent_interpretation",
        documents: "canonical_document",
      },
      next_actions: [{ action: "check_refs", why: "1个引用断链" }],
      generated_at: "2026-11-09T10:00:00Z",
      revision: 2,
      excerpted: true,
      bounds: {
        rounds: 10,
        refs: 10,
        total_rounds: 40,
        max_rounds: 20,
        max_refs: 10,
        excerpt_chars: 300,
        max_bytes: 65536,
      },
      ...overrides,
    };
  }

  it("两步建题：先保存草稿，再补齐目标激活", async () => {
    vi.mocked(workspaceApi.createDraft).mockResolvedValue(draftTopic);
    vi.mocked(workspaceApi.topic).mockResolvedValue(draftTopic);
    vi.mocked(workspaceApi.entries).mockResolvedValue(emptyPage);
    vi.mocked(workspaceApi.context).mockResolvedValue(contextPayload());
    vi.mocked(workspaceApi.activate).mockResolvedValue(activeTopic);
    show(<ResearchTopics />);
    fireEvent.click(await screen.findByRole("button", { name: "新建课题" }));
    expect(screen.getByText("第一步：保存研究问题草稿")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("课题标题"), { target: { value: "先存问题" } });
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "哪些证据未核验?" } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(workspaceApi.createDraft).toHaveBeenCalled());
    expect(
      await screen.findByText("尚未补齐目标版本与验收门槛；补齐并激活前不能发起研究。"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "补齐目标并激活" }));
    fireEvent.change(screen.getByLabelText("目标版本"), { target: { value: "v1" } });
    fireEvent.change(screen.getByLabelText("目标来源（研究文档相对路径）"), {
      target: { value: "ROADMAP.md" },
    });
    fireEvent.change(screen.getByLabelText("目标和验收门槛"), { target: { value: "年化15%" } });
    fireEvent.click(screen.getByRole("button", { name: "激活课题" }));
    await waitFor(() =>
      expect(workspaceApi.activate).toHaveBeenCalledWith(
        "RT-draft",
        expect.objectContaining({
          goal: expect.objectContaining({
            version: "v1",
            criteria: "年化15%",
            source: { kind: "document", ref_id: "ROADMAP.md" },
          }),
        }),
        1,
      ),
    );
  });

  it("版本冲突展示当前版本与变化字段，并允许重新加载", async () => {
    vi.mocked(workspaceApi.topic).mockResolvedValue(activeTopic);
    vi.mocked(workspaceApi.entries).mockResolvedValue(emptyPage);
    vi.mocked(workspaceApi.context).mockResolvedValue(contextPayload());
    vi.mocked(workspaceApi.update).mockRejectedValue({
      status: 409,
      detail: {
        code: "topic_revision_conflict",
        current_revision: 3,
        your_revision: 2,
        changed_fields: ["status", "goal"],
        current_summary: { status: "paused", revision: 3, goal_version: "v2" },
      },
    });
    show(<ResearchTopics />, "/research/topics?topic=RT-draft");
    fireEvent.click(await screen.findByRole("button", { name: "更新记录" }));
    fireEvent.click(screen.getByRole("button", { name: "保存研究记录" }));
    expect(await screen.findByRole("dialog", { name: "课题冲突" })).toBeInTheDocument();
    expect(screen.getByText(/当前 revision 3，你的 revision 2/)).toBeInTheDocument();
    expect(screen.getByText(/变化的字段：status、goal/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新加载最新版本" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "课题冲突" })).not.toBeInTheDocument(),
    );
  });

  it("续接上下文展示建议下一步、最近轮次与断链", async () => {
    vi.mocked(workspaceApi.topic).mockResolvedValue(activeTopic);
    vi.mocked(workspaceApi.entries).mockResolvedValue(emptyPage);
    vi.mocked(workspaceApi.context).mockResolvedValue(contextPayload());
    show(<ResearchTopics />, "/research/topics?topic=RT-draft");
    expect(
      await screen.findByRole("heading", { name: "继续研究（跨客户端续接）" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/核验断链引用：1个引用断链/)).toBeInTheDocument();
    expect(screen.getByText("结论摘录")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/断链引用 1 条/);
    expect(screen.getByText(/已按边界截取/)).toBeInTheDocument();
    expect(screen.getByText(/最近生命周期：研究中 → 已暂停/)).toBeInTheDocument();
    expect(screen.queryByText("进行中的关联")).not.toBeInTheDocument();
  });

  it("打开表单后固定 base revision，后台刷新不会改写提交版本", async () => {
    vi.mocked(workspaceApi.topic).mockResolvedValue(activeTopic);
    vi.mocked(workspaceApi.entries).mockResolvedValue(emptyPage);
    vi.mocked(workspaceApi.context).mockResolvedValue(contextPayload());
    vi.mocked(workspaceApi.update).mockResolvedValue({ ...activeTopic, revision: 3 });
    const view = show(<ResearchTopics />, "/research/topics?topic=RT-draft");
    fireEvent.click(await screen.findByRole("button", { name: "更新记录" }));
    // A background refetch brings a newer revision into the page.
    vi.mocked(workspaceApi.topic).mockResolvedValue({ ...activeTopic, revision: 5 });
    await act(async () => {
      await view.client.refetchQueries({ queryKey: ["topic"] });
    });
    await waitFor(() =>
      expect(view.client.getQueryData<Topic>(["topic", "RT-draft"])?.revision).toBe(5),
    );
    fireEvent.click(screen.getByRole("button", { name: "保存研究记录" }));
    await waitFor(() => expect(workspaceApi.update).toHaveBeenCalled());
    expect(vi.mocked(workspaceApi.update).mock.calls[0][2]).toBe(2);
  });

  it("草稿可编辑且提交不携带目标，使用固定的 base revision", async () => {
    vi.mocked(workspaceApi.topic).mockResolvedValue(draftTopic);
    vi.mocked(workspaceApi.entries).mockResolvedValue(emptyPage);
    vi.mocked(workspaceApi.context).mockResolvedValue(contextPayload({ goal: null }));
    vi.mocked(workspaceApi.updateDraft).mockResolvedValue({ ...draftTopic, revision: 2 });
    show(<ResearchTopics />, "/research/topics?topic=RT-draft");
    fireEvent.click(await screen.findByRole("button", { name: "编辑草稿" }));
    fireEvent.change(screen.getByLabelText("课题标题"), { target: { value: "改过的草稿" } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿修改" }));
    await waitFor(() => expect(workspaceApi.updateDraft).toHaveBeenCalled());
    const [id, body, revision] = vi.mocked(workspaceApi.updateDraft).mock.calls[0];
    expect(id).toBe("RT-draft");
    expect(body).not.toHaveProperty("goal");
    expect(revision).toBe(1);
  });

  it("草稿冲突保留编辑内容且不提供追加轮次入口", async () => {
    vi.mocked(workspaceApi.topic).mockResolvedValue(draftTopic);
    vi.mocked(workspaceApi.entries).mockResolvedValue(emptyPage);
    vi.mocked(workspaceApi.context).mockResolvedValue(contextPayload({ goal: null }));
    vi.mocked(workspaceApi.updateDraft).mockRejectedValue({
      status: 409,
      detail: {
        code: "topic_revision_conflict",
        current_revision: 4,
        your_revision: 1,
        changed_fields: ["summary"],
      },
    });
    show(<ResearchTopics />, "/research/topics?topic=RT-draft");
    fireEvent.click(await screen.findByRole("button", { name: "编辑草稿" }));
    fireEvent.change(screen.getByLabelText("课题标题"), { target: { value: "我的草稿改动" } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿修改" }));
    expect(await screen.findByRole("dialog", { name: "课题冲突" })).toBeInTheDocument();
    expect(screen.getByLabelText("课题标题")).toHaveValue("我的草稿改动");
    expect(screen.queryByRole("button", { name: /仅保留我的说明/ })).not.toBeInTheDocument();
  });
});
