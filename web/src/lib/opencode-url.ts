import type { OpenCodeAccessOut } from "./opencode";

/**
 * 构造 OpenCode Web 工作台的嵌入 / 新窗口 URL(issue #118 / #111 / #121 / #157)。
 *
 * #157 用户决策:单用户模型下 OpenCode Web 不启用 basic auth,直接使用明文
 * `http://127.0.0.1:<port>/?directory=/workspace`。iframe 与「新窗口打开」共用
 * 同一 URL —— URL 中不再有凭证,新标签页不会 401,密码也不再进入浏览器历史 /
 * DOM 属性 / Referer 链路。宿主机侧 127.0.0.1 绑定是唯一网络边界。
 *
 * **directory=/workspace 参数的真实语义(实测 v1.18.15)**:
 * - 服务端请求目录解析(`searchParams.get("directory") || x-opencode-directory ||
 *   process.cwd()`)会用它,而服务端默认 location 本来就是 /workspace(cwd),
 *   因此该参数对服务端是冗余的。
 * - SPA 的「项目列表 / 最近会话」状态**不读该参数**,持久化在浏览器本地存储
 *   (IndexedDB,按 origin + iframe 分区隔离)。全新浏览器分区打开时项目列表
 *   为空,「最近会话」显示空白 —— 这是 opencode web 客户端行为,URL 参数无法
 *   覆盖。首次使用需在 UI 里「添加项目」打开一次 `/workspace`(文件选择器输入
 *   `/` 即可看到,或选择器已从 /workspace 开始),之后该分区会记住。
 * - 若想从 URL 直接打开指定目录的会话,可用 deep-link 路由
 *   `/<base64(directory)>/session`,但会进入会话页而非项目主页,不适合做默认入口。
 *
 * @param access /api/opencode/access 返回的访问信息(明文 web_url)
 */
export function buildWorkbenchUrl(access: OpenCodeAccessOut): string {
  const url = new URL(access.web_url);
  // 强制容器内工作目录(服务端请求目录解析,与容器 cwd 保持一致)。
  url.searchParams.set("directory", "/workspace");
  return url.toString();
}
