# MCP 客户端凭据与作用域(#517)

FinDashboard 的独立研究 MCP(`packages/finboard-mcp`)通过服务端签发的
**按客户端 Bearer 凭据**授权外部 Agent。本文覆盖创建 / 吊销 / 预设 / 身份如何
到达工具 / TLS 与网络边界。工具清单与逐工具作用域见
`packages/finboard-mcp/src/finboard_mcp/scopes.py` 与
`packages/finboard-mcp/README.md`。

## 1. 身份模型

HTTP 请求携带 `Authorization: Bearer <token>`;服务端按 `sha256(token)` 在
`mcp_clients` 表查找客户端,校验未吊销、未过期后解析为:

```python
McpClientIdentity(client_id="MC-<12hex>", name="codex", scopes=frozenset({...}))
```

- 明文 token 只在 `create` 时打印一次;数据库只存 `token_hash`(sha256)与
  `token_prefix`(secret 前 16 字符,仅用于人工辨认,不能认证)。
- 未命中 / 已吊销 / 已过期一律 401,响应体完全相同,不泄露客户端是否存在;
  比较使用常量时间函数,并对「不存在」路径做哑比较。
- `last_used_at` 最多每 60 秒更新一次;已吊销凭据不会再更新。
- 旧 `FINBOARD_MCP_AUTH_TOKEN` 在兼容期合成 `MC-legacy` / `scopes=["*"]`,
  启动打印一行 deprecation warning,下一版本移除。

身份如何到达工具(设计选择):ASGI 中间件把身份写入
`scope["state"]["mcp_identity"]`;每个工具入口的 `app_context(ctx)` 从 MCP
`request_context.request` 取出并写入 `contextvars.ContextVar`;唯一执行漏斗
`execution.run_tool` 读取 contextvar 强制作用域。没有把 identity 加进 155 个
工具签名(改面过大且易漏),也没有只依赖纯 contextvar(实测 MCP SDK 的
in-memory / stdio 任务不会继承外层 ContextVar)。stdio 无 HTTP 请求时回退到
`FINBOARD_MCP_STDIO_SCOPES` 默认身份。

## 2. 作用域与预设

| 作用域 | 覆盖能力 |
| --- | --- |
| `research.read` | 研究域只读(数据查询、因子、ResearchRun、策略、模拟盘查询、报告聚合、诊断、课题读侧) |
| `research.write` | 研究域结构化写入(课题 / 记忆 / 策略规格 / 因子与验证实验元数据 / 自选股 / 研究代码提交 / 报告导出落盘) |
| `research.run` | 启动 / 取消 / 重放研究运行、回测、参数网格、模拟盘会话、沙箱执行(含 `finboard_factor_series_build` 入队构建) |
| `data.write` | 全局数据操作(拉取 / 批量下载 / 发布 / 修复 / ETF / 配置 / 补数) |
| `audit.read` | 审计读取(保留给运维 / 审计客户端) |

预设(CLI `--preset`):

| 预设 | 作用域 |
| --- | --- |
| `readonly` | `{research.read}` |
| `topic-writer` | `{research.read, research.write, research.run}` |
| `data-operator` | `{research.read, research.write, research.run, data.write}` |

`audit.read` 不进入常规研究预设。也可用 `--scopes a,b,c` 显式组合。

注意:`finboard_report_export` 有落盘副作用,归入 `research.write`;
`finboard_factor_series_build` 会入队沙箱构建,归入 `research.run`;readonly
客户端不能调用这两个工具。

## 3. 创建 / 列出 / 吊销

```bash
# 创建:明文 token 只打印一次,请立即写入客户端密钥配置
uv run finboard mcp client create --name codex --preset readonly
uv run finboard mcp client create --name harness --scopes research.read,research.write,research.run
uv run finboard mcp client create --name ops --scopes research.read,audit.read --expires-in-days 30

# 列出(不显示 secret / token 摘要;显示 client_id / name / scopes / token_prefix /
# state=active|expired|revoked)
uv run finboard mcp client list

# 吊销(幂等;已吊销保留首次吊销时间)
uv run finboard mcp client revoke MC-0123456789ab
```

数据库迁移:`alembic upgrade head`(revision `11f6ed5aeef6`)。回滚:
`alembic downgrade -1` 删除 `mcp_clients`;已签发 token 随之失效,可临时回退
旧共享 token 维持接入。

## 4. 强制点与失败语义

- 唯一漏斗 `execution.run_tool` 在 handler 之前检查
  `TOOL_SCOPES[tool_name] ⊆ identity.scopes`;`"*"` 仅 legacy 兼容。
- `TOOL_SCOPES` 未登记的工具 = 契约错误 = **拒绝**(fail-closed),并记录
  `mcp.scope_contract_missing`;契约测试比对全部注册工具名。
- 越权返回 `status=denied` / `error.kind=permission_denied`,handler 不执行,
  业务零写入;拒绝事件同样进审计(`caller` = client_id)。
- 混合 operation 工具(如 `finboard_research_stress` 的 plan/get 与 queue)按
  最高权限取并集 —— 工具级强制点不接受参数级绕过。
- `FINBOARD_MCP_READONLY_ONLY=true`(兼容期)等价于所有客户端只保留
  `research.read`;启动打印 deprecation warning。
- stdio 默认 `research.read`;需要写操作必须显式配置
  `FINBOARD_MCP_STDIO_SCOPES`(如 `research.read,research.write,research.run`)。

## 5. 传输、TLS 与网络边界

| 场景 | 默认 | 规则 |
| --- | --- | --- |
| 本机 stdio | **默认传输**(`FINBOARD_MCP_TRANSPORT=stdio`) | 可信本机子进程;无 HTTP 鉴权,身份由 stdio 作用域决定 |
| 本机 HTTP | `FINBOARD_MCP_TRANSPORT=streamable-http` @ `127.0.0.1:8765` | 必须有凭据(env token 或至少一个有效客户端) |
| 非 loopback 绑定 | 拒绝 | 必须 `FINBOARD_MCP_ALLOW_REMOTE=true`;前置 TLS 反向代理 |

- MCP server 自身**不终结 TLS**,也不提供多用户隔离;对外暴露必须置于
  TLS 反向代理之后,并限制来源网络。
- HTTP 启动时会校验凭据:无 `FINBOARD_MCP_AUTH_TOKEN` 且 `mcp_clients` 无
  有效客户端 → 具名拒绝启动(退出码 2)。
- `FINBOARD_MCP_ENABLED=false` → 具名拒绝启动(退出码 2);未设置默认启用。
- 独立启动不读取任何 `FINBOARD_OPENCODE_*` 变量。
- Make 目标:`make mcp-serve-http`(本机 loopback,凭据用 `mcp_clients` 或兼容期
  `MCP_AUTH_TOKEN`)、`make mcp-serve-http-remote`(非 loopback,显式
  `ALLOW_REMOTE=true`)、`make mcp-client-create`(签发客户端)。
- 重复启动(端口占用)→ FinBoard 具名错误并退出码 2,不会静默换端口。

## 6. 运维检查清单

1. `uv run alembic upgrade head` 应用 `mcp_clients` 迁移。
2. `uv run finboard mcp client create ...` 为每个外部客户端签发独立凭据。
3. 客户端配置 `Authorization: Bearer <token>`;不要把 token 写进仓库或日志。
4. `uv run finboard mcp client list` 定期核对 `last_used_at` 与 `state`。
5. 离职 / 轮换:立即 `revoke`;必要时再签发新客户端并更新客户端配置。
6. 对外部署:确认 `FINBOARD_MCP_ALLOW_REMOTE=true` 仅与 TLS 反向代理同时使用。
