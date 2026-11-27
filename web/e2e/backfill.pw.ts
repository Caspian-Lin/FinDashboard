import { expect, test } from "@playwright/test";
import { evidenceRun, jobId, mockApi, preferences, releaseId, runId, topicId } from "./fixtures";

test("select shared backfill scope, preview, confirm, and find the new release", async ({ page }, info) => {
  test.skip(info.project.name !== "research", "One realistic shared-data path is enough for both editions.");
  await preferences(page, "zh", "light");
  const receipt = { request_id: "BF-verified", status: "queued", job_id: jobId, reused: false };
  const checksum = "d".repeat(64);
  const retryReceipt = { ...receipt, request_id: "BF-retry", job_id: "BJ-retry" };
  let drift = false;
  let previews = 0;
  const wire = await mockApi(page, ({ url, method, body }) => {
    if (url.pathname === "/api/data/instruments") return { json: { items: [
      { code: "FBDEMO01.SH", name: "合成标的", market: "a_share", instrument_type: "stock", listing_board: "main_board" },
      { code: "FBDEMOIDX.SH", name: "合成基准", market: "a_share", instrument_type: "index", listing_board: "main_board" },
    ], total: 2 } };
    if (url.pathname === "/api/data/backfills/preview") {
      const spec = body.spec as { scope: { universe: { explicit_symbols: string[] }; benchmarks: string[] } };
      expect(spec.scope.universe.explicit_symbols).toEqual(["FBDEMO01.SH"]);
      expect(spec.scope.benchmarks).toEqual(["FBDEMOIDX.SH"]);
      previews += 1;
      return { json: {
        plan_checksum: checksum, blockers: previews === 1 ? [{ code: "fixture_blocked", message: "模拟缺少交易日历" }] : [],
        warnings: [], overlaps: [],
        quota: { status: "ok", estimated_total: 2, remaining_today: 100 },
        publish_impact: { new_releases: 1, affected_specs: 0 },
        items: [{ dataset: "bars", source: "tushare", adjustment: "qfq", action: "sync_publish",
          missing_ranges: [["2024-01-03", "2024-01-05"]], estimated_requests: 2, blockers: [],
          publish_preview: { release_id: releaseId, version: "test" },
        }],
      } };
    }
    if (url.pathname === "/api/data/backfills" && method === "POST") {
      expect(body.expected_plan_checksum).toBe(checksum);
      expect(String(body.idempotency_key)).toContain(checksum);
      if (!drift) {
        drift = true;
        return { status: 409, json: { detail: { code: "backfill_plan_drift", message: "Fixture drift" } } };
      }
      return { status: 202, json: receipt };
    }
    if (url.pathname === "/api/data/backfills/" + receipt.request_id) return { json: {
      ...receipt, status: "failed", release_ids: [], items: [], error_summary: "采集队列测试中断。",
    } };
    if (url.pathname === `/api/data/backfills/${receipt.request_id}/retry` && method === "POST") return { status: 202, json: retryReceipt };
    if (url.pathname === "/api/data/backfills/" + retryReceipt.request_id) return { json: {
      ...retryReceipt, status: "succeeded", release_ids: [releaseId], items: [], error_summary: null,
    } };
  });
  await page.goto(`/data?tab=sync&topic=${topicId}`);
  const panel = page.getByRole("region", { name: "共享数据补数" });
  await expect(panel).toBeVisible();
  await expect(panel.getByRole("link", { name: "查看来源课题数据范围" })).toHaveAttribute("href", new RegExp(topicId));
  await panel.getByRole("textbox", { name: "搜索标的或基准" }).fill("合成");
  await panel.getByRole("button", { name: "选择标的 合成标的" }).click();
  await panel.getByRole("button", { name: "设定基准 合成基准" }).click();
  await panel.getByLabel("补数决策开始").fill("2024-01-02");
  await panel.getByLabel("补数决策结束").fill("2024-02-23");
  const submit = panel.getByRole("button", { name: "确认提交补数请求", exact: true });
  await expect(submit).toBeDisabled();
  await panel.getByRole("button", { name: "范围预检", exact: true }).click();
  await expect(panel.getByText("预检结果")).toBeVisible();
  await expect(panel.getByRole("alert")).toContainText("模拟缺少交易日历");
  await expect(submit).toBeDisabled();
  expect(wire.writes().map((request) => request.url.pathname)).toEqual(["/api/data/backfills/preview"]);
  await panel.getByRole("button", { name: "范围预检", exact: true }).click();
  await submit.click();
  await expect(panel.getByRole("alert")).toContainText("补数计划已变化，请重新预检。");
  await expect(submit).toBeDisabled();
  await panel.getByRole("button", { name: "范围预检", exact: true }).click();
  await submit.click();
  await expect(panel.getByRole("link", { name: "查看补数任务" })).toHaveAttribute("href", `/jobs?job=${jobId}`);
  await expect(page).toHaveURL(new RegExp(`backfill=${receipt.request_id}`));
  await page.reload();
  await expect(panel.getByText("采集队列测试中断。")).toBeVisible();
  await panel.getByRole("button", { name: "重试未完成项" }).click();
  const retryDialog = page.getByRole("dialog", { name: "确认续补失败项" });
  await expect(retryDialog).toBeVisible();
  expect(wire.writes().filter((entry) => entry.url.pathname.endsWith("/retry"))).toEqual([]);
  await retryDialog.getByRole("button", { name: "确认提交续补", exact: true }).click();
  await expect(panel.getByRole("link", { name: "查看补数任务" })).toHaveAttribute("href", "/jobs?job=BJ-retry");
  await expect(panel.getByRole("link", { name: releaseId })).toHaveAttribute("href", new RegExp(releaseId));
  expect(wire.unexpected).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("a topic can prefill the frozen calendar coverage and datasets", async ({ page }, info) => {
  test.skip(info.project.name !== "research", "The same form is shared by both editions.");
  await preferences(page, "zh", "dark");
  const scope = {
    universe: { explicit_symbols: ["FBDEMO01.SH"] },
    decision: { start: "2024-01-02", end: "2024-02-23" },
    benchmarks: ["FBDEMOIDX.SH"], datasets: { required: ["bars", "convertible_metrics"] },
    bars_adjustment: "qfq", warmup_sessions: 20, tail_sessions: 3,
  };
  const wire = await mockApi(page, ({ url, body }) => {
    if (url.pathname === `/api/research/topics/${topicId}/runs`) return { json: {
      items: [{ run_id: runId, run_status: "completed" }], has_more: false,
    } };
    if (url.pathname === `/api/research/runs/${runId}`) return { json: {
      ...evidenceRun, manifest: { ...evidenceRun.manifest, data_scope: { scope } },
    } };
    if (url.pathname === "/api/data/backfills/preview") {
      const spec = body.spec as { scope: typeof scope; datasets: Array<{ dataset: string; source: string | null; adjustment: string }> };
      expect(spec.scope).toMatchObject(scope);
      expect(spec.datasets).toEqual([
        expect.objectContaining({ dataset: "bars", source: null, adjustment: "qfq" }),
        expect.objectContaining({ dataset: "convertible_metrics", source: "tushare", adjustment: "none" }),
      ]);
      return { json: {
        plan_checksum: "f".repeat(64), blockers: [], warnings: [], items: [],
        quota: { estimated_total: 0, remaining_today: null }, publish_impact: { new_releases: 0 },
      } };
    }
  });
  await page.goto(`/data?tab=sync&topic=${topicId}`);
  const panel = page.getByRole("region", { name: "共享数据补数" });
  await panel.getByRole("button", { name: "预填最近研究的范围字段" }).click();
  await expect(panel.getByLabel("补数决策开始")).toHaveValue(scope.decision.start);
  await panel.getByText("数据集、来源与覆盖选项", { exact: true }).click();
  await expect(panel.getByLabel("预热交易日数")).toHaveValue("20");
  await expect(panel.getByLabel("估值尾部交易日数")).toHaveValue("3");
  await expect(panel.getByLabel("可转债指标")).toBeChecked();
  await panel.getByRole("button", { name: "范围预检" }).click();
  await expect(panel.getByText("预检结果")).toBeVisible();
  expect(wire.writes().map(({ url }) => url.pathname)).toEqual(["/api/data/backfills/preview"]);
});
