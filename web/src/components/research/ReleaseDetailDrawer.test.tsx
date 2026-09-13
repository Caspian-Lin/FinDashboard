import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ReleaseDetailDrawer } from "./ReleaseDetailDrawer";

const datasetApiMock = vi.hoisted(() => ({
  releaseDetail: vi.fn(),
}));

vi.mock("@/lib/research", () => ({
  datasetApi: datasetApiMock,
}));

function renderDrawer(releaseId: string): ReactElement {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>
      <ReleaseDetailDrawer releaseId={releaseId} onClose={() => {}} />
    </QueryClientProvider>
  );
}

const baseDetail = {
  release_id: "a-share-bars-20260804-v1",
  dataset_name: "a_share_daily_bars",
  source: "tushare",
  version: "20260804-v1",
  schema_version: "v1",
  start_date: "2020-01-02",
  end_date: "2024-12-31",
  period: "1d",
  adjustment: "qfq",
  symbol_count: 1,
  row_count: 100,
  capabilities: [],
  quality_status: "warnings",
  known_limitations: [],
  published_at: "2026-08-04T00:00:00Z",
  release_checksum: "a".repeat(64),
  instruments: [
    {
      code: "000001.SZ",
      name: "平安银行",
      market: "a_share",
      instrument_type: "stock",
      asset_class: "equity",
      artifact_path: "bars/000001.SZ_D1_qfq.parquet",
      row_count: 100,
      start_date: "2020-01-02",
      end_date: "2024-12-31",
      missing_sessions: 835,
      suspended_sessions: 2,
    },
  ],
};

/** issue #349:覆盖率与真实缺口并排可见,字符串 coverage_pct 容错显示。 */
describe("ReleaseDetailDrawer 覆盖率与缺口并排展示", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("字符串 coverage_pct 显示为百分比,缺口合计与口径标注同屏", async () => {
    datasetApiMock.releaseDetail.mockResolvedValue({
      ...baseDetail,
      // 模拟旧实例 / manifest 冻结的 str(Decimal) 通道。
      coverage_pct: "1",
      quality_report: {
        release_coverage: "1",
        coverage: {
          missing_sessions: 835,
          suspended_sessions: 2,
          anomaly_count: 0,
        },
      },
    });

    render(renderDrawer("a-share-bars-20260804-v1"));

    // 字符串 "1" 不再原样展示,而是归一为百分比。
    expect(await screen.findByText("+100.0%")).toBeInTheDocument();
    // 缺口合计(交易日历审计口径)与覆盖率并排可见。
    expect(screen.getByText(/缺 835 日/)).toBeInTheDocument();
    expect(screen.getByText(/停牌 2/)).toBeInTheDocument();
    // 口径标注:真实数据缺口以 missing_sessions 为准。
    expect(
      screen.getByText(/覆盖率含「已查询但无 bar」的区间/),
    ).toBeInTheDocument();
    expect(screen.getByText(/以质量报告 missing_sessions/)).toBeInTheDocument();
  });

  it("质量报告无缺口合计块时显示占位,不强造数据", async () => {
    datasetApiMock.releaseDetail.mockResolvedValue({
      ...baseDetail,
      coverage_pct: 0.98,
      quality_report: { release_coverage: "0.98" },
    });

    render(renderDrawer("a-share-bars-20260804-v1"));

    expect(await screen.findByText("+98.0%")).toBeInTheDocument();
    expect(screen.queryByText(/缺 \d+ 日/)).not.toBeInTheDocument();
  });
});
