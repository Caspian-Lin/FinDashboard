import * as React from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, fetchJSON, TERMINAL_JOB_STATUSES, type JobOut } from "@/lib/api";
import { catalogApi, type CatalogEntry, type CatalogRelease } from "@/lib/data-catalog";
import { useT } from "@/i18n";

interface BundleRelease {
  release_id: string;
  dataset_name: string;
  source: string;
  adjustment: string;
  start_date: string;
  end_date: string;
  symbol_count: number;
}

interface BundlePreview {
  bundle_id: string;
  checksum: string;
  archive_bytes: number;
  unpacked_bytes: number;
  releases: BundleRelease[];
  release_conflicts: string[];
  calendar_conflict: boolean;
}

const portableKinds = new Set(["bars", "daily_metrics", "financial_indicators", "convertible_metrics"]);

function uploadForPreview(file: File, onProgress: (percent: number) => void): Promise<BundlePreview> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/api/data/bundles/preview");
    request.setRequestHeader("Content-Type", "application/zip");
    request.responseType = "json";
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress(Math.floor((event.loaded / event.total) * 100));
    });
    request.addEventListener("load", () => {
      if (request.status >= 200 && request.status < 300 && request.response) {
        resolve(request.response as BundlePreview);
      } else {
        const detail = (request.response as { detail?: unknown } | null)?.detail;
        reject(new Error(typeof detail === "string" ? detail : `${request.status}`));
      }
    });
    request.addEventListener("error", () => reject(new Error("bundle_connection_lost")));
    request.send(file);
  });
}

function prettySize(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
  return `${(bytes / 1024 ** 2).toFixed(2)} MiB`;
}

function useBundleJob(jobId: string | undefined) {
  return useQuery({
    queryKey: ["release-bundle-job", jobId],
    queryFn: () => api.getJob(jobId ?? ""),
    enabled: Boolean(jobId),
    refetchInterval: (query) =>
      query.state.data && TERMINAL_JOB_STATUSES.has(query.state.data.status) ? false : 1500,
  });
}

function BundleActivity({ job, jobId }: { job?: JobOut; jobId: string }) {
  const { tl } = useT();
  const phase = job?.phase;
  const labels = {
    bundle_validate: tl({ zh: "核对清单", en: "Checking manifests" }),
    bundle_export: tl({ zh: "正在导出", en: "Exporting" }),
    bundle_import: tl({ zh: "正在校验并登记", en: "Verifying and registering" }),
    bundle_complete: tl({ zh: "已完成", en: "Complete" }),
  };
  const status = job?.status;
  let message = tl({ zh: "已提交任务", en: "Job submitted" });
  if (status === "succeeded") message = labels.bundle_complete;
  else if (status === "failed") message = tl({ zh: "任务失败", en: "Job failed" });
  else if (status === "interrupted" || status === "cancelled")
    message = tl({ zh: "任务已停止，可以重新提交", en: "Job stopped. You can submit it again." });
  else if (phase && phase in labels) message = labels[phase as keyof typeof labels];
  return (
    <div role="status" className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
      <span>{message}</span>
      {status === "failed" && job?.error_summary && (
        <span className="break-words text-destructive">{job.error_summary}</span>
      )}
      <Link className="underline" to={`/jobs?job=${encodeURIComponent(jobId)}`}>
        {tl({ zh: "查看任务", en: "View job" })}
      </Link>
    </div>
  );
}

export function ReleaseBundlePanel({
  entries, onImported,
}: {
  entries: CatalogEntry[];
  onImported: () => void;
}) {
  const { tl, lang } = useT();
  const section = React.useRef<HTMLElement>(null);
  const [dataset, setDataset] = React.useState("");
  const [chosen, setChosen] = React.useState<CatalogRelease[]>([]);
  const [file, setFile] = React.useState<File>();
  const [preview, setPreview] = React.useState<BundlePreview>();
  const [percent, setPercent] = React.useState(0);
  const [exportId, setExportId] = React.useState<string>();
  const [importId, setImportId] = React.useState<string>();
  const notified = React.useRef<string | null>(null);

  React.useEffect(() => {
    if (window.location.hash === "#release-bundles") section.current?.scrollIntoView?.();
  }, []);

  const options = entries.filter(
    (entry) => entry.release && portableKinds.has(entry.release.dataset_kind),
  );
  const releases = useQuery({
    queryKey: ["data-catalog", "release-bundle-options", dataset],
    queryFn: () => catalogApi.dataset(dataset),
    enabled: !!dataset,
  });
  const exportJob = useBundleJob(exportId);
  const importJob = useBundleJob(importId);
  React.useEffect(() => {
    if (importJob.data?.status === "succeeded" && notified.current !== importJob.data.job_id) {
      notified.current = importJob.data.job_id;
      onImported();
    }
  }, [importJob.data, onImported]);

  const inspect = useMutation({
    mutationFn: (value: File) => uploadForPreview(value, setPercent),
    onMutate: () => { setPreview(undefined); setPercent(0); },
    onSuccess: (result) => setPreview(result),
  });
  const submitImport = useMutation({
    mutationFn: (value: BundlePreview) => fetchJSON<JobOut>("/data/bundles/imports", {
      method: "POST",
      body: JSON.stringify({ bundle_id: value.bundle_id, checksum: value.checksum }),
    }),
    onSuccess: (job) => setImportId(job.job_id),
  });
  const submitExport = useMutation({
    mutationFn: (releaseIds: string[]) => fetchJSON<{ job_id: string }>("/data/bundles/exports", {
      method: "POST",
      body: JSON.stringify({ release_ids: releaseIds }),
    }),
    onSuccess: (job) => setExportId(job.job_id),
  });
  const imported = importJob.data?.status === "succeeded";
  const busyImport = Boolean(importId && !TERMINAL_JOB_STATUSES.has(importJob.data?.status ?? "queued"));
  const busyExport = Boolean(exportId && !TERMINAL_JOB_STATUSES.has(exportJob.data?.status ?? "queued"));
  const hadConflict = preview && (preview.calendar_conflict || preview.release_conflicts.length > 0);
  const verifiedExport = exportJob.data?.status === "succeeded" ? exportJob.data.result_ref : null;

  function toggle(release: CatalogRelease) {
    setChosen((previous) => previous.some((item) => item.release_id === release.release_id)
      ? previous.filter((item) => item.release_id !== release.release_id)
      : previous.length < 32 ? [...previous, release] : previous);
  }

  return (
    <section ref={section} id="release-bundles" aria-labelledby="release-bundle-heading" className="scroll-mt-5 space-y-4 border-b pb-4">
      <h2 id="release-bundle-heading" className="text-base font-semibold">
        {tl({ zh: "冻结发布包", en: "Frozen release bundles" })}
      </h2>
      <div className="grid gap-6 xl:grid-cols-2">
        <div className="space-y-3">
          <h3 className="text-sm font-semibold">{tl({ zh: "导出发布", en: "Export releases" })}</h3>
          <Label htmlFor="bundle-export-dataset">{tl({ zh: "数据集", en: "Dataset" })}</Label>
          <select id="bundle-export-dataset" value={dataset} onChange={(event) => setDataset(event.target.value)} className="block min-h-9 w-full max-w-md rounded-md border border-input bg-background px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <option value="">{tl({ zh: "选择数据集", en: "Select a dataset" })}</option>
            {options.map((entry) => <option key={entry.key} value={entry.key}>{lang === "zh" ? entry.title : entry.title_en}</option>)}
          </select>
          {releases.isFetching && dataset && <p role="status" className="text-sm">{tl({ zh: "正在查询发布", en: "Loading releases" })}</p>}
          {releases.isError && <Button variant="outline" size="sm" onClick={() => void releases.refetch()}>{tl({ zh: "重试查询发布", en: "Retry loading releases" })}</Button>}
          {releases.data && (
            <fieldset className="max-h-52 space-y-1 overflow-auto">
              <legend className="text-sm text-muted-foreground">{tl({ zh: "选择冻结版本", en: "Choose frozen releases" })}</legend>
              {releases.data.releases.filter((release) => portableKinds.has(release.dataset_kind) && ["passed", "warnings"].includes(release.quality_status)).map((release) => (
                <label className="flex items-start gap-2 break-all text-sm" key={release.release_id}>
                  <input className="mt-1 accent-primary" type="checkbox" checked={chosen.some((item) => item.release_id === release.release_id)} onChange={() => toggle(release)} />
                  <span>{release.version} · {release.source} · {release.start_date} – {release.end_date}</span>
                </label>
              ))}
            </fieldset>
          )}
          {chosen.length > 0 && (
            <ul className="space-y-1 text-sm">
              {chosen.map((release) => (
                <li className="flex flex-wrap items-center gap-2 break-all" key={release.release_id}>
                  <span>{release.dataset_name} · {release.version}</span>
                  <Button type="button" variant="ghost" size="sm" onClick={() => toggle(release)}>
                    {tl({ zh: "移除", en: "Remove" })}
                  </Button>
                </li>
              ))}
            </ul>
          )}
          <p className="text-sm text-muted-foreground">
            {tl({ zh: "导出前请核对原数据源的使用与再分发条款。", en: "Check the source terms for use and redistribution before exporting." })}
          </p>
          <Button size="sm" onClick={() => submitExport.mutate(chosen.map((item) => item.release_id))} disabled={chosen.length === 0 || submitExport.isPending || busyExport}>
            {tl({ zh: "提交导出", en: "Export bundle" })}
          </Button>
          {submitExport.isError && <p role="alert" className="text-sm text-destructive">{tl({ zh: "导出提交失败，请重试。", en: "Export submission failed. Try again." })}</p>}
          {exportId && <BundleActivity job={exportJob.data} jobId={exportId} />}
          {verifiedExport && <Button asChild size="sm" variant="outline"><a href={`/api/data/bundles/${encodeURIComponent(verifiedExport)}/download`} download>{tl({ zh: "下载发布包", en: "Download bundle" })}</a></Button>}
        </div>
        <div className="space-y-3">
          <h3 className="text-sm font-semibold">{tl({ zh: "导入发布", en: "Import releases" })}</h3>
          <Label htmlFor="bundle-import-file">{tl({ zh: "本机 ZIP 发布包", en: "Local ZIP bundle" })}</Label>
          <Input id="bundle-import-file" type="file" accept=".zip,application/zip" disabled={inspect.isPending || busyImport} onChange={(event) => {
            setFile(event.target.files?.[0]);
            setPreview(undefined);
            setImportId(undefined);
            inspect.reset();
          }} />
          <Button size="sm" variant="outline" disabled={!file || inspect.isPending || busyImport} onClick={() => file && inspect.mutate(file)}>
            {tl({ zh: "上传并预览", en: "Upload and preview" })}
          </Button>
          {inspect.isPending && (
            <div role="status" className="space-y-1 text-sm">
              <span>{percent === 100 ? tl({ zh: "正在核对清单", en: "Checking manifests" }) : `${percent}%`}</span>
              <progress aria-label={tl({ zh: "上传进度", en: "Upload progress" })} value={percent} max={100} className="block w-full" />
            </div>
          )}
          {inspect.isError && (
            <p role="alert" className="break-all text-sm text-destructive">
              {tl({ zh: "预览失败，请检查发布包或存储容量。", en: "Preview failed. Check the bundle or storage capacity." })} {inspect.error.message}
            </p>
          )}
          {preview && (
            <div className="space-y-2">
              <p className="text-sm">{tl({ zh: "解包体积", en: "Unpacked size" })} {prettySize(preview.unpacked_bytes)}</p>
              <ul className="divide-y divide-border border-y">
                {preview.releases.map((item) => (
                  <li key={item.release_id} className="py-2 text-sm">
                    <div className="break-all font-medium">{item.release_id}</div>
                    <div className="text-muted-foreground">{item.source} · {item.adjustment} · {item.start_date} – {item.end_date} · {item.symbol_count} {tl({ zh: "标的", en: "instruments" })}</div>
                  </li>
                ))}
              </ul>
              {hadConflict && <p role="alert" className="break-words text-sm text-destructive">{tl({ zh: "发布身份或交易日历存在冲突。", en: "Release identity or trading calendar conflicts." })} {preview.release_conflicts.join(", ")}</p>}
              <Button size="sm" onClick={() => submitImport.mutate(preview)} disabled={Boolean(hadConflict) || imported || busyImport || submitImport.isPending}>
                {tl({ zh: "确认导入", en: "Confirm import" })}
              </Button>
            </div>
          )}
          {submitImport.isError && <p role="alert" className="text-sm text-destructive">{tl({ zh: "导入提交失败，请重新预览或重试。", en: "Import submission failed. Preview again or retry." })}</p>}
          {importId && <BundleActivity job={importJob.data} jobId={importId} />}
        </div>
      </div>
    </section>
  );
}
