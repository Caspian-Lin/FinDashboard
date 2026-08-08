import { fetchJSON } from "./api";

/* ============================================================ */
/* OpenCode 研究运行时 API client (issue #109 + #118 + #111)    */
/* ============================================================ */
//
// 后端契约:
//   - /api/agent/conversations/*       —— 会话生命周期 + SSE + 历史回放(#109)
//   - /api/opencode/*                  —— OpenCode Web 网关控制面(#118)
//
// 红线:前端只读取会话/事件/状态,不绕过 MCP/审批门触发研究写操作,
//       不展示未脱敏思考内容,不连实盘。

/* -------------------- 会话(#109) -------------------- */

export type ConversationStatus =
  | "active"
  | "interrupted"
  | "completed"
  | "failed"
  | "orphaned";

export interface ConversationOut {
  conversation_id: string;
  opencode_session_id: string;
  agent_run_id: string | null;
  title: string | null;
  status: ConversationStatus;
  agent_name: string;
  model_ref: string | null;
  last_event_seq: number;
  created_at: string;
  updated_at: string;
}

export interface ConversationCreate {
  title?: string | null;
  agent_run_id?: string | null;
  agent?: string | null;
  model?: string | null;
}

/** OpenCode 投影的关键事件(#109 持久化在 agent_events,token 级 delta 不落库)。 */
export interface AgentEventOut {
  seq: number;
  type: string;
  role: string | null;
  payload: Record<string, unknown>;
  timestamp: string;
}

/** 会话 API:列表 / 详情 / 创建 / 历史回放 / 中断 / 中止。 */
export const conversationApi = {
  list: (limit = 50) =>
    fetchJSON<ConversationOut[]>(`/agent/conversations?limit=${limit}`),
  get: (id: string) =>
    fetchJSON<ConversationOut>(`/agent/conversations/${id}`),
  create: (body: ConversationCreate) =>
    fetchJSON<ConversationOut>(`/agent/conversations`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  /** 关键事件历史回放(从本地 agent_events 读取,OpenCode 离线也可用)。 */
  history: (id: string, afterSeq = 0) =>
    fetchJSON<AgentEventOut[]>(
      `/agent/conversations/${id}/history?after_seq=${afterSeq}`,
    ),
  interrupt: (id: string) =>
    fetchJSON<void>(`/agent/conversations/${id}/interrupt`, { method: "POST" }),
  abort: (id: string) =>
    fetchJSON<void>(`/agent/conversations/${id}/abort`, { method: "POST" }),
};

/* -------------------- OpenCode Web 网关(#118) -------------------- */

/** 隔离实例运行状态(脱敏,不含密码)。 */
export interface OpenCodeStatusOut {
  running: boolean;
  managed: boolean;
  container_id: string | null;
  base_url: string;
  healthy: boolean | null;
  version: string | null;
  started_at: string | null;
}

export interface OpenCodeHealthOut {
  healthy: boolean;
  version: string | null;
  detail: Record<string, unknown> | null;
}

/** 为已授权 ACTIVE 会话签发的访问凭证(iframe 跨源嵌入用)。 */
export interface OpenCodeAccessOut {
  conversation_id: string;
  opencode_session_id: string;
  web_url: string;
  username: string;
  password: string;
  agent_name: string;
}

/** OpenCode Web 网关控制面:状态 / 健康 / 凭证签发。 */
export const opencodeGatewayApi = {
  status: () => fetchJSON<OpenCodeStatusOut>(`/opencode/status`),
  health: () => fetchJSON<OpenCodeHealthOut>(`/opencode/health`),
  /** 为已授权会话签发 OpenCode Web 访问凭证(仅 ACTIVE 会话)。 */
  access: (conversationId: string) =>
    fetchJSON<OpenCodeAccessOut>(`/opencode/access`, {
      method: "POST",
      body: JSON.stringify({ conversation_id: conversationId }),
    }),
};
