import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CatalogEntry, CatalogRelease, CoverageReport, DatasetDetail, TopicCoverage } from "@/lib/data-catalog";
import DataBase from "./DataBase";

const catalogMock = vi.hoisted(() => ({
  list: vi.fn(),
  dataset: vi.fn(),
  coverage: vi.fn(),
  release: vi.fn(),
  releaseInstruments: vi.fn(),
  references: vi.fn(),
  calendar: vi.fn(),
  topicCoverage: vi.fn(),
}));

vi.mock("@/lib/data-catalog", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/data-catalog")>();
  return { ...actual, catalogApi: catalogMock };
});

vi.mock("@/components/data/ResearchDataOps", () => ({
  default: (props: { embedded?: boolean; sub?: string }) => (
    <div data-testid="legacy-ops">
      旧操作面板 embedded={String(props.embedded)} sub={props.sub ?? "bars"}
    </div>
  ),
}));

function makeRelease(partial: Partial<CatalogRelease> = {}): CatalogRelease {
  return {
    release_id: "DR-512",
    dataset_name: "test-release",
    source: "tushare",
    version: "v1",
    dataset_kind: "daily_metrics",
    schema_version: "v1",
    start_date: "2024-01-02",
    end_date: "2024-12-31",
    adjustment: "none",
    symbol_count: 2,
    row_count: 400,
    coverage_pct: 0.98,
    quality_status: "passed",
    published_at: "2026-11-01T08:00:00Z",
    release_checksum: "a".repeat(64),
    storage_uri: "data_releases/DR-512",
    capabilities: [{ key: "stock", missing_requirements: [] }],
    ...partial,
  };
}

function makeEntry(partial: Partial<CatalogEntry> & Pick<CatalogEntry, "key" | "title" | "title_en" | "status">): CatalogEntry {
  return {
    storage: "research_table",
    publishable: true,
    source: "tushare",
    release: null,
    batch: null,
    coverage_pct: 0.98,
    symbol_count: 2,
    row_count: 400,
    denominator: "发布 expected_sessions x 发布标的数",
    local_start: "2024-01-02",
    local_end: "2024-12-31",
    updated_at: "2026-11-01T08:00:00Z",
    cache_files: 0,
    cache_bytes: 0,
    warnings: [],
    ...partial,
  };
}

const entries: CatalogEntry[] = [
  makeEntry({
    key: "bars",
    title: "行情 bars",
    title_en: "Market bars",
    status: "usable",
    release: makeRelease({ release_id: "DR-BARS", dataset_kind: "bars", dataset_name: "mixed" }),
  }),
  makeEntry({
    key: "daily_metrics",
    title: "每日指标",
    title_en: "Daily metrics",
    status: "ingested_not_published",
    release: null,
    coverage_pct: null,
    warnings: ["尚无可用发布"],
  }),
  makeEntry({
    key: "financial_indicators",
    title: "财务指标",
    title_en: "Financial indicators",
    status: "blocked",
    release: null,
    coverage_pct: null,
    warnings: ["quality gate rejected"],
  }),
  makeEntry({
    key: "profiles",
    title: "标的档案",
    title_en: "Instrument profiles",
    status: "not_ingested",
    release: null,
    coverage_pct: null,
    publishable: false,
  }),
];

const coverage: CoverageReport = {
  dataset: "daily_metrics",
  denominator: "发布 expected_sessions x 发布标的数",
  release: null,
  batch: null,
  coverage_pct: 0.98,
  expected_sessions: 400,
  missing_sessions: 2,
  symbols_with_gaps: 1,
  missing_preview: [],
  warnings: [],
};

const detail: DatasetDetail = {
  entry: entries[1],
  fields: ["trade_date", "close"],
  unit_notes: ["市值/股本为原始股数与元;比率为小数比例"],
  pit_rule: "trade_date 17:00 Asia/Shanghai(T 日收盘后)",
  calendar_market: "SSE",
  adjustment: null,
  publishable: true,
  releases: [makeRelease()],
  batches: [
    {
      id: 7,
      dataset: "daily_metrics",
      source: "tushare",
      dataset_version: "daily_metrics:2024-01-02",
      status: "failed",
      quality_status: "rejected",
      expected_rows: 100,
      received_rows: 90,
      accepted_rows: 80,
      rejected_rows: 10,
      parameters: {},
      error_summary: "quality gate rejected",
      started_at: "2026-11-01T07:00:00Z",
      completed_at: "2026-11-01T07:05:00Z",
      published_at: null,
    },
  ],
  failures: [],
};

const topicCoverage: TopicCoverage = {
  topic_id: "RT-512",
  releases: [
    {
      release_id: "DR-512",
      dataset: "daily_metrics",
      dataset_name: "test-release",
      source: "tushare",
      version: "v1",
      start_date: "2024-01-02",
      end_date: "2024-12-31",
      published_at: "2026-11-01T08:00:00Z",
      quality_status: "passed",
      coverage_pct: 0.75,
      expected_sessions: 400,
      missing_sessions: 100,
      symbols_with_gaps: 2,
      denominator: "发布 expected_sessions x 发布标的数",
      warnings: [],
    },
  ],
  coverage: {
    release_count: 1,
    dataset_count: 1,
    min_coverage_pct: 0.75,
    missing_sessions: 100,
    symbols_with_gaps: 2,
    denominators: ["发布 expected_sessions x 发布标的数"],
    resolved_by: "refs",
    warnings: [],
  },
  gaps: [
    {
      release_id: "DR-512",
      dataset: "daily_metrics",
      reason: "coverage_gap",
      message: "2 个标的缺 100 个期望交易日",
      coverage_pct: 0.75,
      missing_sessions: 100,
      symbols_with_gaps: 2,
    },
  ],
  last_sync: "2026-11-01T08:00:00Z",
  next_step: "fix_gaps",
};

function renderPage(path: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <TooltipProvider>
          <DataBase />
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  catalogMock.list.mockResolvedValue({ items: entries, total: entries.length, limit: 200, offset: 0, generated_at: "2026-11-09T00:00:00Z" });
  catalogMock.dataset.mockResolvedValue(detail);
  catalogMock.coverage.mockResolvedValue(coverage);
  catalogMock.release.mockResolvedValue(makeRelease());
  catalogMock.releaseInstruments.mockResolvedValue({ release_id: "DR-512", items: [], total: 0, limit: 20, offset: 0 });
  catalogMock.references.mockResolvedValue({ release_id: "DR-512", items: [] });
  catalogMock.topicCoverage.mockResolvedValue(topicCoverage);
});

describe("数据基座页面(#512)", () => {
  it("默认渲染概览四个区:状态卡、缺口摘要、最近发布、快捷入口", async () => {
    renderPage("/data");
    expect(await screen.findByRole("heading", { level: 1, name: "数据基座" })).toBeInTheDocument();
    const summary = await screen.findByLabelText("目录状态");
    expect(within(summary).getByText("可研究")).toBeInTheDocument();
    expect(within(summary).getByText("已同步 · 待发布")).toBeInTheDocument();
    expect(screen.getByText("全局缺口摘要")).toBeInTheDocument();
    expect(screen.getByText("最近发布")).toBeInTheDocument();
    expect(screen.getByText("快捷入口")).toBeInTheDocument();
    // 最近发布里的 bars 发布可直接点开
    expect(screen.getByText("DR-BARS")).toBeInTheDocument();
    // 四个一级 tab 都存在
    for (const label of ["概览", "数据集", "同步与发布", "技术详情"]) {
      expect(screen.getByRole("tab", { name: label })).toBeInTheDocument();
    }
  });

  it("键盘(ArrowRight)可切换一级 tab,焦点与 aria-selected 同步", async () => {
    renderPage("/data");
    const overview = await screen.findByRole("tab", { name: "概览" });
    overview.focus();
    fireEvent.keyDown(overview, { key: "ArrowRight", code: "ArrowRight" });
    await waitFor(() => expect(screen.getByRole("tab", { name: "数据集" })).toHaveAttribute("aria-selected", "true"));
    expect(screen.getByRole("tab", { name: "概览" })).toHaveAttribute("aria-selected", "false");
  });

  it("数据集列表按服务端参数过滤,详情按钮打开抽屉", async () => {
    const user = userEvent.setup();
    renderPage("/data?tab=datasets&status=blocked");
    await waitFor(() =>
      expect(catalogMock.list).toHaveBeenCalledWith(
        expect.objectContaining({ status: "blocked", limit: 20, offset: 0 }),
      ),
    );
    expect(await screen.findByText("财务指标")).toBeInTheDocument();
    // 表格窄屏横向滚动由 Table 容器与 min-w 提供
    const table = document.querySelector("table");
    expect(table?.className).toContain("min-w-[60rem]");
    // 页签条在窄屏可横向滚动,不折行(主 tablist 为第一个;数据集内还有视图切换)
    expect(screen.getAllByRole("tablist")[0].className).toContain("overflow-x-auto");

    await user.click(screen.getByRole("button", { name: /查看 财务指标 详情/ }));
    expect(await screen.findByText("覆盖与缺口")).toBeInTheDocument();
    expect(await screen.findByText("trade_date 17:00 Asia/Shanghai(T 日收盘后)")).toBeInTheDocument();
    expect(screen.getByText("最近同步批次")).toBeInTheDocument();
  });

  it("旧深链参数保全:dataset 过滤 + release 抽屉直接打开", async () => {
    renderPage("/data?tab=datasets&dataset=daily_metrics&release=DR-512");
    await waitFor(() =>
      expect(catalogMock.list).toHaveBeenCalledWith(
        expect.objectContaining({ dataset: "daily_metrics" }),
      ),
    );
    expect(await screen.findByText("DR-512")).toBeInTheDocument();
    // 发布详情里的引用区:空引用状态 + 限权说明
    expect(await screen.findByText("暂无引用")).toBeInTheDocument();
    expect(screen.getByText(/限权分页/)).toBeInTheDocument();
  });

  it("旧 /data?tab= 别名深链在页面内映射到新数据集 tab", async () => {
    renderPage("/data?tab=metrics&release=DR-512");
    await waitFor(() =>
      expect(catalogMock.list).toHaveBeenCalledWith(
        expect.objectContaining({ dataset: "daily_metrics" }),
      ),
    );
    expect(await screen.findByText("DR-512")).toBeInTheDocument();
  });

  it("新式 ?release= 深链缺 tab 时落数据集 tab 并推断 dataset", async () => {
    renderPage("/data?release=DR-512");
    await waitFor(() =>
      expect(catalogMock.list).toHaveBeenCalledWith(
        expect.objectContaining({ dataset: "daily_metrics" }),
      ),
    );
    // 抽屉打开时页面主体被 aria-hidden,直接读 DOM 的选中态。
    const selected = document.querySelector('[role="tab"][aria-selected="true"]');
    expect(selected?.textContent).toContain("数据集");
  });

  it("空目录显示空状态,不渲染表格", async () => {
    catalogMock.list.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0, generated_at: "2026-11-09T00:00:00Z" });
    renderPage("/data?tab=datasets");
    expect(await screen.findByText("没有匹配的数据集")).toBeInTheDocument();
    expect(document.querySelector("table")).toBeNull();
  });

  it("目录接口失败显示错误与重试,重试再次请求", async () => {
    const user = userEvent.setup();
    catalogMock.list.mockRejectedValueOnce(new Error("catalog down")).mockResolvedValue({
      items: entries,
      total: entries.length,
      limit: 200,
      offset: 0,
      generated_at: "2026-11-09T00:00:00Z",
    });
    renderPage("/data");
    expect(await screen.findByText("catalog down")).toBeInTheDocument();
    const calls = catalogMock.list.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(catalogMock.list.mock.calls.length).toBeGreaterThan(calls));
    expect(await screen.findByText("全局缺口摘要")).toBeInTheDocument();
  });

  it("同步与发布是可用的只读状态面板,#511 接入位禁用", async () => {
    const user = userEvent.setup();
    renderPage("/data");
    await user.click(await screen.findByRole("tab", { name: "同步与发布" }));
    expect(await screen.findByText("第一阶段:只读状态面板")).toBeInTheDocument();
    expect(screen.getByText("补数请求 · 接入位")).toBeInTheDocument();
    const pending = screen.getByRole("button", { name: "范围预检(待接入)" });
    expect(pending).toBeDisabled();
    expect(screen.getByRole("button", { name: "提交补数请求(待接入)" })).toBeDisabled();
    // 既有入口链接可达(行情拉取与发布向导)
    expect(screen.getByRole("link", { name: /行情拉取与发布向导/ })).toHaveAttribute(
      "href",
      "/data?tab=datasets&view=ops&sub=bars",
    );
  });

  it("同步与发布:首加载显示加载态,不渲染「全部可研究」健康文案", async () => {
    let resolveList: (value: unknown) => void = () => {};
    catalogMock.list.mockReturnValue(new Promise((resolve) => { resolveList = resolve; }));
    renderPage("/data?tab=sync");
    await waitFor(() => expect(document.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0));
    expect(screen.queryByText("全部数据集均处于可研究状态。")).toBeNull();
    // 收尾:让挂起请求落地,避免测试环境卸载时等待未决 query。
    resolveList({ items: entries, total: entries.length, limit: 200, offset: 0, generated_at: "2026-11-09T00:00:00Z" });
    await waitFor(() => expect(document.querySelectorAll(".animate-pulse").length).toBe(0));
  });

  it("同步与发布:目录失败显示错误与重试,不渲染健康文案", async () => {
    const user = userEvent.setup();
    catalogMock.list.mockRejectedValue(new Error("sync status down"));
    renderPage("/data?tab=sync");
    expect(await screen.findByText("sync status down")).toBeInTheDocument();
    expect(screen.queryByText("全部数据集均处于可研究状态。")).toBeNull();
    const calls = catalogMock.list.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(catalogMock.list.mock.calls.length).toBeGreaterThan(calls));
  });

  it("顶部刷新重取数据集列表(不只刷概览)", async () => {
    const user = userEvent.setup();
    renderPage("/data?tab=datasets");
    await waitFor(() =>
      expect(catalogMock.list).toHaveBeenCalledWith(expect.objectContaining({ limit: 20 })),
    );
    const pageCalls = () =>
      catalogMock.list.mock.calls.filter(([args]) => (args as { limit?: number })?.limit === 20).length;
    const before = pageCalls();
    await user.click(screen.getByRole("button", { name: "刷新目录" }));
    await waitFor(() => expect(pageCalls()).toBeGreaterThan(before));
  });

  it("顶部刷新重取技术详情的独立查询", async () => {
    const user = userEvent.setup();
    renderPage("/data?tab=tech");
    await waitFor(() => expect(catalogMock.dataset).toHaveBeenCalled());
    const before = catalogMock.dataset.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "刷新目录" }));
    await waitFor(() => expect(catalogMock.dataset.mock.calls.length).toBeGreaterThan(before));
  });

  it("数据集 tab 可切到旧操作面板子视图并保持 sub 参数", async () => {
    const user = userEvent.setup();
    renderPage("/data?tab=datasets&view=ops&sub=metrics");
    const legacy = await screen.findByTestId("legacy-ops");
    expect(legacy).toHaveTextContent("embedded=true");
    expect(legacy).toHaveTextContent("sub=metrics");
    await user.click(screen.getByRole("tab", { name: "数据目录" }));
    expect(await screen.findByText("筛选")).toBeInTheDocument();
  });

  it("技术详情展示质量/分区/批次/manifest 与任务日志入口", async () => {
    const user = userEvent.setup();
    renderPage("/data");
    await user.click(await screen.findByRole("tab", { name: "技术详情" }));
    expect(await screen.findByText("质量与状态")).toBeInTheDocument();
    expect(screen.getByText("分区与口径")).toBeInTheDocument();
    expect(screen.getAllByText("批次").length).toBeGreaterThan(0);
    expect(screen.getByText("Manifest 入口")).toBeInTheDocument();
    expect(screen.getByText("trade_date 17:00 Asia/Shanghai(T 日收盘后)")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /同步任务日志/ })[0]).toHaveAttribute(
      "href",
      "/jobs?kind=dataset_sync",
    );
    // 批次表展示失败批次的质量状态
    expect(screen.getAllByText("rejected").length).toBeGreaterThan(0);
  });

  it("课题数据视图:?topic= 命中发布集合与缺口", async () => {
    renderPage("/data?topic=RT-512");
    expect(await screen.findByText("课题数据范围")).toBeInTheDocument();
    expect(await screen.findByText(/1 份发布 · 1 个数据集/)).toBeInTheDocument();
    expect(screen.getByText("覆盖缺口")).toBeInTheDocument();
    expect(screen.getByText("有覆盖缺口,建议补数")).toBeInTheDocument();
    expect(catalogMock.topicCoverage).toHaveBeenCalledWith("RT-512");
  });

  it("课题数据视图:无引用显示空状态与范围预检链接", async () => {
    catalogMock.topicCoverage.mockResolvedValue({
      ...topicCoverage,
      releases: [],
      gaps: [],
      coverage: { ...topicCoverage.coverage, release_count: 0, dataset_count: 0, min_coverage_pct: null, missing_sessions: 0, symbols_with_gaps: 0, denominators: [] },
      last_sync: null,
      next_step: "no_refs",
    });
    renderPage("/data?topic=RT-empty");
    expect(await screen.findByText("本课题还没有引用数据发布")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "从本课题发起范围预检" })).toHaveAttribute(
      "href",
      "/data?tab=sync&topic=RT-empty",
    );
  });

  it("课题数据视图:接口失败显示错误与重试", async () => {
    catalogMock.topicCoverage.mockRejectedValue(new Error("topic coverage down"));
    renderPage("/data?topic=RT-512");
    expect(await screen.findByText("topic coverage down")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });
});
