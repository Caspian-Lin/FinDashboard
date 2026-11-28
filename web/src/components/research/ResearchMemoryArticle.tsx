import type { ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { useT, type LocalizedText } from "@/i18n";
import { Badge } from "@/components/ui/badge";
import { ResearchTimestamp } from "./ResearchTimestamp";
import type { Memory, MemoryDetail } from "@/lib/research-workspace";

const TYPES: Record<string, LocalizedText> = {
  insight: { zh: "研究洞见", en: "Research insight" },
  hypothesis: { zh: "研究假设", en: "Research hypothesis" },
  decision: { zh: "研究决策", en: "Research decision" },
  note: { zh: "研究笔记", en: "Research note" },
  lesson: { zh: "经验记录", en: "Lesson learned" },
  context: { zh: "研究上下文", en: "Research context" },
};
const STATUSES: Record<string, LocalizedText> = {
  active: { zh: "未归档", en: "Active" },
  archived: { zh: "已归档", en: "Archived" },
  forgotten: { zh: "已纠正/移除", en: "Corrected or removed" },
};

function DeferredMemoryImage({ alt }: { alt?: string }) {
  const { tl } = useT();
  return <span className="text-muted-foreground">{tl({
    zh: `[图片：${alt || "未提供说明"}，未自动加载]`,
    en: `[Image: ${alt || "no description"}. Loading disabled.]`,
  })}</span>;
}

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
  img: DeferredMemoryImage,
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
  const { tl } = useT();
  const prefix = /^【([^】\n]{1,180})】\s*/.exec(content);
  const title = prefix?.[1] ?? tl(TYPES[memory.memory_type] ?? { zh: "研究记忆", en: "Research memory" });
  const body = prefix ? content.slice(prefix[0].length) : content;
  return (
    <article
      aria-label={`${detail ? tl({ zh: "完整记忆", en: "Full memory" }) : tl({ zh: "记忆摘录", en: "Memory excerpt" })} ${memory.memory_id}`}
      className="grid min-w-0 gap-5 border-t border-border py-6 md:grid-cols-[13rem_minmax(0,1fr)] md:gap-8"
    >
      <aside className="min-w-0 space-y-3 text-xs text-muted-foreground">
        <Badge variant="outline">{STATUSES[memory.status] ? tl(STATUSES[memory.status]) : memory.status}</Badge>
        <p className="break-all font-mono">{memory.memory_id}</p>
        <div>
          <p className="mb-1">{tl({ zh: "创建时间", en: "Created" })}</p>
          <ResearchTimestamp value={memory.created_at} />
        </div>
        {detail && memory.updated_at && memory.updated_at !== memory.created_at && (
          <div>
            <p className="mb-1">{tl({ zh: "更新时间", en: "Updated" })}</p>
            <ResearchTimestamp value={memory.updated_at} />
          </div>
        )}
        <p className="break-all">{tl({ zh: `来源：${memory.created_by}`, en: `Source: ${memory.created_by}` })}</p>
        <p>
          {memory.confirmed_by
            ? tl({ zh: `已由 ${memory.confirmed_by} 确认，仍不替代文档`, en: `Confirmed by ${memory.confirmed_by}. Refer to the research documents for the canonical conclusion.` })
            : tl({ zh: "未确认解释", en: "Unconfirmed interpretation" })}
        </p>
        {detail && memory.confirmed_at && (
          <div>
            <p className="mb-1">{tl({ zh: "确认时间", en: "Confirmed" })}</p>
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
              {tl({ zh: "正文为前 1200 字摘录，完整记忆保留原文。", en: "This excerpt shows the first 1,200 characters. Open the full memory for the original text." })}
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
