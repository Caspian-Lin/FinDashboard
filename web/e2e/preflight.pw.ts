import { expect, test } from "@playwright/test";
import { activeTopic, evidenceRun, jobId, mockApi, preferences, releaseId, runId, topicId } from "./fixtures";

test("published strategy run keeps preflight separate from worker submission", async ({ page }, info) => {
  test.skip(info.project.name !== "research", "Run the main configuration path against the research edition.");
  await preferences(page, "zh", "dark");
  const checksum = "a".repeat(64);
  const strategy = { strategy_id: "strategy-e2e", strategy_kind: "multi_factor", name: "冻结策略样例", published: true, version: 2,
    spec: { validation_plan: { dataset_release_ids: [releaseId], benchmark_symbol: "000001.SH", test_start: "2024-01-02", test_end: "2024-02-23" } } };
  const wire = await mockApi(page, ({ url, method, body }) => {
    if (url.pathname === "/api/research/strategy-specs") return { json: [strategy] };
    if (url.pathname === "/api/research/runs/preflight") {
      expect(body.strategy_id).toBe(strategy.strategy_id);
      expect(body.strategy_version).toBe(strategy.version);
      expect(body.dataset_release_ids).toEqual([releaseId]);
      return { json: { run_id: runId, manifest_checksum: checksum, manifest: evidenceRun.manifest } };
    }
    if (url.pathname === "/api/research/runs" && method === "POST") {
      expect(body.expected_manifest_checksum).toBe(checksum);
      return { status: 202, json: { ...evidenceRun, status: "queued" } };
    }
  });
  await page.goto("/research/runs");
  await page.getByRole("button", { name: "排队研究运行", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "新建研究运行" });
  await dialog.getByLabel("已发布策略版本").selectOption({ label: "冻结策略样例 · v2" });
  await expect(dialog.getByRole("button", { name: "启动研究运行" })).toBeDisabled();
  await dialog.getByRole("button", { name: "预览生效输入" }).click();
  await expect(dialog.getByText("预检通过")).toBeVisible();
  expect(wire.writes().map((request) => request.url.pathname)).toEqual(["/api/research/runs/preflight"]);
  await dialog.getByRole("button", { name: "启动研究运行" }).click();
  await expect(dialog).toBeHidden();
  await expect(page).toHaveURL(`/research/runs?run=${runId}`);
  expect(wire.writes().map((request) => request.url.pathname)).toEqual(["/api/research/runs/preflight", "/api/research/runs"]);
  expect(wire.unexpected).toEqual([]);
});

test("formal OOS registration requires preflight and leaves the holdout sealed", async ({ page }, info) => {
  test.skip(info.project.name !== "research", "Run the frozen formal workflow in the research edition.");
  await preferences(page, "zh", "light");
  const checksum = "b".repeat(64);
  const experimentId = "VE-new-frozen";
  const wire = await mockApi(page, ({ url, method, body }) => {
    if (url.pathname === `/api/research/experiments/${experimentId}`) {
      return { json: { experiment_id: experimentId, status: "hypothesis", version_stamp: {},
        hypothesis: "冻结规格的独立证据", plan: {}, thresholds: {}, robustness: {}, trials: [], created_at: evidenceRun.created_at } };
    }
    if (method === "POST" && url.pathname === `/api/research/topics/${topicId}/actions/preview`) {
      const payload = body.payload as { plan: { benchmark_symbol: string } };
      expect(payload.plan.benchmark_symbol).toBe("000001.SH");
      return { json: {
        preview_checksum: checksum, version_checksum: "c".repeat(64), baseline_run_id: runId, strategy_version: 2,
        dataset_releases: [{ artifact_id: releaseId, version: "v2", checksum: "d".repeat(64) }],
        plan: payload.plan, thresholds: {}, fee_config: {}, portfolio_config: {}, used_windows: [],
      } };
    }
    if (method === "POST" && url.pathname === `/api/research/topics/${topicId}/actions`) {
      expect(body.action).toBe("validation_experiment.create");
      expect(body.payload).toEqual(expect.objectContaining({ expected_preview_checksum: checksum }));
      return { status: 201, json: { entity_id: experimentId, job_id: null } };
    }
  });
  await page.goto("/research/experiments");
  await page.getByRole("button", { name: "正式规格验证", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "正式规格验证" });
  await dialog.getByLabel("进行中的课题").selectOption({ label: activeTopic.title });
  await dialog.getByLabel("已完成的基线运行").selectOption(runId);
  await dialog.getByLabel("研究假设").fill("冻结规格在独立最终窗口内仍保留正超额收益");
  const labels = ["训练开始", "训练结束", "验证开始", "验证结束", "最终测试开始", "最终测试结束"];
  const dates = ["2022-01-01", "2022-12-31", "2023-01-01", "2023-12-31", "2024-01-01", "2024-12-31"];
  for (let index = 0; index < labels.length; index += 1) await dialog.getByLabel(labels[index], { exact: true }).fill(dates[index]);
  await dialog.getByRole("checkbox", { name: /已列出所有额外/ }).check();
  await expect(dialog.getByRole("button", { name: "登记实验", exact: true })).toBeDisabled();
  await dialog.getByRole("button", { name: "预检生效输入" }).click();
  await expect(dialog.getByText(releaseId, { exact: true })).toBeVisible();
  expect(wire.writes().length).toBe(1);
  await dialog.getByRole("button", { name: "登记实验", exact: true }).click();
  await expect(page.getByRole("link", { name: "回原课题执行与复核" })).toHaveAttribute("href", `/research/topics?topic=${topicId}`);
  expect(wire.writes().length).toBe(2);
  expect(wire.writes().some((request) => request.body.action === "validation_experiment.unseal" || request.body.job_id === jobId)).toBe(false);
});
