import { Navigate, useLocation } from "react-router-dom";

/**
 * 旧 `/research/workbench` → `/research/agent-connect` 参数保全跳转(issue #520)。
 *
 * 内置 OpenCode 工作台退役后,旧深链(尤其 `?topic=RT-…` 交接上下文)不能
 * 404 或丢参数:整个 query string 原样带过去,由新接入页继续交接。
 */
export function LegacyWorkbenchRedirect() {
  const { search } = useLocation();
  return <Navigate to={`/research/agent-connect${search}`} replace />;
}
