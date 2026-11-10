# DeepSeek Harness 接入实测(issue #542,2026-11-10)

## 目标

把 DeepSeek Harness(DSH)从 `docs/agent-integration.md` 的「待验」改为「已验」,
并留下可复核的过程记录:工具发现、真实回执、作用域拒绝、进程重启恢复、Skill 目录
发现,以及一次「连上但 0 工具」的定位过程。

**不是**研究结论,不改任何冻结产物;它只记录客户端接入事实。

## 环境

| 项 | 值 |
|---|---|
| FinDashboard | `m/research-topics-and-agents` @ `611becb`(迁移 head `b7d1e94c3a26`) |
| MCP server | `python -m finboard_mcp`,`streamable-http`,`127.0.0.1:8765` |
| 凭据 | `MC-a77b75839d2b`,预设 `topic-writer`(`research.read` + `research.write` + `research.run`) |
| 客户端 | DeepSeek Harness `44.7.0`(Electron),profile `desktop` |
| 工具数 | 本次快照 156;唯一事实源是 `finboard_mcp.scopes.TOOL_SCOPES` |

## 步骤与证据

### 1. 服务端与凭据

```bash
FINBOARD_MCP_TRANSPORT=streamable-http FINBOARD_MCP_HOST=127.0.0.1 \
  FINBOARD_MCP_PORT=8765 uv run python -m finboard_mcp      # Uvicorn running on http://127.0.0.1:8765
uv run finboard mcp client create --name dsh-harness --preset topic-writer
# → MC-a77b75839d2b,作用域 research.read,research.run,research.write
#   token 明文 fbmc_<redacted>,仅打印一次
```

服务端接入独立复核(不经过 DSH,直接用 MCP Python 客户端):

```
tool_count: 156
  has finboard_topic_read: True    has finboard_topic_write: True
  has finboard_topic_run_start: True   has finboard_audit_query: True
topic_read(limit=2, view=list) → status=ok, OP-f2ff450db02d
audit_query(limit=1)           → status=denied, permission_denied
                                  (已授予 research.read, research.run, research.write)
```

### 2. DSH 侧配置

`~/.dsh/profiles/desktop/cordis.patch.yml` 增加 `finboard-mcp`(`@deepseek-ai/dsh-mcp-client`,
streamable-http)与 `skill-filesystem`(`- id: skill-filesystem` / `disabled: false`);
token 写 `~/.dsh/secrets/finboard-mcp.token`(`icacls` 去继承、仅本用户可读),配置用
`!!js` 读 `FINBOARD_MCP_TOKEN` 或该文件。

### 3. 真实工具回执(经 DSH 调用)

| 工具(模型侧名) | 回执 |
|---|---|
| `mcp__finboard__finboard_topic_read` | `status=ok`,`OP-fa09f0ce74f4`,`RT-345ed180c8244a8ba939ef22` |
| `mcp__finboard__finboard_memory_page` | `status=ok`,`OP-4c915aea2537`,`RM-3863e276a0fc4091ade8cda9` |
| `mcp__finboard__finboard_instrument_search` | `status=ok`,`OP-040a1b8e0c64`,`600519.SH 贵州茅台` |

越权:凭据无 `audit.read`,`finboard_audit_query` 返回 `denied`(`permission_denied`),
不是可重试错误。

### 4. 进程重启恢复

杀掉 MCP 进程并重启(同一端口/同一库),**不动 DSH**,下一次工具调用
(`finboard_instrument_search`)直接成功 → 客户端按请求重试恢复,无需重载插件。
限制:连续失败会消耗桥的 `reconnect.maxAttempts`(默认 10),耗尽后工具被注销,
必须 HMR 重载或重启 Host(该分支未实测,来自 `dsh-mcp-client` 默认值)。

### 5. Skill 目录发现

初始状态下 `finboard-research` **不在** DSH 技能目录,原因两个:provider
(`- id: skill-filesystem`)默认关闭;`SKILL.md` frontmatter 缺 `name`(DSH 要求
`name` + `description`,缺 `name` 只留一条 warn)。补齐后无需重启 DSH,技能目录里
即出现 `finboard-research`(watcher 热更新)。

## 「连上但 0 工具」的定位过程

现象:`list_mcp_resources(server="finboard")` → `mcp-client(finboard): server is
disconnected`;服务端 access log 全是 200,`mcp_clients.last_used_at` 在更新。

1. 在 loopback 串一个 TCP 探针(`8766 → 8765`)打印双向字节,看到固定循环:
   `server/discover`(mcp-protocol-version `2026-07-28`)→ `subscriptions/listen` →
   收到 `notifications/subscriptions/acknowledged` → **连接立刻被客户端关闭** →
   重新 `server/discover`。循环周期呈退避。
2. 从 `C:\Program_Files\dsh\resources\app.asar` 抽出
   `dsh/node_modules/@deepseek-ai/dsh-mcp-client/lib/index.js`,grep 到
   `DEFAULT_MAX_INSTRUCTION_BYTES = 32768`,以及
   `if (Buffer.byteLength(instructions) > maxInstructionBytes) throw ...`。
3. 实测 FinBoard `_INSTRUCTIONS` = 43,087 字节(UTF-8)> 32,768 → 桥在**连接与订阅
   成功之后**读 instructions 才抛错,判连接失败并进入退避重连。
4. 配置 `maxInstructionBytes: 262144` 后,`list_mcp_resources` 返回服务端
   capabilities(不再是 disconnected),工具逐个可用。

已排除的方向:鉴权(401)、协议版本(服务端 `mcp` 2.0.0 完整实现了
`server/discover` / `subscriptions/listen` 并被 DSH 正确 ack)、端口与防火墙。

## 结论

- DSH 44.7.0 可作为 FinBoard 研究 MCP 的客户端;**必须**设
  `maxInstructionBytes`(≥ 64 KiB 余量)、`toolCallTimeoutMs`(> `job_wait` 300s)、
  启用 skill provider,并给 `SKILL.md` 补 `name`。
- 工具清单运行时发现、服务端短时重启自动恢复、Skill 文件热更新;插件配置与 token
  轮换是手工项。
- 未验证:动态新增工具后模型工具表是否自动刷新(只验证了调用路径恢复);
  重连预算耗尽分支;ZCode。

## 未覆盖 / 后续

- DSH 之外的 Harness(ZCode)仍未安装,维持「待验」。
- 代码沙箱相关工具(`research_code_run` / `user_code` 策略 / 用户因子序列)需要
  Docker,本机 daemon 未运行,本次未涉及。
- `make dev` 仍不拉起 MCP;若改为随 dev 托管,需同步修改本文与
  `docs/agent-integration.md` §5。
