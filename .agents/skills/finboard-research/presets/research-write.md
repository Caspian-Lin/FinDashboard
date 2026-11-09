# 预设:research-write(`topic-writer`)

**用途**:课题内研究写客户端 —— 在只读能力之上,可以维护课题 / 目标版本 / 轮次、
写记忆、建因子与策略元数据,并**发起研究运行**(回测 / ResearchRun / 模拟盘 / 沙箱)。
日常研究 Agent 用这一个预设即可。

```bash
uv run finboard mcp client create --name codex-topic-writer --preset topic-writer
# 回执:client_id=MC-xxxxxxxxxxxx  token=fbmc_MC-xxxxxxxxxxxx_<secret>
```

| 项 | 值 |
|---|---|
| 作用域 | `research.read` + `research.write` + `research.run` |
| 不建议 | `data.write`(全局数据拉取 / 发布 / 补数,见 `data-operator.md`) |
| 典型写操作 | `finboard_topic_write`(create / draft_create / activate / append / transition)、`finboard_memory_*` 写、`finboard_strategy_draft_create` / `publish`、`finboard_feature_snapshot_create`、`finboard_run_queue`、`finboard_topic_run_start`、`finboard_backtest_run`、`finboard_sim_session_*` |
| 典型运行操作 | `finboard_run_queue` / `finboard_run_replay` / `finboard_topic_run_start` / `finboard_backtest_*` / `finboard_research_code_run` / `finboard_factor_series_build` |

> ⚠️ **只读预设不得发起运行**;本预设才允许入队 / 启动。任务一旦入队,后续轮询
> 用 `finboard_job_wait`(有界阻塞,超时 `completed=false` 续**同一个** job),
> 不要因超时重新入队。
>
> 研究写操作可自主执行(#122),但**不等于**可以跳过晋级链:运行结果要经
> screen / OOS 等门,`completed` / `published` 都不是晋级通过。

## 客户端配置

```bash
export FINBOARD_MCP_URL=http://127.0.0.1:8765/mcp
export FINBOARD_MCP_TOKEN=<topic-writer 客户端的 token>
```

### Codex(已实测,#519)

`~/.codex/config.toml`:

```toml
[mcp_servers.finboard]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "FINBOARD_MCP_TOKEN"
```

非交互:

```bash
codex exec --ignore-user-config \
  -c 'mcp_servers.finboard.url="http://127.0.0.1:8765/mcp"' \
  -c 'mcp_servers.finboard.bearer_token_env_var="FINBOARD_MCP_TOKEN"' \
  "读课题 RT-… 的 context,追加一条 research-round 轮次并写一条 memory。"
```

### DeepSeek Harness / ZCode(待验)

- **DeepSeek Harness**:MCP 以插件包形式挂载,本机无可粘贴的 settings 片段,
  #519 未打包插件 → **待验**;需要 streamable HTTP URL +
  `Authorization: Bearer $FINBOARD_MCP_TOKEN`。
- **ZCode**:本机未安装 → **待验**;字段以其官方 MCP 文档为准。
- 已验 / 待验与日期见 `docs/agent-integration.md`。

### 外部 OpenCode(已实测,#519)

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

`opencode mcp list` 确认连接后再 `opencode run`;写操作需要该 token 有
`research.write` / `research.run`。

## 场景模板(课题收尾)

```text
1) finboard_topic_read(topic_id="RT-…", view="context")   # 接课题,看目标/开放问题
2) finboard_source_check(...)                              # 核验要引用的证据
3) finboard_topic_write(operation="append", topic_id="RT-…",
     entry_type="evidence",
     source_refs=[{kind:"research_run", ref_id:"RR-…"}], …)  # 追加轮次
4) finboard_memory_remember(memory_type="insight", tags=["research-round"], …)
```

`expected_revision` 冲突返回 `current_revision` / `changed_fields`:重新读取后续接,
**不要覆盖**。`idempotency_key` 同键同内容返回原记录(`created=false`),回执丢失
时同键重试,不要新建第二个课题。

## 网络边界

loopback 默认;非 loopback 必须 `FINBOARD_MCP_ALLOW_REMOTE=true` + TLS 反向代理。
凭据按客户端签发、可吊销;离职 / 轮换先 `revoke` 再换 token。
