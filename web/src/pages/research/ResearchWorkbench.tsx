import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  RefreshCw,
  ShieldCheck,
  AlertTriangle,
  ExternalLink,
} from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusDot } from "@/components/ui/status-badge";
import { LoadingState, ErrorState } from "@/components/ui/states";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { timeAgo } from "@/lib/utils";
import { opencodeGatewayApi } from "@/lib/opencode";
import { buildWorkbenchUrl } from "@/lib/opencode-url";

/* ============================================================ */
/* OpenCode 研究工作台(issue #111 / 重构 #121 / #157 / #160)      */
/* ============================================================ */
//
// 重构 #121:OpenCode 自身管理会话/历史/恢复;FinBoard 不再维护独立 conversation
// 投影层。前端工作台为 iframe 直连 OpenCode Web + 顶部 gateway 状态 banner。
// #160:AI 能力全面迁移 OpenCode,审批中心(REST 审批闭环)随内置 LLM 一并移除。
//
// 边界:
//   - iframe 直连 OpenCode Web(#157 移除 basic auth:明文 URL,单用户模型,
//     127.0.0.1 绑定是唯一网络边界;iframe 与「新窗口打开」共用同一 URL);
//   - 写操作(创建 ResearchRun/回测/模拟盘)由 agent 通过 MCP 自主执行(#122);
//   - banner 显示 OpenCode Web 与内嵌 FinBoard MCP server 的运行状态(#157);
//   - opencode_web_enabled=false(网关 503)时显示降级提示。

export default function ResearchWorkbench() {
  const [workbenchNonce, setWorkbenchNonce] = useState(0);
  // 首次使用引导(opencode web 项目列表在浏览器本地存储,首次打开为空)。
  const [guideDismissed, setGuideDismissed] = useState(
    () => localStorage.getItem("finboard-wb-guide-dismissed") === "1",
  );

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

  /* ---------- 获取工作台访问信息(明文 web_url,网关启用即签发) ---------- */
  const {
    data: access,
    isError: accessError,
    error: accessErr,
  } = useQuery({
    queryKey: ["opencode-access"],
    queryFn: () => opencodeGatewayApi.access(),
    enabled: gatewayEnabled,
    retry: false,
  });

  const workbenchUrl = access ? buildWorkbenchUrl(access) : null;
  const showWorkbenchDowngrade = !gatewayEnabled && !statusLoading;

  return (
    <div>
      <PageHeader
        title="研究工作台"
        description="OpenCode Web 研究交互层(不连实盘)"
      />

      <div className="space-y-4">
        {/* 边界提示 */}
        <Alert>
          <ShieldCheck className="h-4 w-4" />
          <AlertTitle>研究边界</AlertTitle>
          <AlertDescription>
            OpenCode Web 是研究交互层,OpenCode 自身管理会话/历史/恢复。
            研究写操作(创建 ResearchRun/回测/模拟盘)由 agent 通过 MCP 自主执行。
            工作台不连接实盘 broker/账户/订单/持仓。
          </AlertDescription>
        </Alert>

        {showWorkbenchDowngrade ? (
          <Alert variant="warning">
            <AlertTriangle className="h-4 w-4" />
            <AlertTitle>OpenCode Web 网关未启用</AlertTitle>
            <AlertDescription>
              网关返回 503,OpenCode Web 工作台不可用。请检查
              opencode_web_enabled 配置与 Docker 容器状态。
            </AlertDescription>
          </Alert>
        ) : (
          <div className="space-y-3">
            {/* ============ 顶部:网关状态 banner ============ */}
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="flex items-center justify-between text-sm">
                  <span className="flex items-center gap-2">
                    <StatusDot
                      status={
                        status?.healthy
                          ? "online"
                          : status?.running
                            ? "warning"
                            : "offline"
                      }
                    />
                    OpenCode Web
                  </span>
                  {status?.version && (
                    <span className="font-mono text-xs text-muted-foreground">
                      v{status.version}
                    </span>
                  )}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground">
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                    <span>
                      实例状态:
                      <span className="ml-1 font-medium text-foreground">
                        {status?.running ? "运行中" : "未运行"}
                      </span>
                    </span>
                    <span>
                      健康探测:
                      <span className="ml-1 font-medium text-foreground">
                        {status?.healthy == null
                          ? "未知"
                          : status.healthy
                            ? "健康"
                            : "异常"}
                      </span>
                    </span>
                    {/* #157:内嵌 FinBoard MCP 状态(agent 的工具通道) */}
                    <span className="flex items-center gap-1">
                      FinBoard MCP:
                      <StatusDot
                        status={
                          status?.mcp?.embedded_running ? "online" : "offline"
                        }
                      />
                      <span className="font-medium text-foreground">
                        {status?.mcp == null
                          ? "未知"
                          : status.mcp.embedded_running
                            ? `内嵌运行中(${status.mcp.host ?? "?"}:${status.mcp.port ?? "?"})`
                            : status.mcp.embedded_configured
                              ? "已配置未运行(检查 MCP_AUTH_TOKEN)"
                              : "未启用内嵌(独立进程模式)"}
                      </span>
                    </span>
                    {status?.managed && status.container_id != null && (
                      <span>
                        容器:
                        <span className="ml-1 font-mono text-foreground">
                          {status.container_id}
                        </span>
                      </span>
                    )}
                    {status?.started_at && (
                      <span>
                        启动:
                        <span className="ml-1 text-foreground">
                          {timeAgo(status.started_at)}
                        </span>
                      </span>
                    )}
                  </div>
                  {workbenchUrl && (
                    <a
                      href={workbenchUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-primary hover:underline"
                    >
                      <ExternalLink className="h-3 w-3" />
                      新窗口打开
                    </a>
                  )}
                </div>
              </CardContent>
            </Card>

            {/* ============ iframe 直连 OpenCode Web ============ */}
            {accessError ? (
              <Card className="flex min-h-[520px] items-center justify-center">
                <ErrorState
                  title="工作台访问信息获取失败"
                  message={String(accessErr?.message ?? "访问信息签发失败")}
                />
              </Card>
            ) : !workbenchUrl ? (
              <Card className="flex min-h-[520px] items-center justify-center">
                <LoadingState rows={4} />
              </Card>
            ) : (
              <Card className="flex h-[calc(100vh-340px)] min-h-[520px] flex-col">
                {/* 工具条 */}
                <CardHeader className="flex-row items-center justify-between space-y-0 border-b pb-3">
                  <CardTitle className="flex items-center gap-2 truncate text-sm">
                    <StatusDot status={status?.healthy ? "online" : "warning"} />
                    OpenCode Web 研究工作台
                  </CardTitle>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setWorkbenchNonce((n) => n + 1)}
                    title="刷新工作台"
                    data-testid="wb-refresh"
                  >
                    <RefreshCw className="h-3.5 w-3.5" />
                    刷新
                  </Button>
                </CardHeader>
                  {/* 首次使用引导:opencode web 项目列表在浏览器本地(IndexedDB),
                      全新分区(iframe 嵌入/新窗口)首次打开不显示历史会话 */}
                  {workbenchUrl && !guideDismissed && (
                    <Alert className="items-start">
                      <Sparkles className="mt-0.5 h-4 w-4" />
                      <div className="flex-1 space-y-1">
                        <AlertTitle>首次使用:打开历史会话</AlertTitle>
                        <AlertDescription>
                          OpenCode Web 的项目列表保存在浏览器本地,首次打开(iframe
                          或新窗口各自独立)会显示空白。点击左侧「添加项目」→
                          搜索框输入 <code className="rounded bg-muted px-1 font-mono text-xs">/</code>
                          → 点击 <code className="rounded bg-muted px-1 font-mono text-xs">~</code>
                          (主目录,即容器内 /workspace)即可恢复历史会话;打开一次后
                          该浏览器会自动记住。
                        </AlertDescription>
                      </div>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          localStorage.setItem("finboard-wb-guide-dismissed", "1");
                          setGuideDismissed(true);
                        }}
                      >
                        我知道了
                      </Button>
                    </Alert>
                  )}

                  {/* iframe(#157 明文 URL,无凭证;直连 OpenCode Web) */}
                  <div className="relative min-h-0 flex-1">
                    <iframe
                      key={workbenchNonce}
                      src={workbenchUrl}
                      title="OpenCode Web 研究工作台"
                      className="absolute inset-0 h-full w-full border-0"
                      sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                    />
                  </div>
                </Card>
              )}
            </div>
          )}
        </TabsContent>

                {/* iframe(#157 明文 URL,无凭证;直连 OpenCode Web) */}
                <div className="relative min-h-0 flex-1">
                  <iframe
                    key={workbenchNonce}
                    src={workbenchUrl}
                    title="OpenCode Web 研究工作台"
                    className="absolute inset-0 h-full w-full border-0"
                    sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                  />
                </div>
              </Card>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
