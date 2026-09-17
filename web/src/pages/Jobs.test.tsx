import type { ReactElement } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  api,
  type FlamegraphMeta,
  type FlamegraphSession,
  type JobOut,
} from "../lib/api";
import { LanguageProvider } from "@/i18n";
import Jobs from "./Jobs";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      listJobs: vi.fn(),
      cancelJob: vi.fn(),
      getJob: vi.fn(),
      archiveJob: vi.fn(),
      unarchiveJob: vi.fn(),
      bulkArchiveJobs: vi.fn(),
      flamegraphMeta: vi.fn(),
      listFlamegraphSessions: vi.fn(),
      startFlamegraph: vi.fn(),
    },
  };
});

function makeJob(overrides: Partial<JobOut> = {}): JobOut {
  return {
    job_id: "BJ-TEST000000000001",
    kind: "echo",
    queue: "default",
    status: "succeeded",
    priority: 0,
    payload: { steps: 2 },
    payload_checksum: "abc",
    idempotency_key: "idem-1",
    progress_total: 2,
    progress_done: 2,
    phase: null,
    result_ref: "echo:test",
    error_code: null,
    error_summary: null,
    attempt: 1,
    max_attempts: 3,
    worker_id: "worker-test",
    heartbeat_at: null,
    lease_until: null,
    requested_by: "tester",
    created_at: "2026-08-16T08:00:00Z",
    started_at: "2026-08-16T08:00:01Z",
    finished_at: "2026-08-16T08:00:02Z",
    archived_at: null,
    updated_at: "2026-08-16T08:00:02Z",
    run_status: null,
    timing: null,
    ...overrides,
  };
}

/** listJobs 分页返回(issue #373):{items, total}。 */
function listResult(items: JobOut[], total = items.length) {
  return { items, total };
}

function renderWithProviders(ui: ReactElement, initialEntries = ["/jobs"]) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={initialEntries}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

// api 模块级 vi.fn 的调用计数跨用例累积,每个用例前清空。
beforeEach(() => {
  vi.clearAllMocks();
  // 诊断重放能力表默认回显(echo 在拒绝集合;后端静态能力)。
  vi.mocked(api.flamegraphMeta).mockResolvedValue({
    replayable_kinds: {
      backtest_run: "重放会真实插入一行新 backtest_runs(执行层无幂等检查)",
    },
    rejected_kinds: { echo: "自检桩,无诊断价值" },
  } satisfies FlamegraphMeta);
  vi.mocked(api.listFlamegraphSessions).mockResolvedValue([]);
});

describe("任务中心页", () => {
  it("渲染任务列表(ID/类型/状态/进度)", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([
        makeJob({ status: "running", progress_total: 10, progress_done: 4, phase: "fetch" }),
        makeJob({ job_id: "BJ-TEST000000000002", kind: "backtest_run" }),
      ]),
    );
    renderWithProviders(<Jobs />);
    expect(await screen.findByText("BJ-TEST000000000001")).toBeInTheDocument();
    expect(screen.getByText("BJ-TEST000000000002")).toBeInTheDocument();
    expect(screen.getByText("执行中")).toBeInTheDocument();
    expect(screen.getByText("成功")).toBeInTheDocument();
    expect(screen.getByText("回测")).toBeInTheDocument();
    expect(screen.getByText(/4\/10/)).toBeInTheDocument();
  });

  it("列表请求带分页参数:默认第 1 页每页 10 条(issue #373)", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(listResult([makeJob()]));
    renderWithProviders(<Jobs />);
    await screen.findByText("BJ-TEST000000000001");
    expect(vi.mocked(api.listJobs)).toHaveBeenCalledWith(
      expect.objectContaining({ limit: 10, offset: 0 }),
    );
  });

  it("翻页写入 URL 页码并按 offset 取数,总数经 X-Total-Count 透出(issue #373)", async () => {
    const page1 = Array.from({ length: 10 }, (_, i) =>
      makeJob({ job_id: `BJ-PAGEONE000000${i}` }),
    );
    const page2 = [makeJob({ job_id: "BJ-SECOND0000000001" })];
    vi.mocked(api.listJobs).mockImplementation(async (params) => {
      if ((params?.offset ?? 0) >= 10) return listResult(page2, 11);
      return listResult(page1, 11);
    });
    renderWithProviders(<Jobs />);
    await screen.findByText("BJ-PAGEONE0000000");
    expect(screen.getByText("共 11 条")).toBeInTheDocument();
    expect(screen.getByText("第 1 / 2 页")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("BJ-SECOND0000000001")).toBeInTheDocument();
    expect(vi.mocked(api.listJobs)).toHaveBeenLastCalledWith(
      expect.objectContaining({ limit: 10, offset: 10 }),
    );
  });

  it("状态过滤变更时页码重置回第 1 页(issue #373)", async () => {
    vi.mocked(api.listJobs).mockImplementation(async () =>
      listResult([makeJob({ status: "failed" })], 11),
    );
    renderWithProviders(<Jobs />, ["/jobs?status=failed&page=2"]);
    await screen.findByText("BJ-TEST000000000001");
    expect(vi.mocked(api.listJobs)).toHaveBeenCalledWith(
      expect.objectContaining({ status: ["failed"], offset: 10 }),
    );
    await userEvent.click(screen.getByRole("combobox", { name: "按状态过滤" }));
    await userEvent.click(await screen.findByRole("option", { name: "全部状态" }));
    await waitFor(() =>
      expect(vi.mocked(api.listJobs)).toHaveBeenLastCalledWith(
        expect.objectContaining({ status: undefined, offset: 0 }),
      ),
    );
  });

  it("空列表显示空状态", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(listResult([]));
    renderWithProviders(<Jobs />);
    expect(await screen.findByText("暂无任务")).toBeInTheDocument();
  });

  it("列表加载失败显示错误与重试", async () => {
    vi.mocked(api.listJobs).mockRejectedValueOnce(new Error("后端不可用"));
    renderWithProviders(<Jobs />);
    expect(await screen.findByText("加载失败")).toBeInTheDocument();
    vi.mocked(api.listJobs).mockResolvedValueOnce(listResult([makeJob()]));
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("BJ-TEST000000000001")).toBeInTheDocument();
  });

  it("状态过滤写入 URL,刷新后从 URL 恢复", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([makeJob({ status: "failed" })]),
    );
    renderWithProviders(<Jobs />, ["/jobs?status=failed"]);
    expect(await screen.findByText("BJ-TEST000000000001")).toBeInTheDocument();
    expect(vi.mocked(api.listJobs)).toHaveBeenCalledWith(
      expect.objectContaining({ status: ["failed"] }),
    );
    // "失败" 同时出现在筛选下拉选项与状态徽章中
    expect(screen.getAllByText("失败").length).toBeGreaterThanOrEqual(2);
  });

  it("运行中的任务显示取消按钮,点击后调用 cancelJob 并刷新列表", async () => {
    const cancel = vi.mocked(api.cancelJob).mockResolvedValue(
      makeJob({ status: "cancelled" }),
    );
    const list = vi.mocked(api.listJobs).mockResolvedValue(
      listResult([makeJob({ status: "running" })]),
    );
    renderWithProviders(<Jobs />);
    const cancelButton = await screen.findByRole("button", {
      name: "取消任务 BJ-TEST000000000001",
    });
    // 分页后 listJobs 同时服务主列表与归档计数轻量查询(#373),
    // 断言语义改为「操作成功后重取发生」而非精确次数。
    const callsAtMount = list.mock.calls.length;
    await userEvent.click(cancelButton);
    await waitFor(() => expect(cancel).toHaveBeenCalledWith("BJ-TEST000000000001"));
    await waitFor(() =>
      expect(list.mock.calls.length).toBeGreaterThan(callsAtMount),
    );
  });

  it("点击展开按钮显示任务详情与 payload", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(listResult([makeJob()]));
    renderWithProviders(<Jobs />);
    await screen.findByText("BJ-TEST000000000001");
    await userEvent.click(
      screen.getByRole("button", { name: "展开任务详情" }),
    );
    const detail = await screen.findByText("入参 payload");
    expect(within(detail.closest("div")!).getByText(/"steps": 2/)).toBeInTheDocument();
    expect(screen.getByText("结果引用")).toBeInTheDocument();
  });

  it("归档过滤写入 URL,archived=only 时透传给 listJobs(issue #221)", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([makeJob({ archived_at: "2026-08-17T08:00:00Z" })]),
    );
    renderWithProviders(<Jobs />, ["/jobs?archived=only"]);
    expect(await screen.findByText("BJ-TEST000000000001")).toBeInTheDocument();
    expect(vi.mocked(api.listJobs)).toHaveBeenCalledWith(
      expect.objectContaining({ archived: "only" }),
    );
    // 归档行显示「已归档」标记与「恢复」按钮
    expect(screen.getByText("已归档")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "取消归档任务 BJ-TEST000000000001" }),
    ).toBeInTheDocument();
  });

  it("终态任务显示归档按钮,点击后调用 archiveJob 并刷新列表(issue #221)", async () => {
    const archive = vi.mocked(api.archiveJob).mockResolvedValue(
      makeJob({ archived_at: "2026-08-17T08:00:00Z" }),
    );
    const list = vi.mocked(api.listJobs).mockResolvedValue(listResult([makeJob()]));
    renderWithProviders(<Jobs />);
    const archiveButton = await screen.findByRole("button", {
      name: "归档任务 BJ-TEST000000000001",
    });
    const callsAtMount = list.mock.calls.length;
    await userEvent.click(archiveButton);
    await waitFor(() => expect(archive).toHaveBeenCalledWith("BJ-TEST000000000001"));
    await waitFor(() =>
      expect(list.mock.calls.length).toBeGreaterThan(callsAtMount),
    );
  });

  it("批量归档按钮调用 bulkArchiveJobs(succeeded+cancelled)并提示计数(issue #221/#373)", async () => {
    const bulk = vi
      .mocked(api.bulkArchiveJobs)
      .mockResolvedValue({ archived_count: 3 });
    // 分页后归档目标计数来自轻量 count 查询(listJobs limit=1),当前页只渲染 failed。
    vi.mocked(api.listJobs).mockImplementation(async (params) => {
      if (params?.status && params.status.length === 2) return listResult([], 3);
      return listResult([makeJob({ status: "failed" })]);
    });
    renderWithProviders(<Jobs />);
    const bulkButton = await screen.findByRole("button", {
      name: "批量归档已完成的任务",
    });
    await waitFor(() => expect(bulkButton).toBeEnabled());
    await userEvent.click(bulkButton);
    await waitFor(() =>
      expect(bulk).toHaveBeenCalledWith({
        statuses: ["succeeded", "cancelled"],
        limit: 1000,
      }),
    );
    expect(await screen.findByText(/已归档 3 条任务/)).toBeInTheDocument();
  });

  it("archived=only 视图不显示批量归档按钮(issue #221)", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(listResult([]));
    renderWithProviders(<Jobs />, ["/jobs?archived=only"]);
    await screen.findByText("暂无任务");
    expect(
      screen.queryByRole("button", { name: "批量归档已完成的任务" }),
    ).not.toBeInTheDocument();
  });

  it("research_run 加载期 phase 显示 k/N 推进(issue #308)", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([
        makeJob({
          kind: "research_run",
          status: "running",
          progress_total: 0,
          progress_done: 0,
          phase: "research_run:decision_load 4/36",
        }),
      ]),
    );
    renderWithProviders(<Jobs />);
    expect(await screen.findByText("BJ-TEST000000000001")).toBeInTheDocument();
    expect(screen.getByText(/加载决策上下文 4\/36 期/)).toBeInTheDocument();
  });

  it("research_run 决策期 phase 显示决策序号与日期(issue #308)", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([
        makeJob({
          kind: "research_run",
          status: "running",
          progress_total: 13,
          progress_done: 16,
          phase: "research_run:signals#2@2024-01-03",
        }),
      ]),
    );
    renderWithProviders(<Jobs />);
    expect(await screen.findByText("BJ-TEST000000000001")).toBeInTheDocument();
    expect(screen.getByText(/决策 #2\(2024-01-03\)/)).toBeInTheDocument();
  });

  it("展开 research_run 详情显示 run_status,不一致时告警(issue #306/#308)", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([
        makeJob({
          kind: "research_run",
          status: "running",
          phase: "research_run:decision_load 4/36",
        }),
      ]),
    );
    // 列表不 join,单查才透传 run_status —— 模拟 run 已被标 interrupted
    // 但 job 仍 running 的僵尸形态(RR-7a74)。
    vi.mocked(api.getJob).mockResolvedValue(
      makeJob({
        kind: "research_run",
        status: "running",
        run_status: "interrupted",
        phase: "research_run:decision_load 4/36",
      }),
    );
    renderWithProviders(<Jobs />);
    await screen.findByText("BJ-TEST000000000001");
    await userEvent.click(
      screen.getByRole("button", { name: "展开任务详情" }),
    );
    expect(vi.mocked(api.getJob)).toHaveBeenCalledWith("BJ-TEST000000000001");
    expect(await screen.findByText("Run 状态")).toBeInTheDocument();
    expect(screen.getByText("interrupted")).toBeInTheDocument();
    expect(
      screen.getByText(/run 与任务状态不一致/),
    ).toBeInTheDocument();
  });

  it("run 状态一致时不显示不一致告警(issue #308)", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([makeJob({ kind: "research_run", status: "succeeded", phase: null })]),
    );
    vi.mocked(api.getJob).mockResolvedValue(
      makeJob({
        kind: "research_run",
        status: "succeeded",
        run_status: "completed",
      }),
    );
    renderWithProviders(<Jobs />);
    await screen.findByText("BJ-TEST000000000001");
    await userEvent.click(
      screen.getByRole("button", { name: "展开任务详情" }),
    );
    expect(await screen.findByText("completed")).toBeInTheDocument();
    expect(
      screen.queryByText(/run 与任务状态不一致/),
    ).not.toBeInTheDocument();
  });
});

describe("任务详情:错误摘要与耗时(issue #373)", () => {
  it("错误摘要多行完整展示且可一键复制", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const summary = "[stage=signals; decision=2024-03-01; decision_index=3] 硬约束拒绝";
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([makeJob({ error_summary: summary })]),
    );
    renderWithProviders(<Jobs />);
    await screen.findByText("BJ-TEST000000000001");
    await userEvent.click(screen.getByRole("button", { name: "展开任务详情" }));
    expect(await screen.findByText(summary)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "复制错误摘要" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(summary));
  });

  it("无错误摘要时不渲染摘要块", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(listResult([makeJob()]));
    renderWithProviders(<Jobs />);
    await screen.findByText("BJ-TEST000000000001");
    await userEvent.click(screen.getByRole("button", { name: "展开任务详情" }));
    await screen.findByText("入参 payload");
    expect(screen.queryByText("复制错误摘要")).not.toBeInTheDocument();
  });

  it("timing 聚合展示耗时与 IO 占比;null 显示 —(issue #383/#373)", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([
        makeJob({
          timing: {
            execute_elapsed_seconds: 12.5,
            parquet_reads: {
              read_ops: 48,
              read_elapsed_ms: 6250,
              read_bytes: 12_300_000,
              ops_by_entry: {},
            },
          },
        }),
      ]),
    );
    renderWithProviders(<Jobs />);
    await screen.findByText("BJ-TEST000000000001");
    await userEvent.click(screen.getByRole("button", { name: "展开任务详情" }));
    expect(await screen.findByText(/耗时 12\.5s · parquet 读 48 次/)).toBeInTheDocument();
    expect(screen.getByText(/IO 占比 50%/)).toBeInTheDocument();
  });

  it("timing 为 null 显示 —", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(listResult([makeJob()]));
    renderWithProviders(<Jobs />);
    await screen.findByText("BJ-TEST000000000001");
    await userEvent.click(screen.getByRole("button", { name: "展开任务详情" }));
    const timingLabel = await screen.findByText("执行耗时 / IO");
    expect(within(timingLabel.closest("div")!).getByText("—")).toBeInTheDocument();
  });
});

describe("任务详情:诊断重放入口(issue #373)", () => {
  it("拒绝 kind:按钮置灰且 tooltip 显示原因", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(listResult([makeJob()])); // echo 在拒绝集合
    renderWithProviders(<Jobs />);
    await screen.findByText("BJ-TEST000000000001");
    await userEvent.click(screen.getByRole("button", { name: "展开任务详情" }));
    const button = await screen.findByRole("button", { name: /火焰图诊断/ });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "自检桩,无诊断价值");
  });

  it("放行 kind:确认弹窗明示副作用,确认后调用 startFlamegraph", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([makeJob({ kind: "backtest_run" })]),
    );
    const start = vi.mocked(api.startFlamegraph).mockResolvedValue({
      session: {
        session_id: "BJ-TEST000000000001-20260908-120000",
        status: "running",
        meta: null,
        timing: null,
        result: null,
      } satisfies FlamegraphSession,
    });
    renderWithProviders(<Jobs />);
    await screen.findByText("BJ-TEST000000000001");
    await userEvent.click(screen.getByRole("button", { name: "展开任务详情" }));
    const button = await screen.findByRole("button", { name: /火焰图诊断/ });
    expect(button).toBeEnabled();
    await userEvent.click(button);
    expect(
      await screen.findByText(/重放会真实插入一行新 backtest_runs/),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "确认" }));
    await waitFor(() => expect(start).toHaveBeenCalledWith("BJ-TEST000000000001"));
  });

  it("非终态 job 不渲染诊断入口", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([makeJob({ kind: "backtest_run", status: "running" })]),
    );
    renderWithProviders(<Jobs />);
    await screen.findByText("BJ-TEST000000000001");
    await userEvent.click(screen.getByRole("button", { name: "展开任务详情" }));
    await screen.findByText("入参 payload");
    expect(screen.queryByText(/火焰图诊断/)).not.toBeInTheDocument();
  });

  it("完成的诊断会话内嵌火焰图并提供下载", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([makeJob({ kind: "backtest_run" })]),
    );
    vi.mocked(api.listFlamegraphSessions).mockResolvedValue([
      {
        session_id: "BJ-TEST000000000001-20260908-120000",
        status: "done",
        meta: {
          job_id: "BJ-TEST000000000001",
          kind: "backtest_run",
          source_status: "succeeded",
          replay_side_effect: "副作用说明",
          format: "flamegraph",
          rate_hz: 50,
          started_at: "2026-09-08T12:00:00Z",
        },
        timing: {
          execute_elapsed_seconds: 3.2,
          parquet_reads: {
            read_ops: 5,
            read_elapsed_ms: 800,
            read_bytes: 1_000_000,
            ops_by_entry: {},
          },
        },
        result: {
          status: "succeeded",
          result_ref: "bt:1",
          error_code: null,
          error_summary: null,
        },
      },
    ]);
    renderWithProviders(<Jobs />);
    await screen.findByText("BJ-TEST000000000001");
    await userEvent.click(screen.getByRole("button", { name: "展开任务详情" }));
    const img = await screen.findByRole("img");
    expect(img).toHaveAttribute(
      "src",
      "/api/jobs/BJ-TEST000000000001/flamegraph/BJ-TEST000000000001-20260908-120000/flamegraph.svg",
    );
    expect(screen.getByText("下载火焰图")).toBeInTheDocument();
  });
});

describe("任务进度可视化(issue #442)", () => {
  afterEach(() => {
    // en 用例写入的语言偏好不能泄漏给同文件其他用例(默认中文零破坏)。
    localStorage.removeItem("finboard-lang");
  });

  /** 包 LanguageProvider 的渲染(语言从 localStorage 读入)。 */
  function renderWithLang(ui: ReactElement, lang: "zh" | "en") {
    localStorage.setItem("finboard-lang", lang);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/jobs"]}>
          <LanguageProvider>{ui}</LanguageProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("done/total>0 渲染进度条与百分比(running)", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([
        makeJob({
          status: "running",
          progress_total: 10,
          progress_done: 4,
          phase: "fetch",
        }),
      ]),
    );
    renderWithProviders(<Jobs />);
    expect(await screen.findByText("BJ-TEST000000000001")).toBeInTheDocument();
    expect(
      screen.getByRole("progressbar", { name: "执行进度" }),
    ).toBeInTheDocument();
    expect(screen.getByText("4/10 · 40%")).toBeInTheDocument();
  });

  it("total=0 不渲染进度条,phase 缺失显示 —", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([
        makeJob({
          status: "running",
          progress_total: 0,
          progress_done: 0,
          phase: null,
          started_at: null,
        }),
      ]),
    );
    renderWithProviders(<Jobs />);
    expect(await screen.findByText("BJ-TEST000000000001")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("已运行时长 = started_at → now,随轮询刷新(刷新按钮触发重取)", async () => {
    const base = new Date("2026-09-11T04:00:00Z").getTime();
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(base);
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([
        makeJob({
          status: "running",
          progress_total: 10,
          progress_done: 4,
          started_at: "2026-09-11T03:59:00Z",
        }),
      ]),
    );
    renderWithProviders(<Jobs />);
    expect(await screen.findByText(/已运行 1 分 0 秒/)).toBeInTheDocument();
    // 轮询/刷新语义:now 前进 65 秒后重取,时长重算(60s → 125s)。
    nowSpy.mockReturnValue(base + 65_000);
    await userEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
    await waitFor(() =>
      expect(screen.getByText(/已运行 2 分 5 秒/)).toBeInTheDocument(),
    );
    nowSpy.mockRestore();
  });

  it("终态时长定格于 started_at → finished_at,不随墙钟增长", async () => {
    const base = new Date("2026-09-11T04:00:00Z").getTime();
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(base);
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([
        makeJob({
          status: "succeeded",
          progress_total: 2,
          progress_done: 2,
          started_at: "2026-09-11T03:58:30Z",
          finished_at: "2026-09-11T03:59:30Z",
        }),
      ]),
    );
    renderWithProviders(<Jobs />);
    expect(await screen.findByText(/运行耗时 1 分 0 秒/)).toBeInTheDocument();
    nowSpy.mockReturnValue(base + 600_000);
    await userEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
    await waitFor(() =>
      expect(screen.getByText(/运行耗时 1 分 0 秒/)).toBeInTheDocument(),
    );
    nowSpy.mockRestore();
  });

  it("运行中/终态样式区分:终态进度条降灰、行打 terminal 标记", async () => {
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([
        makeJob({
          status: "running",
          progress_total: 10,
          progress_done: 4,
          started_at: null,
        }),
        makeJob({
          job_id: "BJ-TEST000000000002",
          status: "succeeded",
          progress_total: 2,
          progress_done: 2,
          started_at: null,
        }),
      ]),
    );
    const { container } = renderWithProviders(<Jobs />);
    await screen.findByText("BJ-TEST000000000001");
    const bars = screen.getAllByRole("progressbar", { name: "执行进度" });
    expect(bars).toHaveLength(2);
    const runningIndicator = bars[0].firstElementChild as HTMLElement;
    expect(runningIndicator.className).toContain("bg-primary");
    expect(runningIndicator.className).not.toContain("bg-muted-foreground");
    const terminalIndicator = bars[1].firstElementChild as HTMLElement;
    expect(terminalIndicator.className).toContain("bg-muted-foreground/40");
    expect(terminalIndicator.className).not.toContain("bg-primary");
    expect(
      container.querySelector('[data-job-progress="running"]'),
    ).toBeInTheDocument();
    expect(
      container.querySelector('[data-job-progress="terminal"]'),
    ).toBeInTheDocument();
  });

  it("JobDetail 展开完整:phase 解析 + 缺失字段显示 — + 最后更新时龄", async () => {
    const updated = new Date(Date.now() - 30_000).toISOString();
    const job = makeJob({
      kind: "research_run",
      status: "running",
      progress_total: 0,
      progress_done: 0,
      phase: "research_run:decision_load 4/36",
      started_at: null,
      updated_at: updated,
    });
    vi.mocked(api.listJobs).mockResolvedValue(listResult([job]));
    // 单查透传(run_status)与列表同形;显式打桩避免用例间 mock 实现泄漏
    // (clearAllMocks 不清 mockResolvedValue)。
    vi.mocked(api.getJob).mockResolvedValue(job);
    const { container } = renderWithProviders(<Jobs />);
    await screen.findByText("BJ-TEST000000000001");
    await userEvent.click(screen.getByRole("button", { name: "展开任务详情" }));
    const detailProgress = container.querySelector('[data-variant="detail"]');
    expect(detailProgress).not.toBeNull();
    const scope = within(detailProgress as HTMLElement);
    // 加载帧 phase 解析(issue #308 回归:详情同样走 parseJobPhase)。
    expect(scope.getByText(/加载决策上下文 4\/36 期/)).toBeInTheDocument();
    // total=0:进度条区域显示 —。
    expect(scope.getByText("—")).toBeInTheDocument();
    // started_at 缺失 → 已运行位置显示 —;updated_at 时龄(心跳代理)可见。
    expect(scope.getByText(/^— · 最后更新 3\d 秒前/)).toBeInTheDocument();
  });

  it("zh/en 双语:en 显示 Running for / Updated … ago,中文默认零破坏", async () => {
    const base = new Date("2026-09-11T04:00:00Z").getTime();
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(base);
    vi.mocked(api.listJobs).mockResolvedValue(
      listResult([
        makeJob({
          status: "running",
          progress_total: 10,
          progress_done: 4,
          started_at: "2026-09-11T03:59:00Z",
          updated_at: new Date(base - 30_000).toISOString(),
        }),
      ]),
    );
    renderWithLang(<Jobs />, "en");
    // 行内紧凑:done/total + 百分比 + 已运行时长(英文)。
    expect(await screen.findByText("4/10 · 40%")).toBeInTheDocument();
    expect(screen.getByText(/Running for 1m 0s/)).toBeInTheDocument();
    // 时龄在展开详情里(完整形态):进度条 + 阶段 + 时长 + 最后更新。
    await userEvent.click(screen.getByRole("button", { name: "Expand job details" }));
    const scope = await screen
      .findByText("Request payload")
      .then(() => document.querySelector('[data-variant="detail"]'));
    expect(scope).not.toBeNull();
    expect(scope!.textContent).toContain("Running for 1m 0s");
    expect(scope!.textContent).toContain("Updated 30s ago");
    nowSpy.mockRestore();
  });
});
