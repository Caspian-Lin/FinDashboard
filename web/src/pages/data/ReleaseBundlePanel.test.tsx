import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { ReleaseBundlePanel } from "./ReleaseBundlePanel";

vi.mock("@/i18n", () => ({
  useT: () => ({ lang: "en", tl: (value: { en: string }) => value.en }),
}));

const requests: PreviewRequest[] = [];

class PreviewRequest extends EventTarget {
  upload = new EventTarget();
  status = 0;
  response: unknown;
  responseType = "";
  file?: File;

  open() {}
  setRequestHeader() {}
  send(file: File) {
    this.file = file;
    requests.push(this);
  }

  complete(conflicts: string[] = []) {
    this.status = 200;
    this.response = {
      format: "finboard-release-bundle-v1",
      bundle_id: "RB-123",
      checksum: "a".repeat(64),
      unpacked_bytes: 1024 * 1024,
      archive_bytes: 70_000,
      release_conflicts: conflicts,
      calendar_conflict: false,
      releases: [{
        release_id: "bars-demo-v1", dataset_name: "bars", version: "202401", source: "fixture",
        adjustment: "qfq", start_date: "2024-01-02", end_date: "2024-01-31",
        symbol_count: 51,
      }],
    };
    this.dispatchEvent(new Event("load"));
  }
}

function show() {
  vi.stubGlobal("XMLHttpRequest", PreviewRequest);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ReleaseBundlePanel entries={[]} onImported={vi.fn()} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  requests.length = 0;
  vi.unstubAllGlobals();
});

describe("importing a frozen bundle", () => {
  it("requires explicit confirmation after preview", async () => {
    const fetchMock = vi.fn(async (path: string) => new Response(JSON.stringify(
      path === "/api/data/bundles/imports"
        ? { job_id: "BJ-bundle" }
        : { job_id: "BJ-bundle", status: "succeeded" },
    )));
    vi.stubGlobal("fetch", fetchMock);
    show();
    expect(screen.queryByRole("button", { name: "Confirm import" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Local ZIP bundle"), {
      target: { files: [new File(["bundle"], "example.zip", { type: "application/zip" })] },
    });
    fireEvent.click(screen.getByRole("button", { name: "Upload and preview" }));
    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0].file?.name).toBe("example.zip");
    requests[0].complete();
    expect(await screen.findByText("bars-demo-v1")).toBeInTheDocument();
    expect(screen.getByText("finboard-release-bundle-v1")).toBeInTheDocument();
    expect(screen.getByText("bars · 202401")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Confirm import" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/data/bundles/imports",
      expect.objectContaining({ method: "POST", body: JSON.stringify({
        bundle_id: "RB-123", checksum: "a".repeat(64),
      }) }),
    ));
    expect(await screen.findByRole("link", { name: "View job" })).toHaveAttribute("href", "/jobs?job=BJ-bundle");
  });

  it("blocks confirmation when a release conflicts", async () => {
    show();
    fireEvent.change(screen.getByLabelText("Local ZIP bundle"), {
      target: { files: [new File(["bundle"], "conflict.zip")] },
    });
    fireEvent.click(screen.getByRole("button", { name: "Upload and preview" }));
    await waitFor(() => expect(requests).toHaveLength(1));
    requests[0].complete(["bars-demo-v1"]);
    expect(await screen.findByRole("alert")).toHaveTextContent(/release identity or trading calendar conflicts/i);
    expect(screen.getByRole("button", { name: "Confirm import" })).toBeDisabled();
  });
});
