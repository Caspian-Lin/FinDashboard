import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FeatureSnapshotProgress } from "./FeatureSnapshotProgress";
import { formatDuration } from "./FeatureSnapshotProgress.utils";
import type { JobOut } from "@/lib/api";

function makeJob(overrides: Partial<JobOut> = {}): JobOut {
  return {
    job_id: "BJ-TEST",
    kind: "feature_snapshot",
    queue: "research",
    status: "running",
    priority: 0,
    payload: {},
    payload_checksum: "abc",
    idempotency_key: "feature_snapshot:test",
    progress_total: 100,
    progress_done: 25,
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
    created_at: "2026-08-08T00:00:00+00:00",
    started_at: "2026-08-08T00:00:01+00:00",
    finished_at: null,
    archived_at: null,
    updated_at: "2026-08-08T00:00:01+00:00",
    ...overrides,
  };
}

describe("FeatureSnapshotProgress", () => {
  it("显示标的进度与状态标签", () => {
    render(<FeatureSnapshotProgress job={makeJob()} />);

    expect(screen.getByRole("progressbar")).toHaveAttribute(
      "aria-valuenow",
      "25",
    );
    expect(screen.getByText("25 / 100 · 25.0%")).toBeInTheDocument();
    expect(screen.getByText("计算中")).toBeInTheDocument();
  });

  it("失败时显示服务端错误,不显示预计剩余时间", () => {
    render(
      <FeatureSnapshotProgress
        job={makeJob({
          status: "failed",
          finished_at: "2026-08-08T00:01:06+00:00",
          error_summary: "冻结发布文件读取失败",
        })}
      />,
    );

    expect(screen.getByText("冻结发布文件读取失败")).toBeInTheDocument();
    // 终态时预计剩余显示为 "—"
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});

describe("formatDuration", () => {
  it("格式化小时级耗时", () => {
    expect(formatDuration(3661)).toBe("1 小时 1 分 1 秒");
  });
});
