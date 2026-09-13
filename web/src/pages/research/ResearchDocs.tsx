import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type * as React from "react";
import { BookOpen, Map, ScrollText, Table2, FileText } from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import {
  MasterList,
  MasterListItem,
} from "@/components/ui/master-list";
import { Badge } from "@/components/ui/badge";
import {
  EmptyState,
  ErrorState,
  LoadingState,
} from "@/components/ui/states";
import { researchDocsApi, type ResearchDocKind, type ResearchDocSummary } from "@/lib/research";
import { useT } from "@/i18n";
import { cn, timeAgo } from "@/lib/utils";

const KIND_BADGE: Record<ResearchDocKind, { zh: string; en: string; className: string }> = {
  overview: { zh: "治理", en: "Governance", className: "bg-primary/10 text-primary" },
  roadmap: { zh: "路线", en: "Roadmap", className: "bg-primary/10 text-primary" },
  findings: { zh: "结论", en: "Findings", className: "bg-success/10 text-success" },
  round: { zh: "轮次", en: "Round", className: "bg-info/10 text-info" },
  other: { zh: "文档", en: "Doc", className: "bg-muted text-muted-foreground" },
};

function docIcon(kind: ResearchDocKind) {
  if (kind === "roadmap") return <Map className="h-3.5 w-3.5 shrink-0" />;
  if (kind === "findings") return <Table2 className="h-3.5 w-3.5 shrink-0" />;
  if (kind === "round") return <ScrollText className="h-3.5 w-3.5 shrink-0" />;
  return <FileText className="h-3.5 w-3.5 shrink-0" />;
}

function formatSize(bytes: number): string {
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

/** 手写元素样式(仓库未装 @tailwindcss/typography):覆盖 GFM 表格与标题等核心形态。 */
const markdownComponents: Components = {
  h1: (props: React.ComponentPropsWithoutRef<"h1">) => (
    <h1 className="mb-4 mt-2 border-b border-border pb-2 text-xl font-bold text-foreground" {...props} />
  ),
  h2: (props: React.ComponentPropsWithoutRef<"h2">) => (
    <h2 className="mb-3 mt-6 text-lg font-semibold text-foreground" {...props} />
  ),
  h3: (props: React.ComponentPropsWithoutRef<"h3">) => (
    <h3 className="mb-2 mt-4 text-base font-semibold text-foreground" {...props} />
  ),
  p: (props: React.ComponentPropsWithoutRef<"p">) => (
    <p className="my-2 text-sm leading-6 text-foreground/90" {...props} />
  ),
  ul: (props: React.ComponentPropsWithoutRef<"ul">) => (
    <ul className="my-2 list-disc space-y-1 pl-5 text-sm text-foreground/90" {...props} />
  ),
  ol: (props: React.ComponentPropsWithoutRef<"ol">) => (
    <ol className="my-2 list-decimal space-y-1 pl-5 text-sm text-foreground/90" {...props} />
  ),
  blockquote: (props: React.ComponentPropsWithoutRef<"blockquote">) => (
    <blockquote className="my-3 border-l-2 border-border pl-3 text-sm text-muted-foreground" {...props} />
  ),
  a: (props: React.ComponentPropsWithoutRef<"a">) => (
    <a className="text-primary underline underline-offset-2" target="_blank" rel="noreferrer" {...props} />
  ),
  code: (props: React.ComponentPropsWithoutRef<"code">) => (
    <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs" {...props} />
  ),
  pre: (props: React.ComponentPropsWithoutRef<"pre">) => (
    <pre
      className="my-3 overflow-x-auto scrollbar-thin rounded-md border border-border bg-muted/50 p-3 font-mono text-xs"
      {...props}
    />
  ),
  hr: () => <hr className="my-4 border-border" />,
  table: (props: React.ComponentPropsWithoutRef<"table">) => (
    <div className="my-3 overflow-x-auto scrollbar-thin rounded-md border border-border">
      <table className="w-full text-xs" {...props} />
    </div>
  ),
  th: (props: React.ComponentPropsWithoutRef<"th">) => (
    <th className="border-b border-border bg-background px-2.5 py-1.5 text-left font-semibold text-foreground" {...props} />
  ),
  td: (props: React.ComponentPropsWithoutRef<"td">) => (
    <td className="border-b border-border/60 px-2.5 py-1.5 align-top text-foreground/90" {...props} />
  ),
};

function DocListItem({
  doc,
  selected,
  onClick,
}: {
  doc: ResearchDocSummary;
  selected: boolean;
  onClick: () => void;
}) {
  const { tl, lang } = useT();
  const badge = KIND_BADGE[doc.kind];
  return (
    <MasterListItem selected={selected} onClick={onClick}>
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-1.5 text-sm font-medium text-foreground">
          {docIcon(doc.kind)}
          <span className="min-w-0 truncate">{doc.title}</span>
        </span>
        <Badge variant="outline" className={cn("shrink-0 text-[10px]", badge.className)}>
          {tl(badge)}
        </Badge>
      </div>
      <div className="mt-1.5 flex items-center justify-between gap-2 text-xs text-muted-foreground">
        <span className="min-w-0 truncate font-mono">{doc.path}</span>
        <span className="shrink-0">
          {formatSize(doc.size_bytes)} · {timeAgo(doc.updated_at, lang)}
        </span>
      </div>
    </MasterListItem>
  );
}

export default function ResearchDocs() {
  const { tl } = useT();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeDoc = searchParams.get("doc");

  const listQuery = useQuery({
    queryKey: ["research-docs"],
    queryFn: researchDocsApi.list,
  });
  const docs = listQuery.data?.docs ?? [];

  const selectedPath = useMemo(() => {
    if (activeDoc && docs.some((d) => d.path === activeDoc)) return activeDoc;
    return docs[0]?.path ?? null;
  }, [activeDoc, docs]);

  const detailQuery = useQuery({
    queryKey: ["research-docs", selectedPath],
    queryFn: () => researchDocsApi.get(selectedPath as string),
    enabled: selectedPath !== null,
  });

  const selectDoc = (path: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("doc", path);
    setSearchParams(next, { replace: true });
  };

  const canonical = docs.filter((d) => d.kind !== "round");
  const rounds = docs.filter((d) => d.kind === "round");

  return (
    <div>
      <PageHeader
        title={tl({ zh: "研究记录", en: "Research Notes" })}
        description={tl({
          zh: "外置研究 agent 维护的仓库文档(三件套 + 轮次日志),只读展示;canonical 仍是仓库文件,经 PR 维护。",
          en: "Repository docs maintained by the external research agent (three-piece set + round logs), read-only; canonical source stays in the repo via PRs.",
        })}
        actions={
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <BookOpen className="h-4 w-4" />
            {docs.length > 0 &&
              tl({ zh: `${docs.length} 份文档`, en: `${docs.length} docs` })}
          </div>
        }
      />

      <div className="flex flex-col gap-4 lg:flex-row">
        <div className="w-full shrink-0 lg:w-80 lg:self-start lg:sticky lg:top-16">
          <MasterList title={tl({ zh: "文档列表", en: "Documents" })} count={docs.length}>
            {listQuery.isLoading ? (
              <LoadingState rows={4} />
            ) : listQuery.isError ? (
              <ErrorState
                message={tl({ zh: "无法加载研究记录", en: "Failed to load research notes" })}
                onRetry={() => listQuery.refetch()}
              />
            ) : docs.length === 0 ? (
              <EmptyState
                icon={<BookOpen className="h-8 w-8" />}
                title={tl({ zh: "暂无研究记录", en: "No research notes" })}
                description={tl({
                  zh: "仓库 docs/research/ 目录不存在或为空;由研究 agent 按 issue #268 协议写入。",
                  en: "The repo docs/research/ directory is missing or empty; written by the research agent per issue #268.",
                })}
              />
            ) : (
              <>
                {canonical.map((doc) => (
                  <DocListItem
                    key={doc.path}
                    doc={doc}
                    selected={doc.path === selectedPath}
                    onClick={() => selectDoc(doc.path)}
                  />
                ))}
                {rounds.length > 0 && (
                  <p className="px-1 pb-1 pt-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    {tl({ zh: "轮次记录", en: "Round logs" })}
                  </p>
                )}
                {rounds.map((doc) => (
                  <DocListItem
                    key={doc.path}
                    doc={doc}
                    selected={doc.path === selectedPath}
                    onClick={() => selectDoc(doc.path)}
                  />
                ))}
              </>
            )}
          </MasterList>
        </div>

        <div className="min-w-0 flex-1">
          {selectedPath === null ? (
            <EmptyState
              icon={<BookOpen className="h-8 w-8" />}
              title={tl({ zh: "选择一份文档", en: "Select a document" })}
            />
          ) : detailQuery.isLoading ? (
            <LoadingState rows={8} />
          ) : detailQuery.isError || !detailQuery.data ? (
            <ErrorState
              message={tl({ zh: "无法读取文档", en: "Failed to read document" })}
              onRetry={() => detailQuery.refetch()}
            />
          ) : (
            <div className="rounded-lg border border-border bg-card">
              <div className="flex items-center justify-between gap-2 border-b border-border px-5 py-3">
                <h2 className="min-w-0 truncate text-base font-semibold text-foreground">
                  {detailQuery.data.title}
                </h2>
                <span className="shrink-0 font-mono text-xs text-muted-foreground">
                  {detailQuery.data.path}
                </span>
              </div>
              <div className="px-5 py-4">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={markdownComponents}
                >
                  {detailQuery.data.content}
                </ReactMarkdown>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
