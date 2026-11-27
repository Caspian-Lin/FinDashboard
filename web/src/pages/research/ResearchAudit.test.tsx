import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import ResearchAudit from "./ResearchAudit";

afterEach(() => vi.unstubAllGlobals());

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={["/research/audit?job=BJ-example"]}>
        <ResearchAudit />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function signIn() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("审计凭据（audit.read）"), "local-audit-credential");
  await user.click(screen.getByRole("button", { name: "查询审计" }));
  return user;
}

it("用指定作用域展示审计覆盖及任务、运行、课题回链", async () => {
  const fetchAudit = vi.fn(async (path: string) => path.endsWith("/coverage")
    ? Response.json({
      server_observed_from: "2026-11-01T00:00:00Z",
      client_declared_from: null,
      gaps: ["client_declared_events_not_backfilled"],
      retention_days: 365,
    })
    : Response.json({
      items: [{
        audit_id: "RA-example", topic_id: "RT-example", trust: "server_observed",
        entity_id: "BJ-example", correlation: { run_id: "RR-example" },
        params_summary: {}, params_digest: "deadbeef",
        source: "worker", actor: "worker:test", operation: "job.failed",
        outcome: "failed", result_code: "worker_interrupted", occurred_at: "2026-11-01T00:00:00Z",
      }],
      next_cursor: null,
    }));
  vi.stubGlobal("fetch", fetchAudit);
  renderPage();
  expect(fetchAudit).not.toHaveBeenCalled();
  const user = await signIn();
  expect(await screen.findByText("job.failed")).toBeVisible();
  expect(screen.getByText(/外部客户端须主动上报/)).toBeVisible();
  expect(screen.getByRole("link", { name: "BJ-example" })).toHaveAttribute("href", "/jobs?job=BJ-example");
  expect(screen.getByRole("link", { name: "RR-example" })).toHaveAttribute("href", "/research/runs?run=RR-example");
  expect(screen.getByRole("link", { name: "RT-example" })).toHaveAttribute("href", "/research/topics?topic=RT-example");
  expect(fetchAudit).toHaveBeenCalledWith("/api/research/audit?limit=25&job_id=BJ-example", expect.objectContaining({
    cache: "no-store",
    headers: { Authorization: "Bearer local-audit-credential" },
  }));
  await user.click(screen.getByRole("button", { name: "清除凭据" }));
  expect(screen.queryByText("job.failed")).not.toBeInTheDocument();
});

it("审计凭据权限不足只显示拒绝反馈", async () => {
  const fetchAudit = vi.fn(async () => new Response(null, { status: 403 }));
  vi.stubGlobal("fetch", fetchAudit);
  renderPage();
  await signIn();
  expect(await screen.findByText("凭据无效或缺少 audit.read 权限。")).toBeVisible();
  expect(fetchAudit).toHaveBeenCalledTimes(1);
});
