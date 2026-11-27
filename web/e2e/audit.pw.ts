import { expect, test } from "@playwright/test";
import { failedJob, jobId, mockApi, preferences, runId, stamp, topicId } from "./fixtures";

test("archived failure preserves the task, scoped audit, and return path", async ({ page }, info) => {
  const research = info.project.name === "research";
  await preferences(page, "zh", research ? "light" : "dark");
  const event = {
    audit_id: "RA-fixture-archived", occurred_at: stamp, actor: "worker:test",
    source: "worker", operation: "job.failed", outcome: "failed", trust: "server_observed",
    topic_id: topicId, entity_id: jobId, correlation: { run_id: runId },
    result_code: failedJob.error_code, error_summary: failedJob.error_summary,
    params_summary: { decision: 4 }, params_digest: "f".repeat(64),
  };
  const wire = await mockApi(page, ({ url, headers }) => {
    if (url.pathname === "/api/research/audit/coverage") {
      return { json: { server_observed_from: stamp, client_declared_from: null, gaps: ["client_declared_events_not_backfilled"], retention_days: 365 } };
    }
    if (url.pathname === "/api/research/audit") {
      expect(headers.authorization).toBe("Bearer fixture-audit-reader");
      expect(url.searchParams.get("job_id")).toBe(jobId);
      return { json: { items: [event], next_cursor: null } };
    }
  });
  await page.goto(`/jobs?job=${jobId}`);
  const archivedTask = page.getByRole("region", { name: "深链任务" });
  await expect(archivedTask.getByText(failedJob.error_summary)).toBeVisible();
  await archivedTask.getByRole("link", { name: "研究审计" }).click();
  await page.getByLabel("审计凭据（audit.read）").fill("fixture-audit-reader");
  await page.getByRole("button", { name: "查询审计" }).press("Enter");
  await expect(page.getByText("job.failed", { exact: true })).toBeVisible();
  await expect(page.getByLabel("审计凭据（audit.read）")).toHaveCount(0);
  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
  if (research) {
    const filters = page.getByRole("button", { name: "筛选", exact: true });
    await expect(filters).toHaveAttribute("aria-expanded", "false");
    await filters.press("Enter");
    await expect(page.getByLabel("课题记录")).toBeVisible();
    await filters.press("Enter");
  }
  await page.getByRole("link", { name: jobId, exact: true }).click();
  await expect(archivedTask.getByText(failedJob.error_summary)).toBeVisible();
  expect(wire.writes()).toEqual([]);
});

test("task pagination preserves URL and reads the next bounded page", async ({ page }, info) => {
  test.skip(info.project.name !== "full", "Pagination is shared by both editions.");
  await preferences(page, "zh", "dark");
  const second = "BJ-FIXTURE-PAGE-2";
  const wire = await mockApi(page, ({ url }) => {
    if (url.pathname !== "/api/jobs") return undefined;
    const index = Number(url.searchParams.get("offset") ?? 0);
    const item = { ...failedJob, job_id: index ? second : jobId, archived_at: null };
    return { json: [item], headers: { "X-Total-Count": "11" } };
  });
  await page.goto("/jobs");
  await expect(page.getByText(jobId, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.getByText(second, { exact: true })).toBeVisible();
  expect(new URL(page.url()).searchParams.get("page")).toBe("2");
  expect(wire.requests.some((request) => request.url.pathname === "/api/jobs" && request.url.searchParams.get("offset") === "10")).toBe(true);
  expect(wire.writes()).toEqual([]);
});
