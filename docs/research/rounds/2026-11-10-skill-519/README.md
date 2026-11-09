# #519 通用研究 Skill 与外部 Harness 接入验收(2026-11-10)

本目录是 issue #519「通用 Skill + 外部 Harness 接入契约」的**真实客户端验收证据**。
两台外部 Harness 在 loopback MCP 上跑通了「认证 → 读课题上下文 → 追加轮次 →
只读越权被拒」的端到端链路;未实测的客户端如实标注**待验**。

- 分支:`feat/research-skill-harness-519`(基于里程碑 `1c4fb30`,含 #513/#514/#515/#517/#518)
- 环境:Windows 10 / Python 3.12.12 / uv 0.9.18 / PostgreSQL(WSL)
- 服务端:`packages/finboard-mcp`,`FINBOARD_MCP_TRANSPORT=streamable-http`,
  `127.0.0.1:8781`(避开主目录 8765 与 wt4 `.env` 的 8767),`legacy_token=False`
  (未设置 `FINBOARD_MCP_AUTH_TOKEN`,**只认按客户端凭据**)
- 数据库:wt4 开发库 `findashboard_wt4`(`alembic upgrade head` 到
  `9c4f2a7d1e63`,含课题 / 运行链接 / 审计 / `mcp_clients`)

## 结果总表

| 客户端 | 版本 | 配置方式 | 读课题 context | 追加轮次 | readonly 写入被拒 | 状态 |
|---|---|---|---|---|---|---|
| Codex(OpenAI Codex CLI) | `codex-cli 0.163.0` | `~/.codex/config.toml` + `codex exec -c` 覆盖 | ✅ `status=ok` | ✅ `RE-391bab58c76648a581ed0c6b` | ✅ `denied` / `permission_denied` | **已验** |
| 外部 OpenCode | `opencode v2.0.32` | 项目 `opencode.json`(`"mcp": {"finboard": {"type": "remote"}}`) | ✅ `status=ok` | ✅ `RE-dacddc0cc903469faac1c34d` | ✅ `denied` / `permission_denied` | **已验** |
| DeepSeek Harness | 运行中(本 issue 的实现会话) | MCP 以**插件包**形态挂载(本机 profile 里是 `@local/*-mcp`) | — | — | — | **待验** |
| ZCode | 未安装 | — | — | — | — | **待验** |

待验的**确切原因**:

- **DeepSeek Harness**:本机运行时没有可粘贴的 MCP server 配置文件 —— MCP 接入是
  以 profile 插件包(`@local/blender-official-mcp`、`@local/godot-mcp` 等)安装的,
  `~/.dsh` 下没有声明远程 MCP server 的 settings 片段,#519 也未打包 finboard MCP
  插件。字段需求与其它客户端相同(streamable HTTP URL + `Authorization: Bearer`),
  但**未在真实运行中验证**,因此不写"已支持"。待验项记录在
  `docs/agent-integration.md` §待验。
- **ZCode**:`Get-Command zcode` 无结果(未安装),无法实测;文档只给"端点 +
  Bearer"的字段需求,不提供未经验证的配置片段。

## 验收场景(实际执行的步骤)

1. 用 `topic-writer` 预设在服务端签发凭据(明文 token 只打印一次,本目录已脱敏)。
2. 通过 MCP 建一个课题(seed:`RT-9dac7584b738450bb1ac42f1`,目标 `v1`),作为
   「用 `RT-` 交接」的锚点。seed 用同一套 MCP HTTP 通道完成,避免把客户端 LLM 的
   不确定性混进数据准备。
3. **Codex(topic-writer)**:`finboard_topic_read(view="context")` → 追加
   `entry_type=round` 轮次(`entry_id=RE-391bab58c76648a581ed0c6b`)。
4. **Codex(readonly)**:同一个课题,`finboard_topic_read` 成功;`finboard_topic_write`
   返回 `status=denied` / `error.kind=permission_denied`
   (`客户端缺少所需作用域:research.write(已授予:research.read)`)。
5. **外部 OpenCode(topic-writer)**:新进程、新会话,`view=context` 读到的
   `最近轮次=1` 正是 Codex 写入的那条 —— 跨客户端、跨会话**续接**成立;随后追加
   自己的轮次(`entry_id=RE-dacddc0cc903469faac1c34d`)。
6. **外部 OpenCode(readonly)**:读 `ok`、写 `denied`(同上语义)。
7. 服务端侧核对:课题最终 3 条 entry(`goal` + 2 条 `round`),readonly 的两次越权
   尝试**零业务写入**;structlog 审计行 `caller=MC-… status=denied
   error_kind=permission_denied`。

## 证据索引

| 文件 | 内容 |
|---|---|
| `01-environment.md` | 版本、服务端启动命令与日志、凭据签发(脱敏)、数据库状态 |
| `02-codex.md` | Codex 命令、参数、回执、踩坑(审批策略 / stdin 传 prompt) |
| `03-opencode.md` | 外部 OpenCode 配置、非交互命令、回执、踩坑(服务端口 / 配置发现 / 模型抖动) |
| `04-server-log.md` | 服务端启动与越权拒绝日志摘录 |
| `streams/codex-writer.jsonl` | Codex 写入场景 `--json` 事件流(裁掉无关事件,保留工具调用与回执) |
| `streams/codex-readonly.jsonl` | Codex 只读越权场景事件流 |
| `streams/opencode-writer.jsonl` | OpenCode 写入场景 `--format json` 事件流(输出截断到 2000 字符) |
| `streams/opencode-readonly.jsonl` | OpenCode 只读越权场景事件流 |

原始未裁剪日志留在 worktree 的 `.tmp/skill519/`(`.tmp/` 已 gitignore),不进仓库。
> 目录名用 `streams/` 而不是 `logs/`:仓库 `.gitignore` 全局忽略 `logs/`,放在那里
> 会被静默排除(本轮第一版提交就踩过)。

## 未覆盖 / 待验(不夸大)

- **DeepSeek Harness / ZCode** 未实测(原因见上)。因此 issue 验收标准里的
  「DeepSeek Harness 或 ZCode 至少一款」这一条,本 issue 是**用外部 OpenCode**
  作为第二款客户端满足的,不是 Harness/ZCode。
- **断线续接**:证据是「Codex 会话结束 → OpenCode 新进程读到 Codex 的轮次并继续」
  以及「readonly 与 writer 是不同客户端、不同连接」。**没有**在单个客户端内做
  TCP 断链 / 服务端重启演练(未覆盖),需要时另立 issue。
- **原 `BJ-` / `RR-` 续等**:本机 wt4 开发库没有可用的研究运行(`research_runs=0`),
  所以本轮只验证了课题轮次里的 `source_refs` 语义与 `view=context` 的引用核验,
  **没有**真实 `BJ-`/`RR-` 的等待与引用回执。协议字段(documentation 契约)在
  `references/tools.md` 与 `_INSTRUCTIONS` 中已同步,但未做端到端运行证据。
- 客户端**日志可用性**单独记录,见 `02-codex.md` / `03-opencode.md` 的「日志」小节。

