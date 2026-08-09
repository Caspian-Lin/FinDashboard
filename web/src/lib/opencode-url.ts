import type { OpenCodeAccessOut } from "./opencode";

/**
 * 构造 OpenCode Web 工作台的 iframe 嵌入 URL(issue #118 / #111 / 重构 #121)。
 *
 * #118 采用进程级隔离 + basic auth 保护 OpenCode 端口(强制 127.0.0.1 绑定)。
 * OpenCode v1.18.15 不支持 --base-path 子路径部署,故用跨源 iframe 而非反代。
 * 前端拿到 /api/opencode/access 签发的 web_url + basic auth 凭证后,构造
 * `http://<user>:<password>@127.0.0.1:<port>/?directory=/workspace` 嵌入 iframe。
 *
 * 重构 #121:OpenCode 自身管理会话/历史;FinBoard 只签发凭证,不再绑定 conversation。
 *
 * **directory=/workspace 强制传参**:opencode web 前端 SPA 会把上次打开的工作目录
 * 缓存在 localStorage。如果用户曾直接访问 `localhost:4097`,localStorage 会记住宿主机
 * 路径(如 `C:/Users/.../FinDashboard`),iframe 加载时把它作为 session directory 发给
 * 容器内 opencode —— 但容器是 Linux,找不到这个 Windows 路径,导致 `prompt_async` ENOENT
 * 失败(发消息无回复无报错)。强制传 `?directory=/workspace`(容器内 bind mount 路径)
 * 确保每次 iframe 加载都使用正确的容器内路径。
 *
 * 安全约束:
 *   - 凭证只在内存中构造 URL,不写入 localStorage / 不打印控制台;
 *   - 凭证不出现在可见 DOM 文本中(iframe src 由浏览器持有,不渲染为文本节点)。
 *
 * @param access /api/opencode/access 返回的凭证对象
 */
export function buildWorkbenchUrl(access: OpenCodeAccessOut): string {
  const url = new URL(access.web_url);
  url.username = encodeURIComponent(access.username);
  url.password = encodeURIComponent(access.password);
  // 强制容器内工作目录,避免 SPA localStorage 缓存的宿主机路径导致 ENOENT。
  url.searchParams.set("directory", "/workspace");
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
