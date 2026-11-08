import { describe, expect, it } from "vitest";
import {
  dataHref,
  legacyResearchDataTarget,
  parseDataTab,
  RELEASE_KIND_DATASET,
} from "./data-links";

describe("dataHref", () => {
  it("默认概览省略 tab 参数", () => {
    expect(dataHref({})).toBe("/data");
    expect(dataHref({ tab: "overview" })).toBe("/data");
  });

  it("按稳定顺序拼接参数并跳过空值", () => {
    expect(
      dataHref({ tab: "datasets", release: "DR-1", dataset: "bars", status: null, q: "" }),
    ).toBe("/data?tab=datasets&dataset=bars&release=DR-1");
  });

  it("保留 topic 预填与 offset", () => {
    expect(dataHref({ tab: "sync", topic: "RT-1" })).toBe("/data?tab=sync&topic=RT-1");
    expect(dataHref({ tab: "datasets", offset: 40 })).toBe("/data?tab=datasets&offset=40");
  });
});

describe("parseDataTab", () => {
  it("未知值回退概览", () => {
    expect(parseDataTab(null)).toBe("overview");
    expect(parseDataTab("bogus")).toBe("overview");
    expect(parseDataTab("tech")).toBe("tech");
  });
});

describe("legacyResearchDataTarget(旧 /research/data 深链保全)", () => {
  it("旧页签映射到目录数据集并落「数据集」tab", () => {
    expect(legacyResearchDataTarget("?tab=bars")).toBe("/data?tab=datasets&dataset=bars");
    expect(legacyResearchDataTarget("?tab=metrics")).toBe(
      "/data?tab=datasets&dataset=daily_metrics",
    );
    expect(legacyResearchDataTarget("?tab=financial")).toBe(
      "/data?tab=datasets&dataset=financial_indicators",
    );
    expect(legacyResearchDataTarget("?tab=income")).toBe(
      "/data?tab=datasets&dataset=income_statements",
    );
    expect(legacyResearchDataTarget("?tab=balance")).toBe(
      "/data?tab=datasets&dataset=balance_sheets",
    );
    expect(legacyResearchDataTarget("?tab=cashflow")).toBe(
      "/data?tab=datasets&dataset=cashflow_statements",
    );
    expect(legacyResearchDataTarget("?tab=dividends")).toBe(
      "/data?tab=datasets&dataset=dividends",
    );
    expect(legacyResearchDataTarget("?tab=convertible")).toBe(
      "/data?tab=datasets&dataset=convertible_metrics",
    );
  });

  it("旧的 fetch / releases 别名仍落行情数据集", () => {
    expect(legacyResearchDataTarget("?tab=fetch")).toBe("/data?tab=datasets&dataset=bars");
    expect(legacyResearchDataTarget("?tab=releases")).toBe("/data?tab=datasets&dataset=bars");
  });

  it("标的页签额外落到旧操作面板的标的子页签", () => {
    expect(legacyResearchDataTarget("?tab=instruments")).toBe(
      "/data?tab=datasets&dataset=profiles&view=ops&sub=instruments",
    );
  });

  it("release 深链保留并落「数据集」tab", () => {
    expect(legacyResearchDataTarget("?tab=metrics&release=DR-512")).toBe(
      "/data?tab=datasets&dataset=daily_metrics&release=DR-512",
    );
    expect(legacyResearchDataTarget("?release=DR-512")).toBe(
      "/data?tab=datasets&release=DR-512",
    );
  });

  it("未知页签丢弃但不丢其他参数,不 404", () => {
    expect(legacyResearchDataTarget("?tab=bogus&release=DR-1")).toBe(
      "/data?tab=datasets&release=DR-1",
    );
    expect(legacyResearchDataTarget("?tab=bogus")).toBe("/data");
    expect(legacyResearchDataTarget("")).toBe("/data");
  });
});

describe("RELEASE_KIND_DATASET(发布 kind → 目录 key)", () => {
  it("遗留 bars 发布 kind 收敛到 bars", () => {
    expect(RELEASE_KIND_DATASET.a_share_tushare).toBe("bars");
    expect(RELEASE_KIND_DATASET.multi_asset_mixed).toBe("bars");
    expect(RELEASE_KIND_DATASET.daily_metrics).toBe("daily_metrics");
  });
});
