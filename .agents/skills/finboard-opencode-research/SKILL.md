---
description: 【已更名】本 Skill 名为 finboard-opencode-research，已迁移为 .agents/skills/finboard-research/（通用名，适配 Codex / DeepSeek Harness / ZCode / 外部 OpenCode）。请改读新目录；本目录仅保留迁移映射，#520 后删除。
---

# ⚠️ 已更名:请使用 `finboard-research`

本目录(`.agents/skills/finboard-opencode-research/`)**自 #519 起弃用**。研究
Skill 的 canonical 位置是:

```
.agents/skills/finboard-research/
```

新目录不绑定任何客户端(不假定 OpenCode 目录、容器挂载或权限 frontmatter),
同一套 `finboard_*` 工具名对所有外部 Harness 生效。

**本目录不再维护 references/**:`references/tools.md`、`workflow.md`、
`memory.md`、`research-workflow.md`、`system-overview.md` 已全部迁到
`.agents/skills/finboard-research/references/`,旧副本已删除(避免两份文档漂移)。
`#520` 将删除整个目录。

## 迁移映射表

| 旧位置 / 旧写法 | 新位置 / 新写法 |
|---|---|
| `.agents/skills/finboard-opencode-research/SKILL.md` | `.agents/skills/finboard-research/SKILL.md` |
| `.agents/skills/finboard-opencode-research/references/tools.md` | `.agents/skills/finboard-research/references/tools.md` |
| `.agents/skills/finboard-opencode-research/references/workflow.md` | `.agents/skills/finboard-research/references/workflow.md` |
| `.agents/skills/finboard-opencode-research/references/memory.md` | `.agents/skills/finboard-research/references/memory.md` |
| `.agents/skills/finboard-opencode-research/references/research-workflow.md` | `.agents/skills/finboard-research/references/research-workflow.md` |
| `.agents/skills/finboard-opencode-research/references/system-overview.md` | `.agents/skills/finboard-research/references/system-overview.md` |
| 工具名 `finboard.run.list` / `finboard.memory.remember` / `finboard.sim.*`(dotted) | `finboard_run_list` / `finboard_memory_remember` / `finboard_sim_*`(**真实注册名**) |
| 家族说法 `finboard.workspace.*` | `finboard_topic_read` / `finboard_topic_write` / `finboard_source_check` / `finboard_memory_page` / `finboard_strategy_explain` / `finboard_decision_explain` |
| 审计事件名 `finboard.research.*` | 审计名保持 dotted(与工具名区分);工具仍用 `finboard_*` |
| 「OpenCode 研究 Agent」 | 「研究 Agent(外部 Harness)」 |
| 容器绝对路径 `/workspace/docs/research/…` | 仓库相对路径 `docs/research/…`(以你的工作目录为根) |
| 「权限由 OpenCode 配置 + FinBoard 服务端策略共同控制」 | 权限由**服务端 Bearer 凭据作用域**决定(`docs/mcp-client-auth.md`);客户端 agent 配置只管本地文件权限 |
| 依赖 OpenCode `permission:` frontmatter / `skill` 权限 | 通用前置:MCP 端点 + 凭据 + `--preset`(见新目录 `presets/`) |
| 会话启动先读 `ROADMAP.md` / `FINDINGS.md` | 先 `finboard_topic_read(view="context")` 接课题 + `finboard_source_check` 核证据,再读文档 |
| 内置 OpenCode Web 工作台(iframe) | 外部 Harness 直连 MCP;客户端配置见 `docs/agent-integration.md` |

## 现在该做什么

1. 把加载路径改成 `.agents/skills/finboard-research/SKILL.md`(或整体目录)。
2. 客户端配置按 `.agents/skills/finboard-research/presets/` 选预设
   (`read-only` / `research-write` / `data-operator`)。
3. 接入步骤、已验 / 待验客户端清单与排障见 `docs/agent-integration.md`。

研究域边界不变:只读直接调用、研究写操作受作用域约束、实盘能力永久不注册。
