import { expect, test } from "@playwright/test";
import { activeTopic, mockApi, preferences, topicId } from "./fixtures";

test("create a manual draft in an empty workspace without starting work", async ({ page }, info) => {
  test.skip(info.project.name !== "research", "Exercise the empty path in the research edition.");
  await preferences(page, "zh", "light");
  const manualId = "RT-manual-draft";
  let draft: Record<string, unknown> | undefined;
  const wire = await mockApi(page, ({ method, url, body }) => {
    if (url.pathname === "/api/research/topics" && method === "POST") {
      draft = { ...activeTopic, ...body, topic_id: manualId, status: "draft", goal: null, revision: 1 };
      return { status: 201, json: draft };
    }
    if (url.pathname === "/api/research/topics") return { json: { items: draft ? [draft] : [], total: draft ? 1 : 0 } };
    if (url.pathname === `/api/research/topics/${manualId}`) return { json: draft };
    if (url.pathname === `/api/research/topics/${manualId}/entries`) return { json: { items: [], total: 0 } };
    if (url.pathname.endsWith("/coverage")) return { status: 404, json: { detail: "The draft has no source data yet." } };
  });
  await page.goto("/research/topics");
  await expect(page.getByRole("heading", { name: "研究课题", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "新建课题" }).click();
  await page.getByLabel("课题标题", { exact: true }).fill("空库首个研究问题");
  await page.getByLabel("研究问题", { exact: true }).fill("组合集中度变化能否解释观察到的收益差?");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(page).toHaveURL(`/research/topics?topic=${manualId}`);
  await expect(page.getByText("空库首个研究问题", { exact: true }).first()).toBeVisible();
  expect(wire.writes().map((request) => request.url.pathname)).toEqual(["/api/research/topics"]);
});

test("agent handoff opens the original topic from the old workspace link", async ({ page }, info) => {
  test.skip(info.project.name !== "research", "Exercise the old agent handoff under the research edition.");
  await preferences(page, "zh", "dark");
  const wire = await mockApi(page, ({ url }) => {
    if (url.pathname.endsWith("/coverage")) return { status: 404, json: { detail: "No fixture coverage yet" } };
  });
  await page.goto(`/research/workbench?topic=${topicId}#handoff`);
  await expect(page.getByText(/finboard_topic_read\(topic_id=/)).toContainText(topicId);
  await page.getByRole("link", { name: /回到课题/ }).click();
  await expect(page).toHaveURL(`/research/topics?topic=${topicId}`);
  await expect(page.getByText(activeTopic.title, { exact: true }).first()).toBeVisible();
  expect(wire.writes()).toEqual([]);
});
