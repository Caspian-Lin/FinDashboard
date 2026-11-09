# 01 环境与服务端

## 版本

```text
codex: codex-cli 0.163.0
opencode: opencode v2.0.32
uv: uv 0.9.18 (0cee76417 2025-12-16)
python: Python 3.12.12
PostgreSQL: WSL 实例 127.0.0.1:5432(库 findashboard_wt4)
```

## 服务端启动(独立进程,不读 `.env`)

从 `.tmp/skill519/`(空目录)启动,避免被 worktree `.env` 里的
`FINBOARD_MCP_AUTH_TOKEN` / `FINBOARD_MCP_PORT=8767` 污染:

```powershell
$env:FINBOARD_MCP_ENABLED="true"
$env:FINBOARD_MCP_TRANSPORT="streamable-http"
$env:FINBOARD_MCP_HOST="127.0.0.1"
$env:FINBOARD_MCP_PORT="8781"          # 避开主目录 8765 / wt4 .env 8767
$env:FINBOARD_MCP_ALLOW_REMOTE="false"
$env:FINBOARD_DB_URL="postgresql+psycopg://findashboard:<pw>@127.0.0.1:5432/findashboard_wt4"
uv run --project C:\Users\28491\Desktop\Lab\FinDashboard-wt4 python -m finboard_mcp
```

两个启动门禁在这轮里被真实触发过,可复现:

```text
# ① 先启动、后签发凭据 → 具名拒绝(退出码 2),证明"HTTP 必须有凭据"
[finboard-mcp] 启动被拒绝:HTTP 传输没有可用凭据:FINBOARD_MCP_AUTH_TOKEN 未设置,
且 mcp_clients 表中没有未吊销/未过期的客户端。先运行 `finboard mcp client create --name <n> --preset readonly`,
或设置 FINBOARD_MCP_AUTH_TOKEN(旧共享 token,已弃用)。

# ② 签发凭据后启动成功,且 legacy_token=False(只认按客户端凭据)
2026-11-10 00:51:49 [info] mcp.http_starting host=127.0.0.1 legacy_token=False port=8781 transport=streamable-http
INFO:     Uvicorn running on http://127.0.0.1:8781 (Press CTRL+C to quit)
```

> 首轮误用 `.env` 启动时日志出现过 `FINBOARD_MCP_AUTH_TOKEN 是旧共享 token…已进入
> 弃用期` 的 deprecation warning;为让证据只反映"按客户端凭据"链路,重启时改用
> 干净目录 + 显式 env(见上)。

## 凭据签发(明文 token 已脱敏)

```bash
uv run finboard mcp client create --name codex-topic-writer --preset topic-writer
uv run finboard mcp client create --name codex-readonly --preset readonly
uv run finboard mcp client create --name opencode-topic-writer --preset topic-writer
uv run finboard mcp client create --name opencode-readonly --preset readonly
```

```text
已创建 MCP 客户端: MC-b4e2d8f4df17  name=codex-topic-writer
作用域: research.read, research.run, research.write
明文 token(仅显示一次...): fbmc_<redacted>
```

`uv run finboard mcp client list`(验收结束时):

```text
CLIENT_ID            NAME                 PREFIX            SCOPES                                        STATE
MC-b4e2d8f4df17      codex-topic-writer   <prefix>  research.read,research.run,research.write     active
  created_at=2026-11-10T00:51:19 last_used_at=2026-11-10T01:03:24
MC-308e5eccc3bd      codex-readonly       <prefix>  research.read                                 active
  created_at=2026-11-10T00:51:30 last_used_at=2026-11-10T00:57:53
MC-8e1b95d5629b      opencode-topic-writer <prefix> research.read,research.run,research.write     active
  created_at=2026-11-10T00:51:34 last_used_at=2026-11-10T01:01:10
MC-e82515fe8725      opencode-readonly    <prefix>  research.read                                 active
  created_at=2026-11-10T00:51:38 last_used_at=2026-11-10T01:02:04
```

`last_used_at` 只会在认证成功时更新,是"这次调用确实来自该客户端"的旁证
(4 个客户端各有独立时间戳,失效 / 错误 token 不会推进)。

## 课题 fixture

- 课题:`RT-9dac7584b738450bb1ac42f1`,`status=active`,`revision=1`,目标 `v1`
  (source=`document`,ref_id=`docs/research/ROADMAP.md`)
- 通过 MCP 建题(同一 HTTP 通道 + topic-writer 凭据),`idempotency_key=skill-519-seed`
- 验收结束时 `finboard_topic_read(entries=true)` 返回 3 条 entry:
  `goal`(建题)+ `round`(Codex,`RE-391bab58c76648a581ed0c6b`)+
  `round`(OpenCode,`RE-dacddc0cc903469faac1c34d`),`has_more=false`。
  readonly 的两次越权 append **没有**产生任何 entry。

## 收尾(产物留在哪里)

- 4 个客户端凭据**保留**在 wt4 开发库 `mcp_clients` 里(topic-writer ×2 / readonly ×2),
  便于复跑同一场景;明文 token 只在 gitignore 的 `.tmp/skill519/` 与进程环境里,
  未进仓库。要清理:`uv run finboard mcp client revoke <CLIENT_ID>`。
- 课题 `RT-9dac7584b738450bb1ac42f1` 与 2 条轮次留在 `research_topics` /
  `research_topic_entries`(dev 库),是**工作记录**,不是 canonical 结论。
- 本轮未产生 `RR-` / `BJ-`(wt4 开发库 `research_runs` 为空),所以 `BJ-`/`RR-`
  续接只验证了协议字段与文档契约,没有真实回执 —— 见 `README.md` §未覆盖。

