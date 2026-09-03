import type { ReactElement } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type JobOut } from "../lib/api";
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
    ...overrides,
  };
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
});

describe("任务中心页", () => {
  it("渲染任务列表(ID/类型/状态/进度)", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([
      makeJob({ status: "running", progress_total: 10, progress_done: 4, phase: "fetch" }),
      makeJob({ job_id: "BJ-TEST000000000002", kind: "backtest_run" }),
    ]);
    renderWithProviders(<Jobs />);
    expect(await screen.findByText("BJ-TEST000000000001")).toBeInTheDocument();
    expect(screen.getByText("BJ-TEST000000000002")).toBeInTheDocument();
    expect(screen.getByText("执行中")).toBeInTheDocument();
    expect(screen.getByText("成功")).toBeInTheDocument();
    expect(screen.getByText("回测")).toBeInTheDocument();
    expect(screen.getByText(/4\/10/)).toBeInTheDocument();
  });

  it("空列表显示空状态", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([]);
    renderWithProviders(<Jobs />);
    expect(await screen.findByText("暂无任务")).toBeInTheDocument();
  });

  it("列表加载失败显示错误与重试", async () => {
    vi.mocked(api.listJobs).mockRejectedValueOnce(new Error("后端不可用"));
    renderWithProviders(<Jobs />);
    expect(await screen.findByText("加载失败")).toBeInTheDocument();
    vi.mocked(api.listJobs).mockResolvedValueOnce([makeJob()]);
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("BJ-TEST000000000001")).toBeInTheDocument();
  });

  it("状态过滤写入 URL,刷新后从 URL 恢复", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([makeJob({ status: "failed" })]);
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
    const list = vi.mocked(api.listJobs).mockResolvedValue([
      makeJob({ status: "running" }),
    ]);
    renderWithProviders(<Jobs />);
    const cancelButton = await screen.findByRole("button", {
      name: "取消任务 BJ-TEST000000000001",
    });
    await userEvent.click(cancelButton);
    await waitFor(() => expect(cancel).toHaveBeenCalledWith("BJ-TEST000000000001"));
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2)); // invalidate 触发重取
  });

  it("点击展开按钮显示任务详情与 payload", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([makeJob()]);
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
    vi.mocked(api.listJobs).mockResolvedValue([
      makeJob({ archived_at: "2026-08-17T08:00:00Z" }),
    ]);
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
    const list = vi.mocked(api.listJobs).mockResolvedValue([makeJob()]);
    renderWithProviders(<Jobs />);
    const archiveButton = await screen.findByRole("button", {
      name: "归档任务 BJ-TEST000000000001",
    });
    await userEvent.click(archiveButton);
    await waitFor(() => expect(archive).toHaveBeenCalledWith("BJ-TEST000000000001"));
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
  });

  it("批量归档按钮调用 bulkArchiveJobs(succeeded+cancelled)并提示计数(issue #221)", async () => {
    const bulk = vi
      .mocked(api.bulkArchiveJobs)
      .mockResolvedValue({ archived_count: 3 });
    vi.mocked(api.listJobs).mockResolvedValue([
      makeJob(),
      makeJob({ job_id: "BJ-TEST000000000002", status: "cancelled" }),
      makeJob({ job_id: "BJ-TEST000000000003", status: "failed" }),
    ]);
    renderWithProviders(<Jobs />);
    await screen.findByText("BJ-TEST000000000001");
    await userEvent.click(
      screen.getByRole("button", { name: "批量归档已完成的任务" }),
    );
    await waitFor(() =>
      expect(bulk).toHaveBeenCalledWith({
        statuses: ["succeeded", "cancelled"],
        limit: 1000,
      }),
    );
    expect(await screen.findByText(/已归档 3 条任务/)).toBeInTheDocument();
  });

  it("archived=only 视图不显示批量归档按钮(issue #221)", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([]);
    renderWithProviders(<Jobs />, ["/jobs?archived=only"]);
    await screen.findByText("暂无任务");
    expect(
      screen.queryByRole("button", { name: "批量归档已完成的任务" }),
    ).not.toBeInTheDocument();
  });

  it("research_run 加载期 phase 显示 k/N 推进(issue #308)", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([
      makeJob({
        kind: "research_run",
        status: "running",
        progress_total: 0,
        progress_done: 0,
        phase: "research_run:decision_load 4/36",
      }),
    ]);
    renderWithProviders(<Jobs />);
    expect(await screen.findByText("BJ-TEST000000000001")).toBeInTheDocument();
    expect(screen.getByText(/加载决策上下文 4\/36 期/)).toBeInTheDocument();
  });

  it("research_run 决策期 phase 显示决策序号与日期(issue #308)", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([
      makeJob({
        kind: "research_run",
        status: "running",
        progress_total: 13,
        progress_done: 16,
        phase: "research_run:signals#2@2024-01-03",
      }),
    ]);
    renderWithProviders(<Jobs />);
    expect(await screen.findByText("BJ-TEST000000000001")).toBeInTheDocument();
    expect(screen.getByText(/决策 #2\(2024-01-03\)/)).toBeInTheDocument();
  });

  it("展开 research_run 详情显示 run_status,不一致时告警(issue #306/#308)", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([
      makeJob({
        kind: "research_run",
        status: "running",
        phase: "research_run:decision_load 4/36",
      }),
    ]);
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
    vi.mocked(api.listJobs).mockResolvedValue([
      makeJob({ kind: "research_run", status: "succeeded", phase: null }),
    ]);
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
