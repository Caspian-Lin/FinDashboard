import { fetchJSON } from "./api";

/* ============================================================ */
/* OpenCode 研究运行时 API client (issue #118 + #111 + 重构 #121 + #157) */
/* ============================================================ */
//
// 后端契约:
//   - /api/opencode/*  —— OpenCode Web 网关控制面(#118)
//
// 重构 #121:FinBoard 不再维护独立研究会话投影层,会话/历史由 OpenCode 自身管理。
// 前端只查询网关状态 + 签发访问信息,iframe 直连 OpenCode Web。
// #157:移除 basic auth(单用户明文 URL 决策),AccessOut 不再有 username/password;
// /status 额外返回内嵌 FinBoard MCP server 运行状态。
// 红线:前端不连实盘。

/* -------------------- OpenCode Web 网关(#118) -------------------- */

/** 内嵌 FinBoard MCP server 状态(#157:前端据此显示 MCP 是否已连接)。 */
export interface OpenCodeMcpStatus {
  embedded_configured: boolean;
  embedded_running: boolean;
  host: string | null;
  port: number | null;
  remote_url: string;
  auth: boolean;
}

/** 隔离实例运行状态(无敏感字段)。 */
export interface OpenCodeStatusOut {
  running: boolean;
  managed: boolean;
  container_id: string | null;
  base_url: string;
  healthy: boolean | null;
  version: string | null;
  started_at: string | null;
  mcp: OpenCodeMcpStatus | null;
}

export interface OpenCodeHealthOut {
  healthy: boolean;
  version: string | null;
  detail: Record<string, unknown> | null;
}

/** OpenCode Web 访问信息(#157 移除 basic auth 后只有明文 web_url,无凭证)。 */
export interface OpenCodeAccessOut {
  web_url: string;
  agent_name: string;
}

/** OpenCode Web 网关控制面:状态 / 健康 / 访问信息签发。 */
export const opencodeGatewayApi = {
  status: () => fetchJSON<OpenCodeStatusOut>(`/opencode/status`),
  health: () => fetchJSON<OpenCodeHealthOut>(`/opencode/health`),
  /** 签发 OpenCode Web 访问信息(明文 URL;#121 后无 conversation 绑定)。 */
  access: () =>
    fetchJSON<OpenCodeAccessOut>(`/opencode/access`, { method: "POST" }),
};
