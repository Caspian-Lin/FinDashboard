import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ClipboardList } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { TaskCenterNav } from "@/components/jobs/TaskCenterNav";
import { dayBound } from "@/lib/task-center";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PageHeader } from "@/components/ui/page-header";
import { PageContainer } from "@/components/ui/page-container";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { useT } from "@/i18n";
import { cn, formatDateTime } from "@/lib/utils";

interface AuditEvent {
  audit_id: string;
  occurred_at: string;
  actor: string;
  source: string;
  operation: string;
  outcome: string;
  trust: string;
  topic_id: string | null;
  entity_id: string | null;
  correlation: Record<string, unknown>;
  result_code: string | null;
  error_summary: string | null;
  params_summary: Record<string, unknown>;
  params_digest: string;
}

interface AuditPage {
  items: AuditEvent[];
  next_cursor: string | null;
}

interface AuditCoverage {
  server_observed_from: string | null;
  client_declared_from: string | null;
  gaps: string[];
  retention_days: number;
}

class AuditError extends Error {
  constructor(readonly status: number) {
    super(String(status));
  }
}

async function readAudit<T>(path: string, token: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(`/api/research/audit${path}`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
    signal,
  });
  if (!response.ok) throw new AuditError(response.status);
  return response.json() as Promise<T>;
}

const idFields = [
  ["topic", "topic_id", "topic"],
  ["entry", "entry_id", "entry"],
  ["job", "job_id", "tasks"],
  ["run", "run_id", "run"],
] as const;
const filterOptions = {
  source: ["web", "mcp", "worker", "system"],
  outcome: ["succeeded", "failed", "rejected", "denied"],
};

function evidenceId(event: AuditEvent, field: string, prefix: string): string | undefined {
  return [event.correlation[field], event.entity_id].find(
    (id): id is string => typeof id === "string" && id.startsWith(prefix),
  );
}

function EventRow({ event }: { event: AuditEvent }) {
  const { t } = useT();
  const job = evidenceId(event, "job_id", "BJ-");
  const run = evidenceId(event, "run_id", "RR-");
  const topic = event.topic_id ?? evidenceId(event, "topic_id", "RT-");
  return (
    <li className="space-y-2 p-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-medium">{event.operation}</span>
        <span className="rounded border border-border px-1.5 text-xs">{t(`taskCenter.${event.trust}`)}</span>
        <span className={event.outcome === "succeeded" ? "text-muted-foreground" : "text-destructive"}>
          {t(`taskCenter.${event.outcome}`)}{event.result_code && ` · ${event.result_code}`}
        </span>
        <span className="text-sm text-muted-foreground">{formatDateTime(event.occurred_at)}</span>
      </div>
      <div className="break-words text-sm text-muted-foreground">
        {t(`taskCenter.${event.source}`)} · {event.actor} · {event.audit_id}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-sm">
        {topic && <Link className="text-primary underline" to={`/research/topics?topic=${encodeURIComponent(topic)}`}>{topic}</Link>}
        {job && <Link className="text-primary underline" to={`/jobs?job=${encodeURIComponent(job)}`}>{job}</Link>}
        {run && <Link className="text-primary underline" to={`/research/runs?run=${encodeURIComponent(run)}`}>{run}</Link>}
      </div>
      {event.error_summary && <p className="whitespace-pre-wrap break-words text-sm">{event.error_summary}</p>}
      <details className="text-sm">
        <summary className="cursor-pointer">{t("taskCenter.receipt")}</summary>
        <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded border border-border bg-background p-2 text-xs">
          {JSON.stringify({
            correlation: event.correlation,
            params_summary: event.params_summary,
            params_digest: event.params_digest,
          }, null, 2)}
        </pre>
      </details>
    </li>
  );
}

export default function ResearchAudit() {
  const { t } = useT();
  const [searchParams, setSearchParams] = useSearchParams();
  const [draft, setDraft] = React.useState("");
  const [showFilters, setShowFilters] = React.useState(false);
  const [credential, setCredential] = React.useState<{ token: string; session: number }>({ token: "", session: 0 });
  const [paging, setPaging] = React.useState<{ filter: string; stack: (string | null)[] }>({ filter: "", stack: [null] });
  const filter = searchParams.toString();
  const stack = paging.filter === filter ? paging.stack : [null];
  const cursor = stack.at(-1);
  const queryParams = new URLSearchParams({ limit: "25" });
  for (const [key, name] of idFields) {
    const value = searchParams.get(key)?.trim();
    if (value) queryParams.set(name, value);
  }
  for (const key of Object.keys(filterOptions)) {
    const value = searchParams.get(key);
    if (value) queryParams.set(key, value);
  }
  const from = dayBound(searchParams.get("from") ?? "");
  const to = dayBound(searchParams.get("through") ?? "", true);
  if (from) queryParams.set("from", from);
  if (to) queryParams.set("before", to);
  if (cursor) queryParams.set("cursor", cursor);
  const path = queryParams.toString();
  // 凭据仅用于请求头，查询键只保留组件内生成的会话序号。
  const scope = React.useId();
  const { token, session } = credential;
  const coverage = useQuery({
    queryKey: ["research-audit", scope, session, "coverage"],
    queryFn: ({ signal }) => readAudit<AuditCoverage>("/coverage", token, signal),
    enabled: !!token,
    retry: false,
    gcTime: 0,
  });
  const events = useQuery({
    queryKey: ["research-audit", scope, session, path],
    queryFn: ({ signal }) => readAudit<AuditPage>(`?${path}`, token, signal),
    enabled: !!token && coverage.isSuccess,
    retry: false,
    gcTime: 0,
  });
  const setFilter = (key: string, value: string) => {
    const params = new URLSearchParams(searchParams);
    if (value) params.set(key, value);
    else params.delete(key);
    setPaging({ filter: params.toString(), stack: [null] });
    setSearchParams(params, { replace: true });
  };
  const loadError = coverage.error ?? events.error;
  const denied = [coverage.error, events.error].some(
    (error) => error instanceof AuditError && [401, 403].includes(error.status),
  );
  const authorized = coverage.isSuccess && !denied;
  const [accessDenied, setAccessDenied] = React.useState(false);
  React.useEffect(() => {
    if (!denied) return;
    setAccessDenied(true);
    setCredential((current) => ({ token: "", session: current.session + 1 }));
    setDraft("");
  }, [denied]);
  const activeRefs = idFields.map(([key]) => searchParams.get(key)).filter(Boolean);

  return (
    <PageContainer>
      <PageHeader title={t("taskCenter.audit")} description={t("taskCenter.auditDescription")} />
      <TaskCenterNav />
      <div className="flex flex-wrap items-end gap-3">
        {!authorized && (
          <form
            className="flex flex-wrap items-end gap-3"
            onSubmit={(event) => {
              event.preventDefault();
              if (!draft.trim()) return;
              setCredential({ token: draft.trim(), session: session + 1 });
              setAccessDenied(false);
              setDraft("");
              setPaging({ filter, stack: [null] });
            }}
          >
            <div className="space-y-1.5">
              <Label htmlFor="audit-token">{t("taskCenter.credential")}</Label>
              <Input id="audit-token" type="password" autoComplete="off" className="h-11 sm:h-9" value={draft} onChange={(event) => setDraft(event.target.value)} />
            </div>
            <Button type="submit" className="h-11 sm:h-9" disabled={!draft.trim()}>{t("taskCenter.openAudit")}</Button>
          </form>
        )}
        {token && <Button type="button" className="h-11 sm:h-9" variant="outline" onClick={() => setCredential({ token: "", session: session + 1 })}>{t("taskCenter.lockAudit")}</Button>}
        <Button type="button" variant="outline" className="h-11 sm:hidden" aria-controls="audit-filters" aria-expanded={showFilters} onClick={() => setShowFilters(!showFilters)}>
          {t("taskCenter.filters")}
        </Button>
        {!showFilters && activeRefs.length > 0 && <p className="w-full break-words font-mono text-xs text-muted-foreground sm:hidden">{activeRefs.join(" · ")}</p>}
      </div>

      <div id="audit-filters" className={cn("flex-wrap items-end gap-3 sm:flex", showFilters ? "flex" : "hidden")}>
        {idFields.map(([key, , label]) => (
          <div className="space-y-1.5" key={key}>
            <Label htmlFor={`audit-${key}`}>{t(`taskCenter.${label}`)}</Label>
            <Input id={`audit-${key}`} className="h-11 w-40 sm:h-9" value={searchParams.get(key) ?? ""} onChange={(event) => setFilter(key, event.target.value)} />
          </div>
        ))}
        {Object.entries(filterOptions).map(([key, options]) => (
          <div className="space-y-1.5" key={key}>
            <Label htmlFor={`audit-${key}`}>{t(`taskCenter.${key}`)}</Label>
            <select id={`audit-${key}`} className="flex h-11 rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:h-9" value={searchParams.get(key) ?? ""} onChange={(event) => setFilter(key, event.target.value)}>
              <option value="">{t("taskCenter.all")}</option>
              {options.map((option) => <option value={option} key={option}>{t(`taskCenter.${option}`)}</option>)}
            </select>
          </div>
        ))}
        {["from", "through"].map((key) => (
          <div className="space-y-1.5" key={key}>
            <Label htmlFor={`audit-${key}`}>{t(`taskCenter.${key}`)}</Label>
            <Input id={`audit-${key}`} type="date" className="h-11 sm:h-9" value={searchParams.get(key) ?? ""} onChange={(event) => setFilter(key, event.target.value)} />
          </div>
        ))}
      </div>
      {authorized && coverage.data && (
        <section className="space-y-2 rounded-lg border border-border bg-card p-4 text-sm">
          <h2 className="font-semibold">{t("taskCenter.coverage")}</h2>
          <dl className="flex flex-wrap gap-x-6 gap-y-1">
            <div><dt className="text-muted-foreground">{t("taskCenter.server_observed")}</dt><dd>{coverage.data.server_observed_from ? formatDateTime(coverage.data.server_observed_from) : t("taskCenter.noRecords")}</dd></div>
            <div><dt className="text-muted-foreground">{t("taskCenter.client_declared")}</dt><dd>{coverage.data.client_declared_from ? formatDateTime(coverage.data.client_declared_from) : t("taskCenter.noRecords")}</dd></div>
          </dl>
          <p className="text-muted-foreground">{t("taskCenter.retention", { days: coverage.data.retention_days })}</p>
          <ul className="list-disc ps-4 text-muted-foreground">
            {coverage.data.gaps.map((gap) => <li key={gap}>{t(`taskCenter.${gap}`)}</li>)}
          </ul>
        </section>
      )}
      {(denied || accessDenied) && <Alert variant="destructive"><AlertDescription>{t("taskCenter.deniedCredential")}</AlertDescription></Alert>}
      {loadError && !denied && <ErrorState message={t("taskCenter.auditError")} onRetry={() => coverage.isError ? coverage.refetch() : events.refetch()} />}
      {(coverage.isLoading || events.isLoading) && <LoadingState rows={4} />}
      {authorized && events.data && events.data.items.length === 0 &&
        <EmptyState icon={<ClipboardList className="h-8 w-8" />} title={t("taskCenter.noRecords")} description={t("taskCenter.emptyAudit")} />
      }
      {authorized && events.data && events.data.items.length > 0 && (
        <>
          <ul className="divide-y divide-border rounded-lg border border-border bg-card">
            {events.data.items.map((event) => <EventRow key={event.audit_id} event={event} />)}
          </ul>
          <nav className="flex justify-end gap-2" aria-label={t("jobs.paginationAria")}>
            <Button variant="outline" disabled={stack.length <= 1} onClick={() => setPaging({ filter, stack: stack.slice(0, -1) })}>{t("common.prevPage")}</Button>
            <Button variant="outline" disabled={!events.data.next_cursor} onClick={() => setPaging({ filter, stack: [...stack, events.data?.next_cursor ?? null] })}>{t("common.nextPage")}</Button>
          </nav>
        </>
      )}
    </PageContainer>
  );
}
