# finboard-mcp

FinBoard MCP Server —— 把 FinDashboard 的研究能力以受控 MCP 工具的形式暴露给外置
Agent 运行时(OpenCode)。FinDashboard 仍是业务工具 / 权限 / 任务 / 数据 / 审计 / 产物的
唯一事实来源;MCP 工具复用现有 service / repository,不直接连接
数据库做裸 SQL,不暴露实盘能力。

## 安全边界(#122 审批门语义)

- **只读工具**(数据 / 因子 / ResearchRun / 模拟盘查询 / AI 问答)自动允许;
- **研究写操作**(创建因子 / 快照 / 策略 / ResearchRun、运行回测、发布数据、启动
  模拟盘)由 agent 通过 MCP **自主执行**(#122),不触及交易安全红线;
- **实盘能力**(下单 / 撤单 / 改持仓 / Kill Switch / 连接 broker / 凭证探测)永久不注册;
- 每次工具调用统一记录审计事件,不记录 API Key / 原始凭证 / 未脱敏思考内容。

## 工具集(115 个,分阶段扩展中;#160 移除 finboard.ai.* 后 FinBoard 零内置 LLM)

完整清单与契约见 server `_INSTRUCTIONS` 与 Skill
`.agents/skills/finboard-opencode-research/references/tools.md`;扩展路线图见
`ROADMAP.md`。命名空间概览:

| 命名空间 | 数量 | 说明 |
| --- | --- | --- |
| `finboard.run.*` | 7 | ResearchRun 查询 + queue/cancel/replay/lineage |
| `finboard.memory.*` | 7 | 研究长期记忆(`research_memories` 表) |
| 数据查询 | 9 | 标的元数据 / 数据集发布 / 缓存 / 质量 / Tushare 配额 |
| `finboard.factor.*` / `.feature_snapshot.*` | 12 | 因子实验室(8 只读 + 4 写) |
| `finboard.strategy.*` / `.preset.*` | 16 | 无代码策略规格生命周期 |
| `finboard.backtest.*` | 5 | 同步回测 + 历史 CRUD |
| `finboard.sim.*` | 21 | 模拟盘全生命周期(隔离 `simulation_*` 表) |
| `finboard.portfolio.*` | 4 | 组合计算(allocate/sizing/feasibility/attribution) |
| `finboard.job.*` | 4 | 统一后台任务队列(研究/数据域) |
| 数据写操作 | 12 | 拉取 / 批量下载 / 发布 / 修复 / ETF / 配置 |
| 验证实验 | 6 | #57 OOS 机器验证实验元数据 |
| `finboard.watchlist.*` | 7 | 自选股标的组管理 |
| 报告 | 3 | 聚合 + CSV/Markdown 导出(`finboard_report_export`) |

所有工具返回统一信封 `ToolEnvelope`(`operation_id` / `status` / `data` / `error` /
`provenance` / `idempotency_key`)。

## 运行

```bash
# 默认 stdio 传输(供 OpenCode 等本地 host 子进程接入)
uv run python -m finboard_mcp

# HTTP 传输(必须设置 Bearer token;容器内 OpenCode 经 host.docker.internal 接入)
FINBOARD_MCP_TRANSPORT=streamable-http FINBOARD_MCP_HOST=0.0.0.0 FINBOARD_MCP_PORT=8765 \
FINBOARD_MCP_AUTH_TOKEN=<token> \
  uv run python -m finboard_mcp
```

配置项(`FINBOARD_` 前缀):`mcp_enabled` / `mcp_transport` / `mcp_host` / `mcp_port` /
`mcp_auth_token` / `mcp_readonly_only` / `mcp_audit_persist`。

## 审计(#157)

默认 structlog 结构化日志 + 内存副本;`FINBOARD_MCP_AUDIT_PERSIST=true` 时每条
审计事件追加到独立 `mcp_audit_events` 表(重启不丢),REST `GET /api/mcp/audit`
可查询。入参先经 `summarize_arguments` 脱敏 / 截断。

## 回滚

关闭 MCP 入口(不启动 server)即可;审计持久化回滚为关闭配置 + 迁移 downgrade。
不影响现有 REST 入口与研究产物 / 审计历史。
