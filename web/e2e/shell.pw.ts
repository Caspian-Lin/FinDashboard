import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { mockApi, preferences, releaseId, topicId } from "./fixtures";

test("window controls reserve their strip only in the desktop workbench", async ({ page }, info) => {
  test.skip(info.project.name !== "research", "The desktop workbench runs the research edition.");
  await preferences(page, "en", "dark");
  await mockApi(page);
  const header = page.locator("[data-finboard-workbench-header]");
  const toolbar = page.locator("[data-finboard-workbench-toolbar]");
  const frame = page.locator("finboard-window-frame");

  await page.goto("/research/strategy");
  await expect(header).toHaveCSS("padding-right", "16px");
  await expect(frame).toHaveCount(0);

  const script = readFileSync(new URL("../../desktop/ui/window-frame.js", import.meta.url), "utf8");
  const stylesheet = readFileSync(new URL("../../desktop/ui/window-frame.css", import.meta.url), "utf8");
  await page.addInitScript({ content: `
    window.__TAURI__ = { core: { invoke: async (_command, { action }) => {
      if (action !== "is_maximized") document.documentElement.dataset.fixtureWindowAction = action;
      return false;
    } } };
    (${script})({
      window: "research", origin: location.origin, stylesheet: ${JSON.stringify(stylesheet)},
      researchCaption: { buttonWidth: 44, height: 34, top: 11, end: 8, dragWidth: 48 },
    });
  ` });
  await page.goto("/research/strategy");
  await expect(header).toHaveCSS("padding-right", "188px");
  await expect(frame.getByRole("button", { name: "Maximize" })).toBeVisible();

  for (const width of [1020, 640, 330]) {
    await page.setViewportSize({ width, height: 730 });
    const [bar, tools, controls] = await Promise.all([
      header.boundingBox(), toolbar.boundingBox(), frame.boundingBox(),
    ]);
    if (!bar || !tools || !controls) throw new Error("Missing desktop header bounds");
    expect(tools.x + tools.width + 47).toBeLessThanOrEqual(controls.x);
    expect(Math.abs(bar.y + bar.height / 2 - (controls.y + controls.height / 2))).toBeLessThanOrEqual(1);
    expect(tools.x).toBeGreaterThanOrEqual(bar.x);
  }

  await header.dispatchEvent("mousedown", { button: 0, buttons: 1, detail: 1 });
  await expect(page.locator("html")).toHaveAttribute("data-fixture-window-action", "start_dragging");
  await toolbar.getByRole("button", { name: "Research tools" }).click();
  await expect(toolbar.getByRole("button", { name: "Research tools" })).toHaveAttribute("aria-expanded", "true");
});

test("home, keyboard skip, edition boundaries, and old URLs", async ({ page }, info) => {
  const research = info.project.name === "research";
  await preferences(page, research ? "en" : "zh", research ? "light" : "dark");
  const wire = await mockApi(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: research ? "Research overview" : "工作总览", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
  await expect(page.getByRole("link", { name: "Check exposure drift" })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("lang", research ? "en" : "zh-CN");
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus")).toHaveAttribute("href", "#main-content");
  await page.keyboard.press("Enter");
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

  if (research) {
    await page.getByRole("button", { name: "Open menu" }).click();
    const menu = page.getByRole("dialog", { name: "Navigation menu" });
    await expect(menu).toBeVisible();
    await expect(menu.getByRole("link", { name: "Live trading", exact: true })).toHaveCount(0);
    await menu.getByRole("link", { name: "Settings", exact: true }).click();
    await expect(menu).toBeHidden();
    await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();

    for (const path of ["/live", "/positions", "/orders", "/fills", "/control"]) {
      await page.goto(`${path}?order=OD-fixture#detail`);
      await expect(page.getByRole("heading", { name: "Live trading requires the full edition" })).toBeVisible();
      expect(new URL(page.url()).search + new URL(page.url()).hash).toBe("?order=OD-fixture#detail");
    }
    await page.goto("/research/simulation?session=SIM-S-fixture");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("full edition");
    expect(wire.requests.filter((entry) => /^\/api\/(account|orders|positions|fills|control|health|simulation)(\/|$)/.test(entry.url.pathname))).toEqual([]);
  } else {
    await page.getByRole("link", { name: "实盘", exact: true }).click();
    await expect(page).toHaveURL(/\/live$/);
  }

  await page.goto(`/research/workbench?topic=${topicId}#handoff`);
  await expect(page).toHaveURL(`/research/agent-connect?topic=${topicId}#handoff`);
  await page.goto(`/research/data?tab=releases&release=${releaseId}#data`);
  await expect(page).toHaveURL(/\/data\?/);
  expect(new URL(page.url()).searchParams.get("release")).toBe(releaseId);
  expect(new URL(page.url()).hash).toBe("#data");
  await page.goto("/strategies?preset=example#saved");
  await expect(page).toHaveURL(/\/backtest\?preset=example#saved$/);
  await page.goto("/fixture-route-missing");
  await expect(page.getByRole("heading", { level: 1 })).toContainText(research ? /does not exist|has been moved/i : /不存在|已被移动/);
  await expect(page.getByRole("main").getByRole("link", { name: research ? /Research/i : /研究/ })).toHaveAttribute("href", "/research");
  expect(wire.unexpected).toEqual([]);
});

test("settings guides empty research instances and resumes after an import", async ({ page }, info) => {
  test.skip(info.project.name !== "research", "The empty-instance guide belongs to the research edition.");
  await preferences(page, "zh", "dark");
  let imported = false;
  await mockApi(page, ({ url }) => {
    if (url.pathname === "/api/data/catalog") return { json: imported
      ? { total: 1, items: [{ status: "stale", release: { release_id: "synthetic-starter-v1" } }] }
      : { total: 0, items: [] } };
  });
  await page.goto("/settings");
  await expect(page.getByText(/当前没有可用发布/)).toBeVisible();
  await expect(page.getByText(/import --bundle/)).toBeVisible();
  await expect(page.getByText("未配置", { exact: true })).toBeVisible();
  await page.getByLabel("界面语言").selectOption("en");
  await page.getByRole("combobox", { name: /Theme/ }).selectOption("light");
  await expect(page.locator("html")).toHaveClass("light");
  await expect(page.getByRole("heading", { name: "System Diagnostics", exact: true })).toBeVisible();
  imported = true;
  await page.getByRole("button", { name: "Refresh Diagnostics" }).click();
  await expect(page.getByRole("link", { name: "Open Research Topics" })).toHaveAttribute("href", "/research/topics");
  await expect(page.getByRole("link", { name: "Open Strategy Studio" })).toHaveAttribute("href", "/research/strategy");
});

test("live pages only read fixtures and dismiss a cancellation confirmation", async ({ page }, info) => {
  test.skip(info.project.name !== "full", "Only the full edition displays the existing live surfaces.");
  await preferences(page, "zh", "dark");
  const wire = await mockApi(page);
  await page.goto("/orders");
  await expect(page.getByRole("row", { name: /510300.SH/ })).toHaveCount(1);
  await page.getByRole("row", { name: /510300.SH/ }).getByRole("button", { name: "撤单", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("ORD-FIXTURE-ONLY");
  await expect(dialog.getByRole("button", { name: /确认撤/ })).toBeVisible();
  expect(wire.writes()).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  for (const path of ["/live", "/positions", "/fills", "/control"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
  }
  expect(wire.writes()).toEqual([]);
  expect(wire.unexpected).toEqual([]);
});

test("direct navigation renders the remaining shared routes with one page heading", async ({ page }, info) => {
  const research = info.project.name === "research";
  await preferences(page, research ? "en" : "zh", research ? "light" : "dark");
  const wire = await mockApi(page);
  for (const path of [
    "/research", "/research/tools", "/research/factors", "/research/strategy", "/research/strategy-explanation",
    "/research/portfolio", "/research/docs", "/backtest",
  ]) {
    await test.step(path, async () => {
      await page.goto(path);
      await expect(page).toHaveURL(path);
      const heading = page.getByRole("main").getByRole("heading", { level: 1 });
      await expect(heading).toBeVisible();
      await expect(heading).not.toContainText(/页面出错了|Something went wrong/);
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    });
  }
  expect(wire.writes()).toEqual([]);
});
