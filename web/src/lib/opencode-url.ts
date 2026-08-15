import type { OpenCodeAccessOut } from "./opencode";

/**
 * 构造 OpenCode Web 工作台的嵌入 / 新窗口 URL(issue #118 / #111 / #121 / #157)。
 *
 * #157 用户决策:单用户模型下 OpenCode Web 不启用 basic auth,直接使用明文
 * `http://127.0.0.1:<port>/?directory=/workspace`。iframe 与「新窗口打开」共用
 * 同一 URL —— URL 中不再有凭证,新标签页不会 401,密码也不再进入浏览器历史 /
 * DOM 属性 / Referer 链路。宿主机侧 127.0.0.1 绑定是唯一网络边界。
 *
 * **directory=/workspace 强制传参**:opencode web 前端 SPA 会把上次打开的工作目录
 * 缓存在 localStorage。如果用户曾直接访问 `localhost:4097`,localStorage 会记住宿主机
 * 路径(如 `C:/Users/.../FinDashboard`),iframe 加载时把它作为 session directory 发给
 * 容器内 opencode —— 但容器是 Linux,找不到这个 Windows 路径,导致 `prompt_async` ENOENT
 * 失败(发消息无回复无报错)。强制传 `?directory=/workspace`(容器内 bind mount 路径)
 * 确保每次加载都使用正确的容器内路径。
 *
 * @param access /api/opencode/access 返回的访问信息(明文 web_url)
 */
export function buildWorkbenchUrl(access: OpenCodeAccessOut): string {
  const url = new URL(access.web_url);
  // 强制容器内工作目录,避免 SPA localStorage 缓存的宿主机路径导致 ENOENT。
  url.searchParams.set("directory", "/workspace");
  return url.toString();
}
