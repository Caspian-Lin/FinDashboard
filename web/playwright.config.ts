import { defineConfig, devices } from "@playwright/test";

const fullPort = Number(process.env.FINBOARD_E2E_FULL_PORT ?? 5386);
const researchPort = Number(process.env.FINBOARD_E2E_RESEARCH_PORT ?? 5387);

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.pw.ts",
  timeout: 60_000,
  expect: { timeout: 20_000 },
  workers: 2,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: { trace: "retain-on-failure", screenshot: "only-on-failure", reducedMotion: "reduce" },
  projects: [
    {
      name: "full",
      use: { ...devices["Desktop Chrome"], baseURL: `http://127.0.0.1:${fullPort}`, viewport: { width: 1500, height: 930 } },
    },
    {
      name: "research",
      use: { ...devices["Pixel 7"], defaultBrowserType: "chromium", baseURL: `http://127.0.0.1:${researchPort}`, viewport: { width: 390, height: 844 } },
    },
  ],
  webServer: [
    { port: fullPort, edition: "false" },
    { port: researchPort, edition: "true" },
  ].map(({ port, edition }) => ({
    command: `npm run dev -- --host 127.0.0.1 --port ${port} --strictPort`,
    url: `http://127.0.0.1:${port}/`,
    env: { VITE_FINBOARD_RESEARCH_EDITION: edition },
    reuseExistingServer: false,
  })),
});
