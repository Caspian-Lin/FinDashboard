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
    updated_at: "2026-08-16T08:00:02Z",
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
});
