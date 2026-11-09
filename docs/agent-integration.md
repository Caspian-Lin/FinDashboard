# 外部 Agent 接入(#519)

FinDashboard(缩写 `finboard`)不内置 LLM。研究 Agent 由**外部 Harness** 承担:
Codex、DeepSeek Harness、ZCode、外部 OpenCode,或任何支持 streamable-http /
stdio 的 MCP 客户端。Harness 通过 `packages/finboard-mcp` 的研究 MCP 工具工作,
FinDashboard 仍是数据 / 权限 / 任务 / 审计 / 产物的唯一事实来源。

配套文档:

- Skill(Agent 行为契约):`.agents/skills/finboard-research/SKILL.md`
- 预设(可复制客户端配置):`.agents/skills/finboard-research/presets/`
- 凭据 / 作用域 / TLS 细节:`docs/mcp-client-auth.md`
- 工具契约:`.agents/skills/finboard-research/references/tools.md`、server `_INSTRUCTIONS`

**研究域 only**:MCP 没有下单 / 撤单 / 改持仓 / Kill Switch / 连接 broker / 凭证探测
工具,永久不注册。研究写操作可自主执行(#122),但受凭据作用域约束(#517)。

## 1. 通用前置(所有客户端一致)

### 1.1 端点

| 传输 | 端点 | 说明 |
|---|---|---|
| `stdio` | `uv run python -m finboard_mcp` 由宿主作为子进程拉起 | **默认传输**(`Settings.mcp_transport` 默认 `stdio`);无 HTTP 鉴权;作用域由 `FINBOARD_MCP_STDIO_SCOPES` 决定,**默认只有 `research.read`** |
| `streamable-http` | `http://127.0.0.1:8765/mcp` | `finboard api` / `make dev` 在 `mcp_enabled`(默认 true)且有凭据时自动挂载同一端口;独立 `python -m finboard_mcp` 则必须显式 `FINBOARD_MCP_TRANSPORT=streamable-http`。必须有凭据,按客户端 Bearer |

> 外部 Harness 场景(Codex / OpenCode 等)用 `streamable-http` + 按客户端凭据;
> stdio 适合「宿主直接拉起子进程」的客户端,但默认只读,写操作需显式
> `FINBOARD_MCP_STDIO_SCOPES`。

```bash
# 独立启动 loopback HTTP(不是 make dev;不依赖 OpenCode / 交易内核)
FINBOARD_MCP_TRANSPORT=streamable-http \
FINBOARD_MCP_HOST=127.0.0.1 \
FINBOARD_MCP_PORT=8765 \
uv run python -m finboard_mcp
```

启动门禁(具名失败,退出码 2):`FINBOARD_MCP_ENABLED=false`、端口被占、
HTTP 无任何可用凭据、非 loopback 未设 `FINBOARD_MCP_ALLOW_REMOTE=true`。

### 1.2 凭据与预设

```bash
uv run finboard mcp client create --name codex --preset topic-writer   # 明文 token 只打印一次
uv run finboard mcp client list      # 只显示 prefix / scopes / state / last_used_at
uv run finboard mcp client revoke MC-xxxxxxxxxxxx
```

| 预设 | 作用域 | 用途 |
|---|---|---|
| `readonly` | `research.read` | 只读研究 / 解释 / 复核 |
| `topic-writer` | `research.read` + `research.write` + `research.run` | 日常研究 Agent(写课题 / 起运行) |
| `data-operator` | 上述 + `data.write` | 数据运维(拉取 / 发布 / 补数) |
| 自定义 | `--scopes a,b` | 例如 `research.read,audit.read`(审计客户端) |

`audit.read` 不进常规研究预设。凭据按客户端签发、可吊销、可过期;库里只存
sha256 摘要。**不要把 token 写进仓库 / 客户端配置文件 / 日志**:客户端用环境
变量注入(下面每个客户端都有示例)。

### 1.3 会话交接(客户端无关)

| 场景 | 做法 |
|---|---|
| 用户给 `RT-` 或深链 `/research/topics?topic=RT-…` | 先 `finboard_topic_read(topic_id="RT-…", view="context")`:目标版本 / 生命周期 / 开放问题 / 最近轮次 / 断链引用 / `next_actions`(≤64 KiB,截断 `excerpted=true`) |
| 只有标题 / 关键词 | `finboard_topic_read(limit=…, offset=…, view="list")` 找候选,再进 `context` |
| 续等原后台任务 `BJ-` | `finboard_job_wait(job_id="BJ-…")`;超时返回 `completed=false`,**续同一个 job**,不重新入队 |
| 引用原运行 `RR-` | `finboard_run_get(run_id="RR-…")` 读冻结输入与状态;课题内运行用 `finboard_topic_run_list(topic_id=…)` |
| 精确证据分页 | `finboard_topic_read(view="refs")` / `finboard_memory_page` / `finboard_decision_projection`(SQL 白名单投影分页) |
| 收尾 | `finboard_topic_write(operation="append", …)` 追加轮次 + `finboard_memory_remember(tags=["research-round"])`;`docs/research/` 是 canonical,工作记录不是结论 |

### 1.4 从内置工作台迁移(#520)

内置 OpenCode 托管(`opencode web` 容器、iframe 工作台、`/api/opencode` 网关、
provider 模型同步)已于 #520 删除;Web 入口改为 `/research/agent-connect`,
旧 `/research/workbench?topic=RT-…` 永久跳转并保留参数。

- **旧配置键**:`FINBOARD_OPENCODE_*` 不再生效。API 启动会打印一行
  `api.opencode_settings_retired`,列出仍存在的键名(不阻断启动);从 `.env` /
  环境变量删除即可。
- **命名卷**:`opencode-data`(会话 + `auth.json`)与 `opencode-config` **不会**
  被 FinDashboard 删除(升级 / 回退都不删)。不再使用时先只读备份:

  ```bash
  docker run --rm -v opencode-data:/d:ro -v "$PWD:/out" alpine \
    sh -c 'cp -a /d /out/opencode-data-backup'
  docker run --rm -v opencode-config:/d:ro -v "$PWD:/out" alpine \
    sh -c 'cp -a /d /out/opencode-config-backup'
  ```

- **回退**:回退到 #520 之前的版本会按旧配置重新创建容器,卷内会话仍在;本版本
  不会自动启动或删除容器/卷。
- **仓库级 OpenCode 配置**:`.opencode/opencode.json` 的 `finboard` URL 已从
  `host.docker.internal:8765` 改为 `127.0.0.1:8765`(外部 OpenCode 从仓库根启动
  即可用;token 仍经 `{env:FINBOARD_MCP_TOKEN}` 注入)。

## 2. 客户端矩阵(实测 / 待验)

| 客户端 | 状态 | 实测日期 | 证据 |
|---|---|---|---|
| Codex CLI `0.163.0` | **已验**:读 context + 追加轮次 + readonly 越权被拒 | 2026-11-10 | [`docs/research/rounds/2026-11-10-skill-519/`](research/rounds/2026-11-10-skill-519/README.md) |
| 外部 OpenCode `v2.0.32` | **已验**:同上,且跨客户端续接(读到 Codex 写入的轮次) | 2026-11-10 | 同上 |
| DeepSeek Harness | **待验** | — | 见 §5 |
| ZCode | **待验** | — | 见 §6 |

待验 ≠ 不支持:是没有做端到端实测,配置片段也不提供未经验证的字段。

## 3. Codex(已验)

### 安装 / 版本

```bash
codex --version      # codex-cli 0.163.0
```

### 配置

`~/.codex/config.toml`(全局)或 `-c` 一次性覆盖:

```toml
[mcp_servers.finboard]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "FINBOARD_MCP_TOKEN"   # 从环境变量读 Bearer,不写明文
```

```bash
codex mcp add finboard --url http://127.0.0.1:8765/mcp --bearer-token-env-var FINBOARD_MCP_TOKEN
```

### 非交互跑法(验收用)

```bash
export FINBOARD_MCP_TOKEN=<token>
codex exec --json - <<'PROMPT'
用 finboard_topic_read(topic_id="RT-…", view="context") 读课题上下文,
然后用 finboard_topic_write(operation="append") 追加一条 round 轮次并报告 entry_id。
PROMPT
```

- `--json` 输出 JSONL 事件(工具调用是 `type=mcp_tool_call`,带完整
  `structured_content` 回执),适合做验收证据。
- 需要隔离用户全局配置时用 `--ignore-user-config -c 'mcp_servers.finboard.url="…"'`
  (`--ignore-user-config` 不忽略登录态)。
- `--ephemeral` 不落会话文件。

### 排障

| 现象 | 原因 / 处理 |
|---|---|
| 工具调用失败 `MCP tool call requires approval, but approval policy is never` | Codex 客户端审批策略;非交互验收需 `--dangerously-bypass-approvals-and-sandbox`,或改交互式手工批准。与 FinBoard 无关 |
| `error: unexpected argument 'xxx' found` | 多行 prompt 不能当位置参数,改 stdin(`codex exec … -`) |
| `HTTP 401 unauthorized` | token 错 / 已吊销 / 过期;`finboard mcp client list` 看 `state` |
| 工具返回 `denied` | 凭据缺少作用域;换 `topic-writer` 客户端,不要给只读凭据加权限 |

## 4. 外部 OpenCode(已验)

### 配置

项目 `opencode.json`(注意:opencode 按**项目根**发现配置):

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

```bash
opencode mcp add finboard --url http://127.0.0.1:8765/mcp \
  --header "Authorization=Bearer {env:FINBOARD_MCP_TOKEN}"
opencode mcp list        # 看连接状态
opencode run --format json -m opencode/qwen3.8-flash "<message>"
```

### 排障

| 现象 | 原因 / 处理 |
|---|---|
| `Managed service port 45278 … already in use` | 本机另有 opencode 服务;用 `--standalone` 起私有 server(别改全局端口) |
| 会话里没有任何 `finboard_*` 工具 | 配置没被加载:确认 `opencode.json` 在**项目根**;放在子目录(尤其仓库内 `.tmp/`)不生效 |
| `provider.invalid-request … not valid JSON` | 客户端模型 / 网关抖动,换模型重试(与 FinBoard 无关) |
| `list_mcp_resources` 里看不到 finboard | 该接口只列暴露 resources 的 server,不代表未连接;直接调工具验证 |

## 5. DeepSeek Harness(待验)

**为什么待验**:本机 Harness 的 MCP 接入是**插件包**形态(profile 里是
`@local/blender-official-mcp`、`@local/godot-mcp` 之类),`~/.dsh` 下没有可直接粘贴
的"远程 MCP server"settings 片段;#519 也没有打包 finboard MCP 插件。因此
**没有**可复现的配置步骤,不宣称支持。

字段需求(与其它客户端相同,待实测确认落点):

- 传输:streamable HTTP;URL:`http://127.0.0.1:8765/mcp`
- 认证:`Authorization: Bearer $FINBOARD_MCP_TOKEN`
- 作用域:按预设(`readonly` / `topic-writer` / `data-operator`)

实测时要记录的项:① 连接是否成功;② 工具是否出现;③ `topic_read(view=context)` 回执;
④ readonly 越权 `denied`;⑤ 日志位置与格式。

## 6. ZCode(待验)

**为什么待验**:本机未安装(`Get-Command zcode` 无结果),无法实测;不提供未经验证
的配置片段。接入时需要的仍是同一组事实:streamable HTTP URL +
`Authorization: Bearer` + 作用域预设。实测记录项同 §5。

## 7. 网络边界

| 场景 | 默认 | 规则 |
|---|---|---|
| 本机 stdio | 宿主拉子进程 | 无 HTTP 鉴权;默认 `research.read`,写操作需显式 `FINBOARD_MCP_STDIO_SCOPES` |
| 本机 HTTP | `127.0.0.1:8765` | 必须有凭据(按客户端或兼容期 env token) |
| 非 loopback | **拒绝启动** | 必须 `FINBOARD_MCP_ALLOW_REMOTE=true` **且**置于 TLS 反向代理之后;MCP server 自身不终结 TLS,也不做多用户隔离 |
| 容器内客户端 | 不可用 loopback | 需 `ALLOW_REMOTE=true` + `host.docker.internal`;`#520` 退役内置容器托管后,这条只服务外部容器化 Harness |

## 8. 统一排障表

| 现象 | 排查顺序 |
|---|---|
| 连不上 / 401 | `finboard mcp client list` → `state` / `token_prefix`;确认 `FINBOARD_MCP_HOST/PORT` 与 URL 一致 |
| 401 但客户端有 token | token 是否被换行 / 引号污染;是否用了已 `revoke` 的客户端 |
| 工具返回 `denied` | 看 `error.message` 里的"已授予"列表;属预期契约,不是重试项 |
| 看不到任何 `finboard_*` 工具 | 客户端配置未加载 / MCP 初始化失败;看客户端 MCP 日志与服务端 access log |
| 服务端退出码 2 | 读 stderr 的具名原因(enabled=false / 端口占用 / 无凭据 / ALLOW_REMOTE) |
| 长任务超时 | 用 `finboard_job_wait` 有界等待,超时 `completed=false` 续同 job;不要重复入队 |

## 9. 日志与审计

- **服务端**:structlog(`mcp.tool_called` / `mcp.scope_denied`)+ uvicorn access log;
  `FINBOARD_MCP_AUDIT_PERSIST=true` 时写 `mcp_audit_events` 表,REST
  `GET /api/mcp/audit` 可查。审计行含 `caller`(client_id)、`status`、
  `error_kind`、脱敏参数摘要,不含 token / 正文。
- **研究操作审计(#518)**:`research_audit_events`,MCP `finboard_audit_query`
  只对 `audit.read` 开放。
- **客户端**:Codex `--json` 的 JSONL / `~/.codex/sessions`;OpenCode
  `--format json` 的 JSONL / `~/.local/share/opencode/log` + `opencode.db`。
  不同 Harness 的日志位置与格式不同,验收时按客户端分别记录。

## 10. 安全清单

1. token 只经环境变量注入;不进仓库、不进配置文件、不进截图。
2. 只读任务用 `readonly`;不要为了"顺手写一次"给只读凭据加作用域。
3. 验收证据里的 token 必须脱敏(`fbmc_<redacted>`),客户端 ID(`MC-…`)不是秘密。
4. 轮换:先 `revoke` 旧客户端,再签发新客户端并更新 Harness 配置。
5. 对外暴露前确认 `FINBOARD_MCP_ALLOW_REMOTE=true` 只与 TLS 反向代理同时使用。
