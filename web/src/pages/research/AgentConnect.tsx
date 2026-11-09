import { Link, useSearchParams } from "react-router-dom";
import {
  AlertTriangle,
  BookOpen,
  KeyRound,
  Plug,
  ShieldCheck,
  Terminal,
} from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useT } from "@/i18n";

/* ============================================================ */
/* 外部 Agent 接入与迁移(#519 接入 / #520 退役内置工作台)        */
/* ============================================================ */
//
// 本页是 `/research/workbench` 的替代入口:
//   - FinDashboard 不内置 LLM,也不托管 OpenCode 容器 / iframe(#520);
//   - 外部 Harness 经 loopback MCP + 按客户端 Bearer 凭据接入研究域;
//   - 保留 `?topic=RT-…` 深链上下文,交接给外部 Agent 续接 BJ- / 引用 RR-。
// 服务端契约见 docs/agent-integration.md 与 docs/mcp-client-auth.md。

const MCP_ENDPOINT = "http://127.0.0.1:8765/mcp";

function CodeBlock({ children }: { children: string }) {
  return (
    <pre className="overflow-x-auto rounded-md border border-border bg-muted/40 p-3 text-xs leading-relaxed">
      <code>{children}</code>
    </pre>
  );
}

const PRESETS: { name: string; scopes: string; useZh: string; useEn: string }[] = [
  {
    name: "readonly",
    scopes: "research.read",
    useZh: "只读研究 / 解释 / 复核",
    useEn: "Read-only research, explanation, review",
  },
  {
    name: "topic-writer",
    scopes: "research.read + research.write + research.run",
    useZh: "日常研究 Agent(写课题 / 起运行)",
    useEn: "Daily research agent (write topics, start runs)",
  },
  {
    name: "data-operator",
    scopes: "topic-writer + data.write",
    useZh: "数据运维(拉取 / 发布 / 补数)",
    useEn: "Data operations (fetch, publish, backfill)",
  },
];

export default function AgentConnect() {
  const { tl } = useT();
  const [params] = useSearchParams();
  const topicId = params.get("topic");

  return (
    <div>
      <PageHeader
        title={tl({ zh: "Agent 接入", en: "Agent Connect" })}
        description={tl({
          zh: "用外部 Harness 经 MCP 接入研究域；FinDashboard 不内置 Agent 运行时。",
          en: "Connect an external Harness over MCP; FinDashboard does not host an agent runtime.",
        })}
      />

      <div className="space-y-4">
        <Alert>
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>
            {tl({ zh: "内置研究工作台已退役(#520)", en: "Built-in workbench retired (#520)" })}
          </AlertTitle>
          <AlertDescription>
            {tl({
              zh: "原 OpenCode Web 工作台(iframe)、托管容器与 /api/opencode 网关已移除。旧 /research/workbench 深链会自动跳转到本页并保留 ?topic= 参数；课题、运行、因子、OOS 与模拟盘等手工流程全部保留。",
              en: "The former OpenCode Web workbench (iframe), managed container, and /api/opencode gateway are removed. Old /research/workbench deep links redirect here with the ?topic= parameter preserved; manual research flows (topics, runs, factors, OOS, simulation) remain fully available.",
            })}
          </AlertDescription>
        </Alert>

        <Alert>
          <ShieldCheck className="h-4 w-4" />
          <AlertTitle>{tl({ zh: "研究域边界", en: "Research boundary" })}</AlertTitle>
          <AlertDescription>
            {tl({
              zh: "MCP 只暴露研究 / 数据能力，永久不注册下单、撤单、改持仓、Kill Switch、连接 broker 或凭证探测；写操作按凭据作用域由服务端 fail-closed 强制。",
              en: "MCP exposes research and data capabilities only. Order placement/cancellation, position mutation, Kill Switch, broker connection, and credential probing are permanently absent; write access is enforced fail-closed by credential scope on the server.",
            })}
          </AlertDescription>
        </Alert>

        {/* 当前课题交接 */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <BookOpen className="h-4 w-4 text-muted-foreground" />
              {tl({ zh: "课题交接", en: "Topic handoff" })}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {topicId ? (
              <>
                <p className="text-sm text-muted-foreground">
                  {tl({
                    zh: "本页携带课题上下文，把下面这条调用作为外部 Agent 的第一条指令，即可续接原课题：",
                    en: "This page carries a topic context. Give the external agent the call below as its first instruction to resume the topic:",
                  })}
                </p>
                <CodeBlock>{`finboard_topic_read(topic_id="${topicId}", view="context")`}</CodeBlock>
                <div className="flex flex-wrap items-center gap-3 text-sm">
                  <Link
                    className="text-primary underline"
                    to={`/research/topics?topic=${encodeURIComponent(topicId)}`}
                  >
                    {tl({ zh: "回到课题", en: "Back to topic" })} · {topicId}
                  </Link>
                  <span className="text-xs text-muted-foreground">
                    {tl({
                      zh: "context 返回目标版本、最近轮次、断链引用与 next_actions（≤64 KiB）。",
                      en: "context returns the target version, recent rounds, broken references, and next_actions (≤64 KiB).",
                    })}
                  </span>
                </div>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                {tl({
                  zh: "没有携带课题上下文。可在「研究课题」选择课题后点「继续与 agent 研究」带 ?topic= 跳转，或直接把 RT- 编号交给 Agent。",
                  en: "No topic context is attached. Pick a topic under Research Topics and choose “Continue with an agent”, or hand the RT- ID to the agent directly.",
                })}
              </p>
            )}

            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-muted-foreground">
                  <tr>
                    <th className="py-1 pr-4 font-medium">{tl({ zh: "对象", en: "Object" })}</th>
                    <th className="py-1 font-medium">{tl({ zh: "接续方式", en: "How to resume" })}</th>
                  </tr>
                </thead>
                <tbody className="align-top">
                  <tr className="border-t border-border">
                    <td className="py-2 pr-4 font-mono">RT-</td>
                    <td className="py-2">
                      {tl({
                        zh: "课题：finboard_topic_read(view=\"context\") 读上下文后再行动",
                        en: "Topic: read finboard_topic_read(view=\"context\") before acting",
                      })}
                    </td>
                  </tr>
                  <tr className="border-t border-border">
                    <td className="py-2 pr-4 font-mono">BJ-</td>
                    <td className="py-2">
                      {tl({
                        zh: "后台任务：finboard_job_wait(job_id=\"BJ-…\")；超时 completed=false 时续等同一 job，不重新入队",
                        en: "Background job: finboard_job_wait(job_id=\"BJ-…\"); on completed=false keep waiting on the same job, never re-enqueue",
                      })}
                    </td>
                  </tr>
                  <tr className="border-t border-border">
                    <td className="py-2 pr-4 font-mono">RR-</td>
                    <td className="py-2">
                      {tl({
                        zh: "运行：finboard_run_get(run_id=\"RR-…\") 读冻结输入与状态；课题内运行用 finboard_topic_run_list",
                        en: "Run: finboard_run_get(run_id=\"RR-…\") for frozen inputs/status; use finboard_topic_run_list within a topic",
                      })}
                    </td>
                  </tr>
                  <tr className="border-t border-border">
                    <td className="py-2 pr-4">{tl({ zh: "收尾", en: "Close-out" })}</td>
                    <td className="py-2">
                      {tl({
                        zh: "finboard_topic_write(operation=\"append\") 追加轮次 + finboard_memory_remember(tags=[\"research-round\"])；docs/research/ 才是 canonical 结论",
                        en: "Append a round with finboard_topic_write(operation=\"append\") plus finboard_memory_remember(tags=[\"research-round\"]); docs/research/ remains the canonical conclusion",
                      })}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>

        {/* 接入三步 */}
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Terminal className="h-4 w-4 text-muted-foreground" />
                {tl({ zh: "1. 启动独立 MCP 服务", en: "1. Start the standalone MCP server" })}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <CodeBlock>{`FINBOARD_MCP_TRANSPORT=streamable-http \\
FINBOARD_MCP_HOST=127.0.0.1 \\
FINBOARD_MCP_PORT=8765 \\
uv run python -m finboard_mcp`}</CodeBlock>
              <p className="text-sm text-muted-foreground">
                {tl({ zh: "端点：", en: "Endpoint: " })}
                <code className="font-mono text-xs">{MCP_ENDPOINT}</code>
              </p>
              <p className="text-xs text-muted-foreground">
                {tl({
                  zh: "独立入口不依赖交易内核、OpenCode 或 Docker。本机子进程也可用 stdio：uv run python -m finboard_mcp（默认仅 research.read，写操作需显式 FINBOARD_MCP_STDIO_SCOPES）。",
                  en: "The standalone entry does not depend on the trading kernel, OpenCode, or Docker. For a local subprocess use stdio: uv run python -m finboard_mcp (research.read only by default; writes need explicit FINBOARD_MCP_STDIO_SCOPES).",
                })}
              </p>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-sm">
                <KeyRound className="h-4 w-4 text-muted-foreground" />
                {tl({ zh: "2. 为每个客户端签发凭据", en: "2. Issue a credential per client" })}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <CodeBlock>{`uv run finboard mcp client create --name codex --preset topic-writer
uv run finboard mcp client list
uv run finboard mcp client revoke MC-xxxxxxxxxxxx`}</CodeBlock>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="text-muted-foreground">
                    <tr>
                      <th className="py-1 pr-3 font-medium">{tl({ zh: "预设", en: "Preset" })}</th>
                      <th className="py-1 pr-3 font-medium">{tl({ zh: "作用域", en: "Scopes" })}</th>
                      <th className="py-1 font-medium">{tl({ zh: "用途", en: "Use" })}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {PRESETS.map((preset) => (
                      <tr key={preset.name} className="border-t border-border align-top">
                        <td className="py-1.5 pr-3 font-mono">{preset.name}</td>
                        <td className="py-1.5 pr-3 font-mono text-muted-foreground">{preset.scopes}</td>
                        <td className="py-1.5">{tl({ zh: preset.useZh, en: preset.useEn })}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-xs text-muted-foreground">
                {tl({
                  zh: "明文 token 只打印一次；库里只存 sha256 摘要。token 用环境变量注入客户端，不要写进仓库、配置或日志。audit.read 不进常规预设。",
                  en: "The plaintext token is printed once; only its sha256 digest is stored. Inject it into the client via an environment variable — never into the repo, config, or logs. audit.read is not part of the regular presets.",
                })}
              </p>
            </CardContent>
          </Card>
        </div>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Plug className="h-4 w-4 text-muted-foreground" />
              {tl({ zh: "3. 配置外部 Harness", en: "3. Configure the external Harness" })}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm text-muted-foreground">
            <p>
              {tl({
                zh: "已验：Codex CLI 0.163.0、外部 OpenCode 2.0.32（读 context + 追加轮次 + 只读越权被拒）。DeepSeek Harness / ZCode 待验，不提供未经验证的片段。",
                en: "Verified: Codex CLI 0.163.0 and external OpenCode 2.0.32 (read context, append a round, readonly denial). DeepSeek Harness / ZCode are pending; no unverified snippets are offered.",
              })}
            </p>
            <ul className="list-disc space-y-1 pl-5 text-xs">
              <li>
                {tl({ zh: "逐客户端安装 / 配置 / 排障：", en: "Per-client install, config, and troubleshooting: " })}
                <code className="font-mono">docs/agent-integration.md</code>
              </li>
              <li>
                {tl({ zh: "客户端无关 Skill（Agent 行为契约）：", en: "Client-agnostic Skill (agent behavior contract): " })}
                <code className="font-mono">.agents/skills/finboard-research/</code>
              </li>
              <li>
                {tl({ zh: "凭据 / 作用域 / TLS 边界：", en: "Credentials, scopes, and TLS boundary: " })}
                <code className="font-mono">docs/mcp-client-auth.md</code>
              </li>
            </ul>
          </CardContent>
        </Card>

        {/* 迁移与备份 */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm">
              {tl({ zh: "迁移与备份(不自动删除卷)", en: "Migration and backup (volumes are never auto-deleted)" })}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm text-muted-foreground">
            <p>
              {tl({
                zh: "旧版本可能留下命名卷 opencode-data(会话 + auth.json)与 opencode-config。FinDashboard 代码不会删除它们；如不再使用外部 OpenCode，可按下面命令先只读导出备份：",
                en: "Older versions may leave named volumes opencode-data (sessions + auth.json) and opencode-config. FinDashboard code never deletes them; if you no longer use external OpenCode, export a read-only backup first:",
              })}
            </p>
            <CodeBlock>{`docker run --rm -v opencode-data:/d:ro -v "$PWD:/out" alpine \\
  sh -c 'cp -a /d /out/opencode-data-backup'
docker run --rm -v opencode-config:/d:ro -v "$PWD:/out" alpine \\
  sh -c 'cp -a /d /out/opencode-config-backup'`}</CodeBlock>
            <p className="text-xs">
              {tl({
                zh: "如需在外部 OpenCode 继续使用原会话 / auth,可让它自行把数据目录指向解压后的备份(或直接挂载同一命名卷);FinDashboard 不再代管该运行时。删除卷前确认外部 Harness 已可独立工作。旧 FINBOARD_OPENCODE_* 配置键请从 .env / 环境变量清理——API 启动会打一行警告列出仍存在的键，但不会因旧键失败。",
                en: "To keep using the old sessions / auth in an external OpenCode, point its data directory at the extracted backup (or mount the same named volume); FinDashboard no longer manages that runtime. Confirm the external Harness works independently before deleting a volume. Remove leftover FINBOARD_OPENCODE_* keys from .env / the environment — API startup logs one warning listing remaining keys but never fails because of them.",
              })}
            </p>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
