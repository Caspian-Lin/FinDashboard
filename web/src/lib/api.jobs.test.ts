import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import {
  api,
  isJobRunning,
  isJobTerminal,
  TERMINAL_JOB_STATUSES,
  type JobOut,
  type JobStatus,
} from "./api";

function makeJob(status: JobStatus): JobOut {
  return {
    job_id: "BJ-TEST",
    kind: "echo",
    queue: "default",
    status,
    priority: 0,
    payload: {},
    payload_checksum: "x",
    idempotency_key: "x",
    progress_total: 0,
    progress_done: 0,
    phase: null,
    result_ref: null,
    error_code: null,
    error_summary: null,
    attempt: 0,
    max_attempts: 3,
    worker_id: null,
    heartbeat_at: null,
    lease_until: null,
    requested_by: "test",
    created_at: "2026-08-01T00:00:00+00:00",
    started_at: null,
    finished_at: null,
    archived_at: null,
    updated_at: "2026-08-01T00:00:00+00:00",
    run_status: null,
    timing: null,
  };
}

describe("isJobRunning", () => {
  it("running / queued / retry_waiting / cancel_requested 视为运行中", () => {
    expect(isJobRunning(makeJob("running"))).toBe(true);
    expect(isJobRunning(makeJob("queued"))).toBe(true);
    expect(isJobRunning(makeJob("retry_waiting"))).toBe(true);
    expect(isJobRunning(makeJob("cancel_requested"))).toBe(true);
  });

  it("终态(succeeded/failed/cancelled/interrupted)不再运行", () => {
    expect(isJobRunning(makeJob("succeeded"))).toBe(false);
    expect(isJobRunning(makeJob("failed"))).toBe(false);
    expect(isJobRunning(makeJob("cancelled"))).toBe(false);
    expect(isJobRunning(makeJob("interrupted"))).toBe(false);
  });

  it("null / undefined 安全返回 false", () => {
    expect(isJobRunning(null)).toBe(false);
    expect(isJobRunning(undefined)).toBe(false);
  });
});

describe("isJobTerminal", () => {
  it("与 TERMINAL_JOB_STATUSES 一致", () => {
    for (const status of TERMINAL_JOB_STATUSES) {
      expect(isJobTerminal(makeJob(status))).toBe(true);
    }
    expect(isJobTerminal(makeJob("running"))).toBe(false);
  });
});

describe("api.getJob / listJobs 路径", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.stubEnv("NODE_ENV", "test");
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.unstubAllEnvs();
  });

  it("getJob 请求 /api/jobs/{jobId}", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(makeJob("succeeded")), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const job = await api.getJob("BJ-ABC");
    expect(job.job_id).toBe("BJ-TEST");
    expect(fetchMock).toHaveBeenCalledOnce();
    const calledUrl = String((fetchMock.mock.calls[0] as unknown[])[0]);
    expect(calledUrl).toContain("/api/jobs/BJ-ABC");
  });

  it("listJobs 拼接 kind/status/queue/limit 查询参数", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([makeJob("queued")]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    await api.listJobs({
      kind: ["bulk_download", "data_sync"],
      status: ["queued", "running"],
      queue: ["data"],
      limit: 25,
    });
    const calledUrl = String((fetchMock.mock.calls[0] as unknown[])[0]);
    // 重复参数用 append,limit 单值。
    expect(calledUrl).toMatch(/kind=bulk_download/);
    expect(calledUrl).toMatch(/kind=data_sync/);
    expect(calledUrl).toMatch(/status=queued/);
    expect(calledUrl).toMatch(/status=running/);
    expect(calledUrl).toMatch(/queue=data/);
    expect(calledUrl).toMatch(/limit=25/);
  });

  it("listJobs 默认不带 archived,显式传时拼接(issue #221)", async () => {
    // Response body 只能读一次,每次调用返回新实例
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    await api.listJobs();
    expect(String((fetchMock.mock.calls[0] as unknown[])[0])).not.toContain("archived=");

    await api.listJobs({ archived: "only" });
    expect(String((fetchMock.mock.calls[1] as unknown[])[0])).toMatch(
      /archived=only/,
    );
  });

  it("archiveJob / unarchiveJob 请求对应端点(issue #221)", async () => {
    // Response body 只能读一次,每次调用返回新实例
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify(makeJob("succeeded")), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    await api.archiveJob("BJ-1");
    await api.unarchiveJob("BJ-1");
    const first = fetchMock.mock.calls[0] as unknown[];
    const second = fetchMock.mock.calls[1] as unknown[];
    expect(String(first[0])).toContain("/api/jobs/BJ-1/archive");
    expect((first[1] as RequestInit).method).toBe("POST");
    expect(String(second[0])).toContain("/api/jobs/BJ-1/unarchive");
    expect((second[1] as RequestInit).method).toBe("POST");
  });

  it("bulkArchiveJobs POST /api/jobs/archive 只回计数(issue #221)", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ archived_count: 7 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const result = await api.bulkArchiveJobs({
      statuses: ["succeeded", "cancelled"],
      limit: 1000,
    });
    expect(result.archived_count).toBe(7);
    const call = fetchMock.mock.calls[0] as unknown[];
    expect(String(call[0])).toContain("/api/jobs/archive");
    expect((call[1] as RequestInit).method).toBe("POST");
    expect(JSON.parse(String((call[1] as RequestInit).body)).statuses).toEqual([
      "succeeded",
      "cancelled",
    ]);
  });
});
