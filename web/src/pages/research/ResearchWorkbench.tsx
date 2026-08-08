import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  MessageSquare,
  Plus,
  RefreshCw,
  Power,
  Square,
  StopCircle,
  ChevronDown,
  ShieldCheck,
  AlertTriangle,
  Terminal,
  History,
} from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusBadge, StatusDot } from "@/components/ui/status-badge";
import { LoadingState, EmptyState, ErrorState } from "@/components/ui/states";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { cn, timeAgo } from "@/lib/utils";
import {
  conversationApi,
  opencodeGatewayApi,
  type ConversationOut,
  type ConversationStatus,
  type AgentEventOut,
} from "@/lib/opencode";
import { buildWorkbenchUrl, redactedWorkbenchUrl } from "@/lib/opencode-url";

/* ============================================================ */
/* OpenCode 研究工作台(issue #111)                              */
/* ============================================================ */
//
// 产品分工:
//   Part 1(左)FinBoard 控制面 —— 会话列表、状态、关键事件摘要、审批产物关联;
//   Part 2(右)OpenCode Web 交互面 —— iframe 跨源嵌入,默认研究交互入口。
//
// 边界:
//   - 只能打开 FinBoard 授权绑定的 ACTIVE conversation_id(后端 /access 校验);
//   - 不展示未脱敏原始思考内容(token 级 delta 不落库,只展示关键事件摘要);
//   - 写操作仍经 FinBoard MCP 草案 + 人工审批,前端不绕过;
//   - opencode_web_enabled=false(网关 503)时降级到 /research/ai。

/** 工作台关键事件类型分组(对齐后端 KEY_EVENT_TYPES)。 */
const EVENT_LABELS: Record<string, string> = {
  message: "消息",
  "message.updated": "消息更新",
  "message.completed": "消息完成",
  "message.removed": "消息移除",
  tool: "工具调用",
  "tool.call": "工具调用",
  "tool.result": "工具结果",
  "tool.output": "工具输出",
  error: "错误",
  "session.state": "会话状态",
  "session.updated": "会话更新",
  "agent.switched": "Agent 切换",
  abort: "中止",
  interrupt: "中断",
};

/** 从事件 payload 提取一行摘要文本(脱敏,不展示完整原始思考)。 */
function summarizeEvent(event: AgentEventOut): string {
  const p = event.payload ?? {};
  // 常见字段优先
  if (typeof p.text === "string" && p.text.trim()) return truncate(p.text, 200);
  if (typeof p.content === "string" && p.content.trim()) return truncate(p.content, 200);
  if (typeof p.message === "string" && p.message.trim()) return truncate(p.message, 200);
  if (typeof p.error === "string" && p.error.trim()) return truncate(p.error, 200);
  if (typeof p.name === "string" && p.name.trim()) return `工具: ${p.name}`;
  // 兜底:展示类型 + seq,不展开 payload(可能含未脱敏内容)
  return `(事件 #${event.seq},${event.type})`;
}

function truncate(text: string, max: number): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

/** 会话是否可打开工作台(后端 /access 只对 ACTIVE 签发)。 */
function canOpenWorkbench(status: ConversationStatus): boolean {
  return status === "active";
}

export default function ResearchWorkbench() {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [workbenchNonce, setWorkbenchNonce] = useState(0);

  /* ---------- 探测 OpenCode Web 网关是否启用 ---------- */
  const {
    data: status,
    isError: statusError,
    isLoading: statusLoading,
  } = useQuery({
    queryKey: ["opencode-status"],
    queryFn: () => opencodeGatewayApi.status(),
    retry: false, // 503 是合法的"未启用"状态,不重试
    refetchInterval: 15_000,
  });

  const gatewayEnabled = !statusError && status != null;

  /* ---------- 会话列表 ---------- */
  const {
    data: conversations,
    isLoading: convLoading,
    isError: convError,
    refetch: refetchConversations,
  } = useQuery({
    queryKey: ["opencode-conversations"],
    queryFn: () => conversationApi.list(50),
    enabled: gatewayEnabled,
    refetchInterval: 10_000,
  });

  const selected = conversations?.find((c) => c.conversation_id === selectedId) ?? null;

  /* ---------- 签发工作台访问凭证(仅选中 ACTIVE 会话) ---------- */
  const {
    data: access,
    isError: accessError,
    error: accessErr,
  } = useQuery({
    queryKey: ["opencode-access", selectedId],
    queryFn: () => opencodeGatewayApi.access(selectedId!),
    enabled: gatewayEnabled && selectedId != null && selected != null && canOpenWorkbench(selected.status),
    retry: false,
  });

  /* ---------- 关键事件历史(选中会话) ---------- */
  const { data: events } = useQuery({
    queryKey: ["opencode-events", selectedId],
    queryFn: () => conversationApi.history(selectedId!, 0),
    enabled: gatewayEnabled && selectedId != null,
    refetchInterval: 8_000,
  });

  /* ---------- 创建会话 ---------- */
  const createMutation = useMutation({
    mutationFn: () =>
      conversationApi.create({
        title: `研究会话 ${new Date().toLocaleString("zh-CN")}`,
      }),
    onSuccess: (record) => {
      queryClient.invalidateQueries({ queryKey: ["opencode-conversations"] });
      setSelectedId(record.conversation_id);
    },
  });

  /* ---------- 中断 / 中止 ---------- */
  const interruptMutation = useMutation({
    mutationFn: (id: string) => conversationApi.interrupt(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["opencode-conversations"] }),
  });
  const abortMutation = useMutation({
    mutationFn: (id: string) => conversationApi.abort(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["opencode-conversations"] }),
  });

  /* ---------- 降级态:网关未启用 ---------- */
  if (!gatewayEnabled && !statusLoading) {
    return (
      <div>
        <PageHeader
          title="研究工作台"
          description="OpenCode Web 研究交互工作台(FinBoard 控制面 + OpenCode Web 交互面)"
        />
        <Alert variant="warning">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>OpenCode Web 工作台未启用</AlertTitle>
          <AlertDescription>
            网关返回 503,OpenCode Web 工作台入口已隐藏。可使用现有 AI 助手作为兼容/降级入口。
            <div className="mt-3">
              <Button asChild variant="outline" size="sm">
                <Link to="/research/ai">
                  <MessageSquare className="h-4 w-4" />
                  前往 AI 助手(降级)
                </Link>
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  const workbenchUrl =
    access && selectedId ? buildWorkbenchUrl(access, selectedId) : null;

  return (
    <div>
      <PageHeader
        title="研究工作台"
        description="OpenCode Web 研究交互工作台(FinBoard 控制面 + OpenCode Web 交互面)"
      />

      {/* 边界提示 */}
      <Alert className="mb-4">
        <ShieldCheck className="h-4 w-4" />
        <AlertTitle>研究边界</AlertTitle>
        <AlertDescription>
          OpenCode Web 是研究交互层,FinBoard API/MCP 是事实来源与权限/审批边界。
          写操作(创建 ResearchRun/回测/模拟盘)仍经 MCP 草案 + 人工审批,前端不绕过。
          工作台不连接实盘 broker/账户/订单/持仓。
        </AlertDescription>
      </Alert>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[360px_1fr]">
        {/* ============ Part 1:FinBoard 控制面 ============ */}
        <div className="space-y-3">
          {/* 网关状态 */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center justify-between text-sm">
                <span className="flex items-center gap-2">
                  <StatusDot status={status?.healthy ? "online" : status?.running ? "warning" : "offline"} />
                  OpenCode Web
                </span>
                {status?.version && (
                  <span className="font-mono text-xs text-muted-foreground">v{status.version}</span>
                )}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-1.5 text-xs text-muted-foreground">
              <div className="flex justify-between">
                <span>实例状态</span>
                <span>{status?.running ? "运行中" : "未运行"}</span>
              </div>
              <div className="flex justify-between">
                <span>健康探测</span>
                <span>{status?.healthy == null ? "未知" : status.healthy ? "健康" : "异常"}</span>
              </div>
              {status?.managed && status.container_id != null && (
                <div className="flex justify-between">
                  <span>容器 ID</span>
                  <span className="font-mono">{status.container_id}</span>
                </div>
              )}
              {status?.started_at && (
                <div className="flex justify-between">
                  <span>启动时间</span>
                  <span>{timeAgo(status.started_at)}</span>
                </div>
              )}
            </CardContent>
          </Card>

          {/* 新建会话 */}
          <Button
            className="w-full"
            onClick={() => createMutation.mutate()}
            disabled={createMutation.isPending}
          >
            <Plus className="h-4 w-4" />
            {createMutation.isPending ? "创建中…" : "新建研究会话"}
          </Button>
          <p className="text-xs text-muted-foreground">
            创建研究会话不会启动 ResearchRun、回测或模拟盘。
          </p>
          {createMutation.isError && (
            <p className="text-xs text-destructive">
              创建失败:{String(createMutation.error?.message ?? "未知错误")}
            </p>
          )}

          {/* 会话列表 */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center justify-between text-sm">
                <span>研究会话</span>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6"
                  onClick={() => refetchConversations()}
                  title="刷新会话列表"
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                </Button>
              </CardTitle>
            </CardHeader>
            <CardContent>
              {convLoading ? (
                <LoadingState rows={3} />
              ) : convError ? (
                <ErrorState
                  message="会话列表加载失败"
                  onRetry={() => refetchConversations()}
                />
              ) : conversations && conversations.length > 0 ? (
                <div className="max-h-[480px] space-y-1.5 overflow-y-auto pr-1">
                  {conversations.map((conv) => (
                    <ConversationListItem
                      key={conv.conversation_id}
                      conv={conv}
                      selected={conv.conversation_id === selectedId}
                      onSelect={() => setSelectedId(conv.conversation_id)}
                    />
                  ))}
                </div>
              ) : (
                <EmptyState
                  icon={<MessageSquare className="h-6 w-6" />}
                  title="暂无研究会话"
                  description="点击「新建研究会话」开始"
                />
              )}
            </CardContent>
          </Card>
        </div>

        {/* ============ Part 2:OpenCode Web 交互面 ============ */}
        <div className="space-y-3">
          {!selected ? (
            <Card className="flex min-h-[520px] items-center justify-center">
              <EmptyState
                icon={<Terminal className="h-6 w-6" />}
                title="未选择研究会话"
                description="从左侧选择或新建一个研究会话,打开 OpenCode Web 工作台"
              />
            </Card>
          ) : !canOpenWorkbench(selected.status) ? (
            <Card className="flex min-h-[520px] items-center justify-center">
              <EmptyState
                icon={<Power className="h-6 w-6" />}
                title={`会话状态:${selected.status}`}
                description="仅 ACTIVE 会话可打开 OpenCode Web 工作台。可新建会话或等待恢复。"
              />
            </Card>
          ) : accessError ? (
            <Card className="flex min-h-[520px] items-center justify-center">
              <ErrorState
                title="工作台访问未授权"
                message={
                  accessErr?.message?.includes("403")
                    ? "会话未授权(不存在 / 非 ACTIVE)。请重新选择或新建会话。"
                    : String(accessErr?.message ?? "凭证签发失败")
                }
              />
            </Card>
          ) : !workbenchUrl ? (
            <Card className="flex min-h-[520px] items-center justify-center">
              <LoadingState rows={4} />
            </Card>
          ) : (
            <WorkbenchPane
              conv={selected}
              workbenchUrl={workbenchUrl}
              redactedUrl={redactedWorkbenchUrl(access!)}
              nonce={workbenchNonce}
              onRefresh={() => setWorkbenchNonce((n) => n + 1)}
              onInterrupt={() => interruptMutation.mutate(selected.conversation_id)}
              onAbort={() => abortMutation.mutate(selected.conversation_id)}
              interruptPending={interruptMutation.isPending}
              abortPending={abortMutation.isPending}
              events={events ?? []}
            />
          )}
        </div>
      </div>
    </div>
  );
}

/* -------------------- 会话列表项 -------------------- */

function ConversationListItem({
  conv,
  selected,
  onSelect,
}: {
  conv: ConversationOut;
  selected: boolean;
  onSelect: () => void;
}) {
  const openable = canOpenWorkbench(conv.status);
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "w-full rounded-md border px-3 py-2 text-left transition-colors",
        selected
          ? "border-primary bg-primary/5"
          : "border-transparent hover:bg-accent",
        !openable && "opacity-70",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <StatusBadge status={conv.status}>{conv.status}</StatusBadge>
        <span className="text-xs text-muted-foreground">{timeAgo(conv.updated_at)}</span>
      </div>
      <div className="mt-1 truncate text-sm font-medium">
        {conv.title ?? conv.conversation_id}
      </div>
      <div className="mt-0.5 truncate font-mono text-xs text-muted-foreground">
        {conv.opencode_session_id}
      </div>
      {!openable && (
        <div className="mt-1 text-xs text-muted-foreground">需 ACTIVE 才可打开工作台</div>
      )}
    </button>
  );
}

/* -------------------- 工作台面板(iframe + 工具条 + 事件摘要) -------------------- */

function WorkbenchPane({
  conv,
  workbenchUrl,
  redactedUrl,
  nonce,
  onRefresh,
  onInterrupt,
  onAbort,
  interruptPending,
  abortPending,
  events,
}: {
  conv: ConversationOut;
  workbenchUrl: string;
  redactedUrl: string;
  nonce: number;
  onRefresh: () => void;
  onInterrupt: () => void;
  onAbort: () => void;
  interruptPending: boolean;
  abortPending: boolean;
  events: AgentEventOut[];
}) {
  return (
    <Card className="flex h-[calc(100vh-220px)] min-h-[520px] flex-col">
      {/* 工具条 */}
      <CardHeader className="flex-row items-center justify-between space-y-0 border-b pb-3">
        <div className="min-w-0 space-y-1">
          <CardTitle className="flex items-center gap-2 truncate text-sm">
            <StatusBadge status={conv.status}>{conv.status}</StatusBadge>
            <span className="truncate">{conv.title ?? conv.conversation_id}</span>
          </CardTitle>
          <div className="flex items-center gap-2 font-mono text-xs text-muted-foreground">
            <span title="OpenCode session id">{conv.opencode_session_id}</span>
            {conv.agent_run_id && (
              <>
                <span>·</span>
                <span title="agent_run_id 关联">{conv.agent_run_id}</span>
              </>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <Button variant="outline" size="sm" onClick={onRefresh} title="刷新工作台" data-testid="wb-refresh">
            <RefreshCw className="h-3.5 w-3.5" />
            刷新
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={onInterrupt}
            disabled={interruptPending}
            title="中断当前运行(可恢复)"
            data-testid="wb-interrupt"
          >
            <Square className="h-3.5 w-3.5" />
            {interruptPending ? "…" : "中断"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={onAbort}
            disabled={abortPending}
            title="中止当前运行"
            data-testid="wb-abort"
          >
            <StopCircle className="h-3.5 w-3.5" />
            {abortPending ? "…" : "中止"}
          </Button>
        </div>
      </CardHeader>

      {/* iframe(凭证在 URL 中,不渲染为可见文本) */}
      <div className="relative min-h-0 flex-1">
        <iframe
          key={`${conv.conversation_id}-${nonce}`}
          src={workbenchUrl}
          title={`OpenCode Web 工作台(${conv.conversation_id})`}
          className="absolute inset-0 h-full w-full border-0"
          sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
          // 凭证由浏览器在 iframe src 中持有,不展示给用户;redactedUrl 仅用于无障碍/审计
          data-redacted-url={redactedUrl}
        />
      </div>

      {/* 关键事件摘要(脱敏,折叠) */}
      <div className="border-t">
        <Accordion type="single" collapsible>
          <AccordionItem value="events" className="border-0">
            <AccordionTrigger className="px-4 py-2 text-xs hover:no-underline">
              <span className="flex items-center gap-2">
                <History className="h-3.5 w-3.5" />
                关键事件摘要({events.length})
                <ChevronDown className="h-3 w-3" />
              </span>
            </AccordionTrigger>
            <AccordionContent className="max-h-48 overflow-y-auto px-4 pb-3">
              {events.length === 0 ? (
                <p className="py-2 text-center text-xs text-muted-foreground">
                  暂无关键事件(token 级增量不落库,完整历史由 OpenCode Web 提供)
                </p>
              ) : (
                <ul className="space-y-1.5">
                  {events
                    .filter((ev) => ev.type in EVENT_LABELS)
                    .slice(-30)
                    .reverse()
                    .map((ev) => (
                      <li key={`${ev.seq}`} className="flex gap-2 text-xs">
                        <span className="shrink-0 font-mono text-muted-foreground">
                          #{ev.seq}
                        </span>
                        <span className="shrink-0">
                          <StatusBadge status={ev.type in ERROR_TYPES ? "error" : "default"}>
                            {EVENT_LABELS[ev.type] ?? ev.type}
                          </StatusBadge>
                        </span>
                        <span className="min-w-0 flex-1 break-words text-muted-foreground">
                          {summarizeEvent(ev)}
                        </span>
                        <span className="shrink-0 text-muted-foreground">
                          {timeAgo(ev.timestamp)}
                        </span>
                      </li>
                    ))}
                </ul>
              )}
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      </div>
    </Card>
  );
}

const ERROR_TYPES = new Set(["error"]);
