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
| `streamable-http` | `http://127.0.0.1:8765/mcp` | 显式 `FINBOARD_MCP_TRANSPORT=streamable-http` 启动 `uv run python -m finboard_mcp`(或 `make mcp-serve-http`)。**#544 起 `make dev` 会托管这份 MCP 子进程**(`--no-mcp` 关闭,`mcp_enabled=false` / stdio 传输自动跳过);`finboard api` 仍不内嵌。必须有凭据,按客户端 Bearer |

> 外部 Harness 场景(Codex / OpenCode 等)用 `streamable-http` + 按客户端凭据;
> stdio 适合「宿主直接拉起子进程」的客户端,但默认只读,写操作需显式
> `FINBOARD_MCP_STDIO_SCOPES`。

```bash
# 独立启动 loopback HTTP(不依赖 OpenCode / 交易内核;make dev 也会托管一份)
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
sha256 摘要。**不要把 token 写进仓库 / 客户端配置文件 / 日志**。注入方式按客户端
能力选其一:①**优先**环境变量(Codex / OpenCode 等,见各自小节);②GUI 客户端
无法读环境变量时,用**收过 ACL 的本地密钥文件**(如 §5 的
`~/.dsh/secrets/finboard-mcp.token`)由客户端配置读取——文件仍在仓库之外、不进
日志、不进截图。

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

  外部 OpenCode 可自行把数据目录指向解压后的备份(或直接挂载同一命名卷)继续
  使用原会话 / auth;FinDashboard 不再代管该运行时。

- **演练状态(2026-11-10)**:legacy 配置键启动告警、无 OpenCode 配置启动、
  深链跳转与网关 404 已实测;**升级(已有容器/卷)与回退未演练**——本机 Docker
  daemon 不可用,仅由静态守卫保证代码不创建/删除容器或卷。记录见
  `docs/research/rounds/2026-11-10-retire-opencode-520/`。
- **回退(预期行为,未演练)**:回退到 #520 之前的版本会按旧配置重新创建容器,
  卷内会话预期仍在;本版本不会自动启动或删除容器/卷。未实跑,不宣称已验证。
- **仓库级 OpenCode 配置**:`.opencode/opencode.json` 的 `finboard` URL 已从
  `host.docker.internal:8765` 改为 `127.0.0.1:8765`(外部 OpenCode 从仓库根启动
  即可用;token 仍经 `{env:FINBOARD_MCP_TOKEN}` 注入)。

## 2. 客户端矩阵(实测 / 待验)

| 客户端 | 状态 | 实测日期 | 证据 |
|---|---|---|---|
| Codex CLI `0.163.0` | **已验**:读 context + 追加轮次 + readonly 越权被拒 | 2026-11-10 | [`docs/research/rounds/2026-11-10-skill-519/`](research/rounds/2026-11-10-skill-519/README.md) |
| 外部 OpenCode `v2.0.32` | **已验**:同上,且跨客户端续接(读到 Codex 写入的轮次) | 2026-11-10 | 同上 |
| DeepSeek Harness `44.7.0` | **已验(手工实测)**:工具全量发现(本次快照 156,以 `TOOL_SCOPES` 为准)+ `topic_read` / `memory_page` / `instrument_search` 真实回执 + 越权 `denied` + Skill 目录发现 | 2026-11-10 | [`docs/research/rounds/2026-11-10-dsh-542/`](research/rounds/2026-11-10-dsh-542/README.md) |
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

## 5. DeepSeek Harness(已验 2026-11-10)

DSH 的 MCP 接入是**插件条目**形态:每个 server 是 `cordis.patch.yml` 里一个
`@deepseek-ai/dsh-mcp-client` 实例(与 `@local/blender-official-mcp` 等同构),
没有"远程 MCP settings 面板"。实测版本 `44.7.0`;过程记录见
[`docs/research/rounds/2026-11-10-dsh-542/`](research/rounds/2026-11-10-dsh-542/README.md)。

### 配置

`~/.dsh/profiles/<profile>/cordis.patch.yml`(本机 GUI 用 `desktop` profile;
`web` / `headless` / `acp` 各自独立,配了才生效)。**两个条目都要**:MCP 实例 +
打开 Skill 目录扫描:

```yaml
- insert:
    - id: finboard-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: finboard
        transport: streamable-http
        url: http://127.0.0.1:8765/mcp
        # token 先读环境变量,再回落到 ACL 收权的本地密钥文件(见下)。
        headers:
          Authorization: !!js '(function(){const t=process.env.FINBOARD_MCP_TOKEN;if(t)return "Bearer "+t;const fs=process.getBuiltinModule("node:fs");const os=process.getBuiltinModule("node:os");return "Bearer "+fs.readFileSync(os.homedir()+"/.dsh/secrets/finboard-mcp.token","utf8").trim()})()'
        maxInstructionBytes: 262144   # 必设,见坑 1
        toolCallTimeoutMs: 360000     # 必须 > finboard_job_wait 的 300s 上限
        failOnStartupError: false

# Skill 目录扫描默认关闭(本机 GUI profile 实测 enabled=false;
# entry id 是 patchId `skill-filesystem`,不是 `include:skill-filesystem`)。
- id: skill-filesystem
  disabled: false
```

- **凭据**:`uv run finboard mcp client create --name dsh --preset topic-writer`
  (明文只打印一次),写进 `~/.dsh/secrets/finboard-mcp.token`。收 ACK:
  Windows `icacls <file> /inheritance:r /grant:r "<user>:(R)"`;macOS/Linux
  `chmod 600`。**不要把 token 写进 patch 文件**。
- **改完即生效**:编辑 patch 触发 HMR 重载,不需要重启 DSH(实测保存后数秒内工具可调;
  只改 mtime 不触发,要改内容)。
- **端点**:`make dev` 默认托管 MCP(issue #544;`--no-mcp` 关闭,`FINBOARD_MCP_ENABLED=false`
  或 stdio 传输自动跳过),启动日志会打印 `已同步启动 MCP 研究服务(streamable-http …)`;
  也可以单独起:`make mcp-serve-http`(或
  `FINBOARD_MCP_TRANSPORT=streamable-http FINBOARD_MCP_HOST=127.0.0.1 uv run python -m finboard_mcp`)。
  注意 `.env` 若留着 `FINBOARD_MCP_HOST=0.0.0.0`(旧容器期配置)会被启动门禁拒绝
  (退出码 2),本机应改成 `127.0.0.1`;`make dev` 里这个失败只告警、不阻断 API。

### 三个坑(都会静默失败)

1. **`maxInstructionBytes` 默认 32768 字节**:FinBoard `_INSTRUCTIONS` 当前约 43 KB。
   超限时链路是 `server/discover` 成功 → `subscriptions/listen` ack 成功 →
   **桥读取 instructions 才抛错** → 判连接失败 → 按 `reconnect.maxAttempts`
   (默认 10)退避重连,耗尽后打 `giving up after N consecutive failed reconnect
   attempts — tools unregistered`,此后必须 HMR 重载或重启 Host 才会再连。外部表现是
   `list_mcp_resources` 报 `mcp-client(finboard): server is disconnected`、一个工具都
   没有,极易误判为协议不兼容。设为 `262144` 即可。
2. **Skill 需要 `name`**:DSH 的 skill provider 要求 `SKILL.md` frontmatter 同时有
   `name` + `description`(kebab-case),缺 `name` 会被**静默丢弃**(只留一条 warn)。
   另外该 provider 默认关闭(本机 GUI profile 实测),需按上面的 `skill-filesystem`
   条目打开;它按 `<项目根>/.agents/skills`(最近 `.git` 祖先)扫描,Skill 的模型侧
   加载工具由 agent preset 提供,与本条目不冲突。
3. **工具名前缀**:模型可见名是 `mcp__finboard__finboard_topic_read` 这种
   `<命名空间>__<真实名>` 双前缀;Skill 与文档里的 `finboard_*` 是**真实注册名**,
   两者不是同一字符串。排障时按前缀名搜会话日志。

### 同步语义

| 对象 | 行为 | 证据等级 |
|---|---|---|
| 工具清单 | 连接时 `tools/list` 发现;`notifications/tools/list_changed` 触发重同步 | 代码路径(`dsh-mcp-client` 说明书);**未单独实测**动态新增工具 |
| 服务端重启 | 短时间重启(未触发桥的失败预算)实测**无需动 DSH**,下一次工具调用直接成功;若连续失败耗尽 `reconnect.maxAttempts`(默认 10),工具被注销,需 HMR 重载或重启 Host | 重启后调用实测;耗尽分支来自代码默认值,未实测 |
| Skill 正文 | Skill 就是仓库文件;provider 有 watcher,每次加载重读 `SKILL.md`,合并即生效,不用打包/重启 | 实测(加 `name` + 启用 provider 后目录立即出现) |
| 插件配置 / token | **手工**:URL、端口、`maxInstructionBytes`、轮换后的 token 都在 profile patch / 密钥文件里 | 实测 |

### 排障

| 现象 | 原因 / 处理 |
|---|---|
| `server is disconnected`,且服务端 access log 已见 200 | 先查 `maxInstructionBytes`(坑 1);DSH 没有通用运行日志,可用 loopback TCP 探针(`8766 → 8765`)看 `discover → subscriptions/listen → 立刻 close → 再 discover` 的循环 |
| 连上但 Skill 目录里没有 `finboard-research` | `name:` 缺失(坑 2)+ provider 未启用;`SKILL.md` 里不得出现 `OpenCode`(测试锁定) |
| 工具调用返回 `denied` | 凭据作用域不足,换 `topic-writer` / `data-operator` 客户端;不要给只读凭据加权限 |
| `finboard_job_wait` 在 240s 左右被切断 | `toolCallTimeoutMs` 小于 `timeout_seconds` 上限(300s),调到 360000 以上 |
| 改了 patch 但工具没变 | HMR 只对**内容**变化生效;`touch` 改 mtime 不触发,改一个字段即可 |

## 6. ZCode(待验)

**为什么待验**:本机未安装(`Get-Command zcode` 无结果),无法实测;不提供未经验证
的配置片段。接入时需要的仍是同一组事实:streamable HTTP URL +
`Authorization: Bearer` + 作用域预设。

§5 的三条经验中,**Skill `name` 是客户端格式要求、`maxInstructionBytes` 与
`mcp__<server>__<raw>` 双前缀都是 DSH 特有实现**——接入 ZCode 时按它自己的
instructions 上限、工具命名与 Skill frontmatter 契约重新核实,不要预设与 DSH 相同。
实测记录项:① 连接是否成功;② 工具是否出现(含命名前缀);③
`topic_read(view=context)` 回执;④ readonly 越权 `denied`;⑤ 日志位置与格式。

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
| 看不到任何 `finboard_*` 工具 | 客户端配置未加载 / MCP 初始化失败;看客户端 MCP 日志与服务端 access log。若服务端 access log 已有 200 响应,先查客户端侧的 instructions 上限(§5 坑 1) |
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

1. token 优先经环境变量注入;GUI 客户端读不到环境变量时用 ACL 收权的本地密钥文件
   (§5),两种方式都**不进仓库、不进截图**;密钥文件本身也不要写进 patch / 版本化配置。
2. 只读任务用 `readonly`;不要为了"顺手写一次"给只读凭据加作用域。
3. 验收证据里的 token 必须脱敏(`fbmc_<redacted>`),客户端 ID(`MC-…`)不是秘密。
4. 轮换:先 `revoke` 旧客户端,再签发新客户端并更新 Harness 配置 / 密钥文件。
5. 对外暴露前确认 `FINBOARD_MCP_ALLOW_REMOTE=true` 只与 TLS 反向代理同时使用。
