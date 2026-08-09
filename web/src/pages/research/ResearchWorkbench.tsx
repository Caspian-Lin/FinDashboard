import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  RefreshCw,
  ShieldCheck,
  AlertTriangle,
  ExternalLink,
  Sparkles,
  FileText,
  History,
  MonitorSmartphone,
} from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusDot } from "@/components/ui/status-badge";
import { LoadingState, ErrorState } from "@/components/ui/states";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { timeAgo } from "@/lib/utils";
import { opencodeGatewayApi } from "@/lib/opencode";
import { buildWorkbenchUrl, redactedWorkbenchUrl } from "@/lib/opencode-url";
import { DraftsTab } from "@/pages/research/approval/DraftsTab";
import { HypothesesTab } from "@/pages/research/approval/HypothesesTab";
import { AuditTab } from "@/pages/research/approval/AuditTab";

/* ============================================================ */
/* OpenCode 研究工作台(issue #111 / 重构 #121)                   */
/* ============================================================ */
//
// 重构 #121:OpenCode 自身管理会话/历史/恢复;FinBoard 不再维护独立 conversation
// 投影层。前端工作台 Tab 简化为 iframe 直连 OpenCode Web + 顶部 gateway 状态 banner。
//
// 边界:
//   - iframe 直连 OpenCode Web(凭证由 /api/opencode/access 签发,前端不缓存/不展示明文);
//   - 写操作(创建 ResearchRun/回测/模拟盘)由 agent 通过 MCP 自主执行(#122);
//   - opencode_web_enabled=false(网关 503)时「工作台」Tab 显示降级提示,
//     「审批中心」Tab 仍可用(审批走 REST 不依赖网关)。

export default function ResearchWorkbench() {
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

  /* ---------- 签发工作台访问凭证(网关启用即签发) ---------- */
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
        description="OpenCode Web 研究交互 + AI 草案/假设审批闭环(不连实盘)"
      />

      <Tabs defaultValue="workbench" className="space-y-4">
        <TabsList>
          <TabsTrigger value="workbench">
            <MonitorSmartphone className="mr-1.5 h-4 w-4" />
            工作台
          </TabsTrigger>
          <TabsTrigger value="approval">
            <ShieldCheck className="mr-1.5 h-4 w-4" />
            审批中心
          </TabsTrigger>
        </TabsList>

        {/* ============ Tab:工作台(OpenCode Web 直连) ============ */}
        <TabsContent value="workbench" className="space-y-4">
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
                网关返回 503,OpenCode Web 工作台不可用。可切换到「审批中心」Tab 管理 AI 草案与因子假设。
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
                    {access && (
                      <a
                        href={redactedWorkbenchUrl(access)}
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
                    title="工作台访问未授权"
                    message={String(accessErr?.message ?? "凭证签发失败")}
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

                  {/* iframe(凭证在 URL 中,不渲染为可见文本) */}
                  <div className="relative min-h-0 flex-1">
                    <iframe
                      key={workbenchNonce}
                      src={workbenchUrl}
                      title="OpenCode Web 研究工作台"
                      className="absolute inset-0 h-full w-full border-0"
                      sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                      // 凭证由浏览器在 iframe src 中持有,不展示给用户;redactedUrl 仅用于无障碍/审计
                      data-redacted-url={redactedWorkbenchUrl(access!)}
                    />
                  </div>
                </Card>
              )}
            </div>
          )}
        </TabsContent>

        {/* ============ Tab:审批中心(AI 草案/假设/审计) ============ */}
        <TabsContent value="approval" className="space-y-4">
          <Alert>
            <ShieldCheck className="h-4 w-4" />
            <AlertTitle>审批边界</AlertTitle>
            <AlertDescription>
              审批中心是 AI 草案/假设的审计与历史查看入口。审批走 REST API,
              不依赖 OpenCode Web 网关。AI 只读研究上下文,不连接实盘账户、不发送订单、不修改持仓。
            </AlertDescription>
          </Alert>
          <Tabs defaultValue="drafts">
            <TabsList>
              <TabsTrigger value="drafts">
                <Sparkles className="mr-1.5 h-4 w-4" />
                AI 草案
              </TabsTrigger>
              <TabsTrigger value="hypotheses">
                <FileText className="mr-1.5 h-4 w-4" />
                假设管理
              </TabsTrigger>
              <TabsTrigger value="audit">
                <History className="mr-1.5 h-4 w-4" />
                审计
              </TabsTrigger>
            </TabsList>
            <TabsContent value="drafts" className="mt-4"><DraftsTab /></TabsContent>
            <TabsContent value="hypotheses" className="mt-4"><HypothesesTab /></TabsContent>
            <TabsContent value="audit" className="mt-4"><AuditTab /></TabsContent>
          </Tabs>
        </TabsContent>
      </Tabs>
    </div>
  );
}
