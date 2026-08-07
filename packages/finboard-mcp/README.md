# finboard-mcp

FinBoard MCP Server —— 把 FinDashboard 的研究能力以受控 MCP 工具的形式暴露给外置
Agent 运行时(OpenCode)。FinDashboard 仍是业务工具 / 权限 / 任务 / 数据 / 审计 / 产物的
唯一事实来源;MCP 工具复用现有 service / repository / `ResearchAssistant`,不直接连接
数据库做裸 SQL,不暴露实盘能力。

## 安全边界

- **只读工具**(数据 / 因子 / ResearchRun / 模拟盘查询 / AI 问答)自动允许;
- **写操作**(创建 ResearchRun / 回测 / 模拟盘 / 定时任务)走「草案 + 人工审批」,
  AI 无法自主执行;
- **实盘能力**(下单 / 撤单 / 改持仓 / Kill Switch / 连接 broker / 凭证探测)永久不注册;
- 每次工具调用统一记录审计事件,不记录 API Key / 原始凭证 / 未脱敏思考内容。

## 工具集(增量扩展中)

| 命名空间 | 工具 | 说明 |
| --- | --- | --- |
| `finboard.ai.*` | `finboard_ai_ask` / `finboard_ai_propose_hypothesis` / `finboard_ai_propose_strategy_draft` / `finboard_ai_propose_strategy_diff` | AI 研究助手(复用 `ResearchAssistant`,自带权限矩阵 + 脱敏) |
| `finboard.run.*` | `finboard_run_list` / `finboard_run_get` / `finboard_run_artifacts` | ResearchRun 只读查询 |

所有工具返回统一信封 `ToolEnvelope`(`operation_id` / `status` / `data` / `error` /
`provenance` / `idempotency_key`)。

## 运行

```bash
# 默认 stdio 传输(供 OpenCode 等本地 host 子进程接入)
uv run python -m finboard_mcp

# HTTP 传输
FINBOARD_MCP_TRANSPORT=streamable-http FINBOARD_MCP_HOST=127.0.0.1 FINBOARD_MCP_PORT=8765 \
  uv run python -m finboard_mcp
```

配置项(`FINBOARD_` 前缀):`mcp_enabled` / `mcp_transport` / `mcp_host` / `mcp_port` /
`mcp_readonly_only` / `mcp_audit_persist`。

## 回滚

关闭 MCP 入口(不启动 server)即可,不影响现有 `ResearchAssistant` / REST 入口与
研究产物 / 审计历史。
