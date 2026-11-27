# 预设:data-operator(`data.write`)

**用途**:数据运维客户端 —— 研究写能力之上再加**全局数据操作**:拉取 / 同步标的
池 / 批量下载 / 数据集发布 / 质量修复 / ETF 元数据 / 补数请求。只在需要修数据链路
时签发,日常研究不要用。

```bash
uv run finboard mcp client create --name ops-data --preset data-operator
# 明确数据作用域边界
uv run finboard mcp client create --name ops-audit --scopes research.read,audit.read
```

| 项 | 值 |
|---|---|
| 作用域 | `research.read` + `research.write` + `research.run` + `data.write` |
| 额外能力 | `finboard_data_fetch` / `finboard_data_sync_universe` / `finboard_data_bulk_download_start` / `finboard_dataset_release_publish` / `finboard_data_quality_repair` / `finboard_etf_*` / `finboard_data_backfill_submit` / `finboard_data_backfill_cancel` |
| 仍不可用 | 实盘能力(下单 / 撤单 / 持仓 / Kill Switch / broker / 凭证探测)永久不在工具集 |

> ⚠️ **只读预设不得发起运行**;本预设是「数据运维」专用,权限更大但**不改变
> 研究域隔离**:数据写只产生新发布 / 新批次,不写实盘 `orders` / `fills` /
> `positions`,也不自动启动研究运行或模拟盘。
>
> 补数(#511)只产生新发布:先 `finboard_data_backfill_preview` 预检,
> 提交时回填 `expected_plan_checksum`;plan 漂移返回 `conflict`
> (`backfill_plan_drift`)时**重新预览**,不要换个参数硬重试。已发布 release
> 不可变。

## 客户端配置

```bash
export FINBOARD_MCP_URL=http://127.0.0.1:8765/mcp
export FINBOARD_MCP_TOKEN=<data-operator 客户端的 token>
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
  "用 finboard_data_catalog_list 列出最近发布,再对缺口做一次 backfill preview(不要提交)。"
```

### DeepSeek Harness / ZCode(待验)

- **DeepSeek Harness**:MCP 以插件包形式挂载,本机无可粘贴的 settings 片段,
  #519 未打包插件 → **待验**。
- **ZCode**:本机未安装 → **待验**。
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

## 运维检查清单

1. `uv run finboard mcp client list` 核对 `state` / `token_prefix` / `last_used_at`。
2. 数据写任务长跑:入队拿 `job_id`,统一 `finboard_job_wait` 有界等待。
3. 补数 / 发布 / 修复都**只追加**,历史 release 不改写。
4. 轮换:先 `revoke` 旧客户端,再签发新客户端并更新客户端配置。
5. 不要把 `data.write` 凭据交给只做研究分析的 Agent。

## 网络边界

loopback 默认;非 loopback 必须 `FINBOARD_MCP_ALLOW_REMOTE=true` + TLS 反向代理,
并限制来源网络 —— `data.write` 是对全局数据链路影响最大的作用域。
