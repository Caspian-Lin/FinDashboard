import { expect, test } from "@playwright/test";
import { expId, mockApi, preferences, runId, sessionId } from "./fixtures";

test("paper results preserve partial evidence and unsupported OOS after reveal", async ({ page }) => {
  await preferences(page, "zh", "dark");
  const wire = await mockApi(page);
  await page.goto(`/research/reports?run=${runId}`);
  await expect(page.getByRole("region", { name: "结果概览" })).toBeVisible();
  await expect(page.getByText("缺少基准证据，暂不可比")).toBeVisible();
  const exportHref = await page.getByRole("link", { name: "CSV" }).getAttribute("href");
  expect(exportHref).toContain(runId);
  await page.reload();
  await expect(page.getByRole("region", { name: "结果概览" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

  await page.goto(`/research/experiments?experiment=${expId}`);
  await expect(page.getByText("OOS：假设未获支持").first()).toBeVisible();
  await expect(page.getByText("已揭盲 · 最终窗口已使用")).toBeVisible();
  await expect(page.getByText("合成验证未达门槛")).toBeVisible();
  expect(wire.writes()).toEqual([]);
});

test("paper simulation shows latest decision, blocked progress, and audit", async ({ page }, info) => {
  test.skip(info.project.name !== "full", "Paper simulation is only available in the full edition.");
  await preferences(page, "zh", "light");
  const wire = await mockApi(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/research/simulation?session=${sessionId}`);
  await expect(page.getByText("最近决策", { exact: true })).toBeVisible();
  await expect(page.getByText("会话已暂停", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "风险与审计" }).click();
  await expect(page.getByText(/fixture_rejection/)).toBeVisible();
  await expect(page.getByText(/fixture ·/)).toBeVisible();
  const decisions = wire.requests.find((request) => request.url.pathname.endsWith("/decisions"));
  expect(decisions?.url.searchParams.get("limit")).toBe("1");
  expect(decisions?.url.searchParams.get("newest_first")).toBe("true");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(wire.writes()).toEqual([]);
});
