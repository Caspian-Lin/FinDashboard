# 预设:read-only(`research.read`)

**用途**:只读研究客户端 —— 查数据、查课题、读 ResearchRun / 模拟盘、读报告、
跑纯计算(`finboard_portfolio_*`)、读诊断。用于「看一眼」「解释一下」「复核结论」
这类不产生任何写入的会话。

```bash
# 服务端签发(明文 token 只打印一次)
uv run finboard mcp client create --name codex-readonly --preset readonly
# 回执形如:client_id=MC-xxxxxxxxxxxx  token=fbmc_MC-xxxxxxxxxxxx_<secret>
```

| 项 | 值 |
|---|---|
| 作用域 | `research.read` |
| 可调用 | 只读工具 + 纯计算工具 + 课题读侧(`finboard_topic_read` / `finboard_source_check` / `finboard_memory_page` / `finboard_run_diagnostics` / `finboard_decision_projection` 等) |
| 不可调用 | 一切 `research.write` / `research.run` / `data.write` 工具;`finboard_report_export`(落盘)、`finboard_factor_series_build`(入队)、`finboard_research_stress` 的 `queue`(运行) |

> ⚠️ **只读预设不得用于发起运行**:不要用它创建课题 / 记忆 / 快照 / 策略,不要
> 入队 `finboard_run_queue` / `finboard_backtest_run` / `finboard_topic_run_start`,
> 也不要启动模拟盘会话。越权调用会返回 `status=denied` /
> `error.kind=permission_denied` —— 这是**契约**,不是可以靠重试或换参数绕过的
> 临时错误。需要写就另签一个 `topic-writer` 客户端,不要给只读凭据加权限。

## 客户端配置

所有客户端共用同一端点与 Bearer 凭据,token 从环境变量读,**不要写进配置文件**:

```bash
# 端点(本机 loopback 默认)
export FINBOARD_MCP_URL=http://127.0.0.1:8765/mcp
export FINBOARD_MCP_TOKEN=<create 回执里的 token>
```

### Codex(已实测,#519)

`~/.codex/config.toml`:

```toml
[mcp_servers.finboard]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "FINBOARD_MCP_TOKEN"

# 可选:限制该 server 的工具白名单/超时(按本机 codex 版本支持情况)
```

等价的一次性命令:`codex mcp add finboard --url http://127.0.0.1:8765/mcp
--bearer-token-env-var FINBOARD_MCP_TOKEN`。非交互跑法:

```bash
codex exec --ignore-user-config \
  -c 'mcp_servers.finboard.url="http://127.0.0.1:8765/mcp"' \
  -c 'mcp_servers.finboard.bearer_token_env_var="FINBOARD_MCP_TOKEN"' \
  "用 finboard_topic_read(view=\"context\") 读课题 RT-…,一句话说明目标与最近轮次。"
```

### DeepSeek Harness / ZCode(待验)

- **DeepSeek Harness**:本机运行时以**插件包**方式挂载 MCP server(profile 里的
  blender / godot 都是 `@local/*-mcp` 插件),没有可直接粘贴的 settings 片段;
  #519 未打包 finboard MCP 插件,故标**待验**。字段需求与其它客户端相同:
  streamable HTTP URL + `Authorization: Bearer $FINBOARD_MCP_TOKEN`。
- **ZCode**:本机未安装(`Get-Command zcode` 无结果),配置字段未验证,**待验**。
  不要照抄未经验证的片段;以其官方 MCP 文档的「streamable HTTP + 自定义 header」
  写法为准,端点与 token 同上。
- 已验 / 待验清单与日期见 `docs/agent-integration.md`。

### 外部 OpenCode(已实测,#519)

`opencode.json`(项目级)或 `~/.config/opencode/opencode.json`(全局):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "finboard": {
      "type": "remote",
      "url": "http://127.0.0.1:8765/mcp",
      "enabled": true,
      "oauth": false,
      "headers": { "Authorization": "Bearer {env:FINBOARD_MCP_TOKEN}" }
    }
  }
}
```

等价命令:`opencode mcp add finboard --url http://127.0.0.1:8765/mcp
--header "Authorization=Bearer {env:FINBOARD_MCP_TOKEN}"`;用 `opencode mcp list`
确认 `finboard` 已连接。

## 网络边界

- 默认只绑 loopback(`FINBOARD_MCP_HOST=127.0.0.1`);容器 / 远程场景必须服务端
  显式 `FINBOARD_MCP_ALLOW_REMOTE=true` **且**置于 TLS 反向代理之后(MCP server
  自身不终结 TLS)。
- 非 loopback + 无 `ALLOW_REMOTE` → 服务端具名拒绝启动(退出码 2),不会静默放宽。

## 排障速查

| 现象 | 原因 / 处理 |
|---|---|
| 客户端连不上 / 401 | token 错、已吊销或过期;`uv run finboard mcp client list` 看 `state` 与 `token_prefix` |
| 工具调用返回 `denied` | 这条凭据没有对应作用域;只读预设**只能读**,换 `topic-writer` 客户端 |
| 服务端启动退出码 2 | `FINBOARD_MCP_ENABLED=false`、端口占用、或 HTTP 无任何可用凭据(具名错误) |
| 看不到工具 | 客户端未连上 MCP server 或 server 初始化失败;查客户端 MCP 日志 |
