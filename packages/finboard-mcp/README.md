# finboard-mcp

FinBoard MCP Server —— 把 FinDashboard 的研究能力以受控 MCP 工具的形式暴露给外置
Agent 运行时。FinDashboard 仍是业务工具 / 权限 / 任务 / 数据 / 审计 / 产物的
唯一事实来源;MCP 工具复用现有 service / repository,不直接连接
数据库做裸 SQL,不暴露实盘能力。

## 安全边界(#122 审批门语义)

- **只读工具**(数据 / 因子 / ResearchRun / 模拟盘查询)自动允许;
- **研究写操作**(创建因子 / 快照 / 策略 / ResearchRun、运行回测、发布数据、启动
  模拟盘)由 agent 通过 MCP **自主执行**(#122),不触及交易安全红线;
- **实盘能力**(下单 / 撤单 / 改持仓 / Kill Switch / 连接 broker / 凭证探测)永久不注册;
- 每次工具调用统一记录审计事件,不记录 API Key / 原始凭证 / 未脱敏思考内容。

## 工具集(148 个;#160 移除 finboard.ai.* 后 FinBoard 零内置 LLM)

完整清单与契约见 server `_INSTRUCTIONS` 与 Skill
`.agents/skills/finboard-opencode-research/references/tools.md`;扩展路线图见
`ROADMAP.md`。每个注册工具都必须在 `finboard_mcp/scopes.py` 的 `TOOL_SCOPES`
登记作用域,未登记即 fail-closed(契约测试比对 148 个注册名)。

| 命名空间 | 数量 | 说明 |
| --- | --- | --- |
| `finboard.run.*` | 7 | ResearchRun 查询 + queue/cancel/replay/lineage |
| `finboard.memory.*` | 7 | 研究长期记忆(`research_memories` 表) |
| 数据查询 / 目录 / 范围 / 补数 | 20 | 标的元数据 / 发布 / 缓存 / 质量 / 目录 / 补数请求 |
| `finboard.factor.*` / `.feature_snapshot.*` | 12 | 因子实验室(8 只读 + 4 写) |
| `finboard.factor_series.*` | 2 | 因子序列工件(读取 / 托管重建) |
| `finboard.strategy.*` / `.preset.*` | 16 | 无代码策略规格生命周期 |
| `finboard.backtest.*` | 7 | 同步回测 + 历史 CRUD + 参数网格 |
| `finboard.sim.*` | 21 | 模拟盘全生命周期(隔离 `simulation_*` 表) |
| `finboard.portfolio.*` | 4 | 组合计算(allocate/sizing/feasibility/attribution) |
| `finboard.job.*` | 7 | 统一后台任务队列(研究/数据域) |
| 数据写操作 | 11 | 拉取 / 批量下载 / 发布 / 修复 / ETF / 配置 |
| `finboard.validation_experiment.*` | 7 | #57 OOS 机器验证实验 |
| `finboard.watchlist.*` | 7 | 自选股标的组管理 |
| 报告 / 诊断 / 课题 / 研究代码 | 20 | 报告聚合导出 / 确定性复核 / 课题读侧 / 研究代码仓库与沙箱 |

所有工具返回统一信封 `ToolEnvelope`(`operation_id` / `status` / `data` / `error` /
`provenance` / `idempotency_key`)。

## 运行(独立于 OpenCode 与交易内核)

```bash
# 默认 stdio 传输(本机 host 以子进程接入;默认作用域 research.read)
uv run python -m finboard_mcp

# HTTP 传输(必须有服务端凭据;本机默认 loopback)
FINBOARD_MCP_TRANSPORT=streamable-http FINBOARD_MCP_HOST=127.0.0.1 FINBOARD_MCP_PORT=8765 \
  uv run python -m finboard_mcp
```

启动门禁(#517):

- `FINBOARD_MCP_ENABLED=false` → 打印具名原因,退出码 **2**;未设置时默认启用;
- HTTP 必须至少有一种凭据:旧 `FINBOARD_MCP_AUTH_TOKEN`,或 `mcp_clients` 表中
  未吊销 / 未过期的客户端;都没有 → 具名拒绝启动;
- `FINBOARD_MCP_HOST` 非 loopback → 必须 `FINBOARD_MCP_ALLOW_REMOTE=true`,
  否则具名拒绝;放行后启动日志打印 TLS / 反向代理要求(本服务不终结 TLS);
- 独立启动不读取任何 `FINBOARD_OPENCODE_*` 变量;仅需 `FINBOARD_DB_URL`(工具执行用)。

配置项(`FINBOARD_` 前缀):`mcp_enabled` / `mcp_transport` / `mcp_host` /
`mcp_port` / `mcp_allow_remote` / `mcp_stdio_scopes` / `mcp_auth_token`(弃用) /
`mcp_readonly_only`(弃用) / `mcp_audit_persist`。

## 身份与作用域(#517)

HTTP 请求的 `Authorization: Bearer <token>` 由服务端解析为
`McpClientIdentity(client_id, name, scopes)`:

| 作用域 | 含义 |
| --- | --- |
| `research.read` | 研究域只读 |
| `research.write` | 研究域结构化写入(课题 / 记忆 / 策略规格 / 因子与验证元数据 / 自选股) |
| `research.run` | 启动 / 取消 / 重放研究运行、回测、模拟盘会话与沙箱执行 |
| `data.write` | 全局数据操作(拉取 / 批量下载 / 发布 / 修复 / ETF / 补数) |
| `audit.read` | 审计读取(保留给运维 / 审计客户端) |

预设:

| 预设 | 作用域 |
| --- | --- |
| `readonly` | `research.read` |
| `topic-writer` | `research.read` + `research.write` + `research.run` |
| `data-operator` | 上述 + `data.write` |

注意:`finboard_report_export` 有落盘副作用,归入 `research.write`;
`finboard_factor_series_build` 会入队沙箱构建,归入 `research.run`;readonly
客户端不能调用这两个工具。

```bash
# 签发(明文 token 只打印一次;库里只存 sha256 摘要)
uv run finboard mcp client create --name codex --preset readonly
uv run finboard mcp client create --name harness --scopes research.read,research.write,research.run
uv run finboard mcp client list          # 只展示 token_prefix,不展示明文 / 摘要
uv run finboard mcp client revoke MC-xxxx
```

越权调用在唯一漏斗 `execution.run_tool` 被拒绝,返回
`status=denied` / `error.kind=permission_denied`,handler 不执行(零业务写入)。
混合工具(如 `finboard_research_stress` 的 plan/get 与 queue)按最高权限取并集,
只读客户端不能调用其只读 operation —— 工具级强制点不接受参数级绕过。

兼容期行为(下一版本移除):

- `FINBOARD_MCP_AUTH_TOKEN` 设置时合成 `MC-legacy` / `scopes=["*"]`,启动打印
  deprecation warning;
- `FINBOARD_MCP_READONLY_ONLY=true` 等价于所有客户端只保留 `research.read`,
  启动打印 deprecation warning;
- stdio 默认作用域为 `research.read`(此前等价全权):需要写操作必须显式设置
  `FINBOARD_MCP_STDIO_SCOPES`。这是**行为收紧**,已在本 issue 显式声明。

完整创建 / 吊销 / 预设 / TLS / 网络边界见 [`docs/mcp-client-auth.md`](../../docs/mcp-client-auth.md)。

## 审计(#157)

默认 structlog 结构化日志 + 内存副本;`FINBOARD_MCP_AUDIT_PERSIST=true` 时每条
审计事件追加到独立 `mcp_audit_events` 表(重启不丢),REST `GET /api/mcp/audit`
可查询。入参先经 `summarize_arguments` 脱敏 / 截断;作用域拒绝同样进审计,
`caller` 记录客户端 ID。

## 回滚

关闭 MCP 入口(不启动 server)即可;凭据迁移回滚为 `alembic downgrade` 删除
`mcp_clients` 表,并临时回退到 `FINBOARD_MCP_AUTH_TOKEN` 旧共享 token。不影响
现有 REST 入口与研究产物 / 审计历史。
