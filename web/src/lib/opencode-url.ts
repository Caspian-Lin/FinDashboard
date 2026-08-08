import type { OpenCodeAccessOut } from "./opencode";

/**
 * 构造 OpenCode Web 工作台的 iframe 嵌入 URL(issue #118 / #111)。
 *
 * #118 采用进程级隔离 + basic auth 保护 OpenCode 端口(强制 127.0.0.1 绑定)。
 * OpenCode v1.18.15 不支持 --base-path 子路径部署,故用跨源 iframe 而非反代。
 * 前端拿到 /api/opencode/access 签发的 web_url + basic auth 凭证后,构造
 * `http://<user>:<password>@127.0.0.1:<port>/` 嵌入 iframe。
 *
 * 安全约束:
 *   - 凭证只在内存中构造 URL,不写入 localStorage / 不打印控制台;
 *   - 只对 FinBoard 授权绑定的 ACTIVE conversation_id 签发(由后端 /access 校验);
 *   - 凭证不出现在可见 DOM 文本中(iframe src 由浏览器持有,不渲染为文本节点)。
 *
 * @param access /api/opencode/access 返回的凭证对象
 * @param conversationId 可选,附加为查询参数便于审计(不影响 OpenCode 路由)
 */
export function buildWorkbenchUrl(
  access: OpenCodeAccessOut,
  conversationId?: string,
): string {
  const url = new URL(access.web_url);
  url.username = encodeURIComponent(access.username);
  url.password = encodeURIComponent(access.password);
  if (conversationId) {
    url.searchParams.set("conv", conversationId);
  }
  return url.toString();
}

/**
 * 构造脱敏的工作台基线 URL(不含凭证),用于 UI 展示和日志。
 * 例如显示 "http://127.0.0.1:4097/" 而不暴露 user:password。
 */
export function redactedWorkbenchUrl(access: OpenCodeAccessOut): string {
  const url = new URL(access.web_url);
  return `${url.protocol}//${url.host}/`;
}
