import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FeatureSnapshotProgress } from "./FeatureSnapshotProgress";
import { formatDuration } from "./FeatureSnapshotProgress.utils";
import type { FeatureSnapshotJobStatus } from "@/lib/research";

function makeJob(
  overrides: Partial<FeatureSnapshotJobStatus> = {},
): FeatureSnapshotJobStatus {
  return {
    job_id: "FSJ-TEST",
    status: "running",
    total_symbols: 100,
    completed_symbols: 25,
    progress_pct: 25,
    elapsed_seconds: 65,
    estimated_remaining_seconds: 195,
    created_at: "2026-08-08T00:00:00+00:00",
    started_at: "2026-08-08T00:00:01+00:00",
    finished_at: null,
    snapshot_id: null,
    error: null,
    ...overrides,
  };
}

describe("FeatureSnapshotProgress", () => {
  it("显示标的进度、已耗时和预计剩余时间", () => {
    render(<FeatureSnapshotProgress job={makeJob()} />);

    expect(screen.getByRole("progressbar")).toHaveAttribute(
      "aria-valuenow",
      "25",
    );
    expect(screen.getByText("25 / 100 · 25.0%")).toBeInTheDocument();
    expect(screen.getByText("1 分 5 秒")).toBeInTheDocument();
    expect(screen.getByText("3 分 15 秒")).toBeInTheDocument();
    expect(screen.getByText("计算中")).toBeInTheDocument();
  });

  it("失败时显示服务端错误,不伪造预计时间", () => {
    render(
      <FeatureSnapshotProgress
        job={makeJob({
          status: "failed",
          estimated_remaining_seconds: null,
          error: "冻结发布文件读取失败",
        })}
      />,
    );

    expect(screen.getByText("冻结发布文件读取失败")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});

describe("formatDuration", () => {
  it("格式化小时级耗时", () => {
    expect(formatDuration(3661)).toBe("1 小时 1 分 1 秒");
  });
});
