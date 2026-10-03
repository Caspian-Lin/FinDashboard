import type { ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Badge } from "@/components/ui/badge";
import { ResearchTimestamp } from "./ResearchTimestamp";
import type { Memory, MemoryDetail } from "@/lib/research-workspace";

const TYPES: Record<string, string> = {
  insight: "研究洞见",
  hypothesis: "研究假设",
  decision: "研究决策",
  note: "研究笔记",
  lesson: "经验记录",
  context: "研究上下文",
};
const STATUSES: Record<string, string> = {
  active: "未归档",
  archived: "已归档",
  forgotten: "已纠正/移除",
};

const markdown: Components = {
  h1: ({ children }) => <h3 className="mb-3 mt-5 text-lg font-semibold">{children}</h3>,
  h2: ({ children }) => <h3 className="mb-2 mt-5 text-base font-semibold">{children}</h3>,
  h3: ({ children }) => <h4 className="mb-2 mt-4 font-semibold">{children}</h4>,
  p: ({ children }) => <p className="my-3 whitespace-pre-wrap leading-7">{children}</p>,
  ul: ({ children }) => <ul className="my-3 list-disc space-y-2 pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="my-3 list-decimal space-y-2 pl-5">{children}</ol>,
  li: ({ children }) => <li className="leading-7">{children}</li>,
  blockquote: ({ children }) => (
    <blockquote className="my-4 rounded-md bg-muted/50 px-4 py-1 text-muted-foreground">
      {children}
    </blockquote>
  ),
  a: ({ href, children }) =>
    href ? (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="text-primary underline underline-offset-4"
      >
        {children}
      </a>
    ) : (
      <span>{children}</span>
    ),
  code: ({ children }) => (
    <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">{children}</code>
  ),
  pre: ({ children }) => (
    <pre className="my-4 max-w-full overflow-x-auto rounded-md bg-muted/50 p-4 leading-6 [&_code]:bg-transparent [&_code]:p-0">
      {children}
    </pre>
  ),
  table: ({ children }) => (
    <div className="my-4 max-w-full overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border-b border-border px-3 py-2 font-semibold">{children}</th>
  ),
  td: ({ children }) => <td className="border-b border-border px-3 py-2 align-top">{children}</td>,
  img: ({ alt }) => (
    <span className="text-muted-foreground">[图片：{alt || "未提供说明"}，未自动加载]</span>
  ),
};

type RecordMeta = Pick<
  Memory,
  | "memory_id"
  | "memory_type"
  | "status"
  | "created_by"
  | "created_at"
  | "confirmed_by"
  | "supersedes_id"
>;

export function ResearchMemoryArticle({
  memory,
  content,
  excerpted = false,
  evidence,
  actions,
  detail = false,
}: {
  memory: RecordMeta & Partial<Pick<MemoryDetail, "updated_at" | "confirmed_at" | "tags">>;
  content: string;
  excerpted?: boolean;
  evidence: ReactNode;
  actions?: ReactNode;
  detail?: boolean;
}) {
  const prefix = /^【([^】\n]{1,180})】\s*/.exec(content);
  const title = prefix?.[1] ?? TYPES[memory.memory_type] ?? "研究记忆";
  const body = prefix ? content.slice(prefix[0].length) : content;
  return (
    <article
      aria-label={`${detail ? "完整记忆" : "记忆摘录"} ${memory.memory_id}`}
      className="grid min-w-0 gap-5 border-t border-border py-6 md:grid-cols-[13rem_minmax(0,1fr)] md:gap-8"
    >
      <aside className="min-w-0 space-y-3 text-xs text-muted-foreground">
        <Badge variant="outline">{STATUSES[memory.status] ?? memory.status}</Badge>
        <p className="break-all font-mono">{memory.memory_id}</p>
        <div>
          <p className="mb-1">创建时间</p>
          <ResearchTimestamp value={memory.created_at} />
        </div>
        {detail && memory.updated_at && memory.updated_at !== memory.created_at && (
          <div>
            <p className="mb-1">更新时间</p>
            <ResearchTimestamp value={memory.updated_at} />
          </div>
        )}
        <p className="break-all">来源：{memory.created_by}</p>
        <p>
          {memory.confirmed_by ? `已由 ${memory.confirmed_by} 确认，仍不替代文档` : "未确认解释"}
        </p>
        {detail && memory.confirmed_at && (
          <div>
            <p className="mb-1">确认时间</p>
            <ResearchTimestamp value={memory.confirmed_at} />
          </div>
        )}
        {!!memory.tags?.length && (
          <div className="flex flex-wrap gap-2">
            {memory.tags.map((tag) => (
              <Badge className="max-w-full break-all whitespace-normal" variant="outline" key={tag}>
                {tag}
              </Badge>
            ))}
          </div>
        )}
      </aside>
      <div className="min-w-0 space-y-5">
        <div className="max-w-[75ch] text-sm [overflow-wrap:anywhere]">
          <h2 className="mb-3 text-base font-semibold leading-7">{title}</h2>
          <ReactMarkdown skipHtml remarkPlugins={[remarkGfm]} components={markdown}>
            {body}
          </ReactMarkdown>
          {excerpted && (
            <p className="mt-3 text-xs text-muted-foreground">
              正文为前 1200 字摘录，完整记忆保留原文。
            </p>
          )}
        </div>
        <div className="space-y-3">
          {evidence}
          {actions}
        </div>
      </div>
    </article>
  );
}
