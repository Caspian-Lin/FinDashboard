import { fetchJSON } from "./api";

/* ============================================================ */
/* OpenCode 研究运行时 API client (issue #118 + #111 + 重构 #121) */
/* ============================================================ */
//
// 后端契约:
//   - /api/opencode/*  —— OpenCode Web 网关控制面(#118)
//
// 重构 #121:FinBoard 不再维护独立研究会话投影层,会话/历史由 OpenCode 自身管理。
// 前端只查询网关状态 + 签发访问凭证,iframe 直连 OpenCode Web。
// 红线:前端不连实盘。

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

/** OpenCode Web 访问凭证(iframe 跨源嵌入用)。 */
export interface OpenCodeAccessOut {
  web_url: string;
  username: string;
  password: string;
  agent_name: string;
}

/** OpenCode Web 网关控制面:状态 / 健康 / 凭证签发。 */
export const opencodeGatewayApi = {
  status: () => fetchJSON<OpenCodeStatusOut>(`/opencode/status`),
  health: () => fetchJSON<OpenCodeHealthOut>(`/opencode/health`),
  /** 签发 OpenCode Web 访问凭证(网关启用即签发,#121 重构后无 conversation 绑定)。 */
  access: () =>
    fetchJSON<OpenCodeAccessOut>(`/opencode/access`, { method: "POST" }),
};
