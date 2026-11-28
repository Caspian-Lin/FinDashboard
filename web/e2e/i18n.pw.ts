import { expect, test } from "@playwright/test";
import { activeTopic, evidenceRun, failedJob, jobId, mockApi, preferences, releaseId, runId, topicId } from "./fixtures";

test.beforeEach(async ({ page }, info) => {
  test.skip(info.project.name !== "research", "Check English copy in the narrow research workspace.");
  await preferences(page, "en", "dark");
});

test("English topic creation and Agent handoff preserve authored text", async ({ page }) => {
  const draftId = "RT-english-manual";
  const title = "核对组合敞口";
  const criteria = "保留这段作者原文";
  let draft: Record<string, unknown> | undefined;
  const wire = await mockApi(page, ({ method, url, body }) => {
    if (url.pathname === "/api/research/topics") {
      if (method === "POST") {
        draft = { ...activeTopic, ...body, topic_id: draftId, status: "draft", goal: null, revision: 1 };
        return { status: 201, json: draft };
      }
      return { json: { items: draft ? [draft] : [], total: draft ? 1 : 0 } };
    }
    if (url.pathname === `/api/research/topics/${draftId}/activate` && method === "POST") {
      expect(body.goal).toEqual(expect.objectContaining({ criteria, source: { kind: "document", ref_id: "ROADMAP.md" } }));
      draft = { ...draft, status: "active", goal: body.goal, revision: 2 };
      return { json: draft };
    }
    if (url.pathname === `/api/research/topics/${topicId}/artifacts`) return { json: { items: [{
      entity_type: "validation_experiment", entity_id: "VE-sealed-i18n", state: "pending", goal_version: "v1",
      status: "hypothesis", final_test_unsealed: false, evidence_gaps: [],
    }], total: 1 } };
    if (url.pathname === "/api/research/docs") return { json: { docs: [{ path: "ROADMAP.md", title: "研究路线" }] } };
    if (url.pathname === `/api/research/topics/${draftId}`) return { json: draft };
    if (url.pathname === `/api/research/topics/${draftId}/context` || url.pathname.endsWith("/coverage")) {
      return { status: 404, json: { detail: "Fixture coverage has not been recorded." } };
    }
    if (url.pathname.startsWith(`/api/research/topics/${draftId}/`)) return { json: { items: [], total: 0, has_more: false } };
  });
  await page.goto("/research/topics");
  await expect(page.getByRole("heading", { name: "Research topics", exact: false })).toBeVisible();
  await page.getByRole("button", { name: "New topic", exact: true }).press("Enter");
  await page.getByLabel("Topic title", { exact: true }).fill(title);
  await page.getByLabel("Research question", { exact: true }).fill("Is portfolio exposure stable?");
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect(page).toHaveURL(`/research/topics?topic=${draftId}`);
  await page.getByRole("button", { name: "Complete goal and activate" }).click();
  await page.getByLabel("Goal version", { exact: true }).fill("v1");
  await page.getByLabel("Goal source (canonical document)").selectOption({ label: "研究路线" });
  await page.getByLabel("Goal and acceptance criteria", { exact: true }).fill(criteria);
  await page.getByRole("button", { name: "Activate topic", exact: true }).click();
  await expect(page.getByRole("button", { name: "Start research run", exact: true })).toBeVisible();
  await expect(page.getByText(title, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(criteria, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/Beijing time/).first()).toBeVisible();

  await page.goto(`/research/workbench?topic=${topicId}#handoff`);
  await page.getByRole("link", { name: /Back to topic/ }).press("Enter");
  await expect(page).toHaveURL(`/research/topics?topic=${topicId}`);
  await expect(page.getByText(activeTopic.title, { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "Run one-time holdout" }).press("Enter");
  const holdout = page.getByRole("dialog", { name: "Confirm one-time holdout" });
  await expect(holdout.getByRole("button", { name: "Confirm and use final window" })).toBeVisible();
  await holdout.getByRole("button", { name: "Back", exact: true }).press("Enter");
  await expect(holdout).toBeHidden();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(wire.writes().map(({ url }) => url.pathname)).toEqual(["/api/research/topics", `/api/research/topics/${draftId}/activate`]);
});

test("English run preflight labels the failure and keeps the server reason", async ({ page }) => {
  let attempts = 0;
  const rawReason = "模拟数据发布暂时不可用";
  const strategyName = "冻结策略样例";
  const run = { ...evidenceRun, manifest: { ...evidenceRun.manifest, parameters: {
    ...evidenceRun.manifest.parameters, decision_schedule: { kind: "custom", dates: ["2024-01-02", "2024-01-10"] },
  } } };
  const wire = await mockApi(page, ({ url, method }) => {
    if (url.pathname === "/api/research/strategy-specs") return { json: [{
      strategy_id: "published-i18n", name: strategyName, published: true, strategy_kind: "multi_factor", version: 2,
      spec: { validation_plan: { dataset_release_ids: [releaseId], benchmark_symbol: "000001.SH" } },
    }] };
    if (url.pathname === `/api/research/runs/${runId}`) return { json: run };
    if (url.pathname === "/api/research/runs/preflight" && method === "POST") {
      attempts += 1;
      return attempts === 1
        ? { status: 422, json: { detail: rawReason } }
        : { json: { run_id: runId, manifest_checksum: "f".repeat(64), manifest: run.manifest } };
    }
  });
  await page.goto(`/research/runs?run=${runId}`);
  await expect(page.getByRole("tab", { name: "Strategy explanation" })).toBeVisible();
  await expect(page.getByText("Decision calendar: 2024-01-02 to 2024-01-10 (2 decisions)")).toBeVisible();
  await page.getByRole("button", { name: "Queue research run" }).press("Enter");
  const dialog = page.getByRole("dialog", { name: "New research run" });
  await dialog.getByLabel("Published strategy version").selectOption({ label: `${strategyName} · v2` });
  const preview = dialog.getByRole("button", { name: "Preview effective inputs" });
  await preview.press("Enter");
  await expect(dialog.getByRole("alert")).toContainText("Research preflight failed");
  await expect(dialog.getByRole("alert")).toContainText(rawReason);
  await expect(dialog.getByRole("button", { name: "Start research run" })).toBeDisabled();
  await preview.click();
  await expect(dialog.getByText("Preflight passed")).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel" }).press("Enter");
  await expect(dialog).toBeHidden();
  expect(wire.writes().map(({ url }) => url.pathname)).toEqual(["/api/research/runs/preflight", "/api/research/runs/preflight"]);

  await page.goto("/research/experiments");
  await expect(page.getByRole("combobox", { name: "Workflow status" })).toBeVisible();
  await page.getByRole("combobox", { name: "Filter by OOS outcome" }).press("Enter");
  await expect(page.getByRole("option", { name: "All outcomes" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Formal Strategy Validation" }).press("Enter");
  const formal = page.getByRole("dialog", { name: "Formal Strategy Validation" });
  await expect(formal.getByLabel("Active topic")).toBeVisible();
  await expect(formal.getByLabel("Final test start", { exact: true })).toBeVisible();
  await expect(formal.getByRole("button", { name: "Register Experiment" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(formal).toBeHidden();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("English settings, audit and backfill retain actions and raw diagnostics", async ({ page }) => {
  const rawBlocker = "当前缺少交易日历";
  const wire = await mockApi(page, ({ url }) => {
    if (url.pathname === "/api/research/audit/coverage") {
      return { json: { server_observed_from: failedJob.created_at, client_declared_from: null, gaps: [], retention_days: 365 } };
    }
    if (url.pathname === "/api/research/audit") return { json: { items: [], next_cursor: null } };
    if (url.pathname === "/api/data/instruments") return { json: { items: [
      { code: "FBDEMO01.SH", name: "合成标的", market: "a_share", instrument_type: "stock" },
    ], total: 1 } };
    if (url.pathname === "/api/data/backfills/preview") return { json: {
      plan_checksum: "d".repeat(64), items: [], warnings: [], blockers: [{ code: "missing_calendar", message: rawBlocker }],
      quota: { status: "ok", estimated_total: 0, remaining_today: null }, publish_impact: { new_releases: 0 },
    } };
  });
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "System Diagnostics" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Refresh Diagnostics" })).toBeVisible();
  await page.goto(`/jobs?job=${jobId}`);
  const task = page.getByRole("region", { name: "Linked job" });
  await expect(task.getByText(failedJob.error_summary)).toBeVisible();
  await task.getByRole("link", { name: "Research audit" }).click();
  await page.getByLabel("Audit credential (audit.read)").fill("fixture-audit");
  await page.getByRole("button", { name: "Read audit" }).press("Enter");
  await expect(page.getByText("No audit records in this scope", { exact: true }).first()).toBeVisible();
  await expect(page.getByLabel("Audit credential (audit.read)")).toHaveCount(0);
  const filters = page.getByRole("button", { name: "Filters", exact: true });
  await filters.press("Enter");
  await expect(filters).toHaveAttribute("aria-expanded", "true");
  await filters.press("Enter");
  await expect(filters).toHaveAttribute("aria-expanded", "false");

  await page.goto("/data?tab=sync");
  const backfill = page.getByRole("region", { name: "Shared data backfill" });
  await backfill.getByRole("textbox", { name: "Search instruments or benchmarks" }).fill("合成");
  await backfill.getByRole("button", { name: "Select instrument 合成标的" }).click();
  await backfill.getByLabel("Backfill decision start").fill("2024-01-02");
  await backfill.getByLabel("Backfill decision end").fill("2024-01-31");
  await backfill.getByRole("button", { name: "Preview Scope" }).press("Enter");
  await expect(backfill.getByText("Backfill plan blocked")).toBeVisible();
  await expect(backfill.getByRole("alert")).toHaveText(rawBlocker);
  await expect(backfill.getByRole("button", { name: "Submit Backfill Request" })).toBeDisabled();
  expect(wire.writes().map(({ url }) => url.pathname)).toEqual(["/api/data/backfills/preview"]);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
