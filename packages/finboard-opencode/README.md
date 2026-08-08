# finboard-opencode

OpenCode 研究运行时集成 —— 会话关联(#109)、SSE 事件订阅与断线恢复、以及 **Web
研究工作台进程托管与控制面网关**(#118)。FinDashboard 是业务工具 / 权限 / 数据 /
审计 / 产物的唯一事实来源;OpenCode 是受控研究运行时,通过 `finboard-mcp` 访问研究
能力,**不连接实盘 broker / 账户 / 订单 / 持仓 / 风控**。

## 模块

| 模块 | 职责 |
| --- | --- |
| `runtime.py` | `OpenCodeRuntimeClient` —— `opencode serve` 的 async HTTP + SSE 客户端 |
| `conversation.py` | `ConversationService` —— 会话编排(创建 / SSE 续订 / 中断 / 孤儿恢复) |
| `repository.py` | `AgentConversationRepository` / `AgentEventRepository` —— 会话关联与事件持久化 |
| `schemas.py` | `ConversationRecord` / `AgentEvent` / `ConversationStatus` 领域对象 |
| `process_manager.py` | `OpenCodeProcessManager` —— `opencode web` 子进程生命周期(启动 / 停止 / 健康 / 就绪) |
| `access.py` | `AccessCredentialIssuer` —— 为已授权会话签发访问凭证 |

## Web 研究工作台网关(#118)

### 架构

```
浏览器(FinBoard Part 1 + iframe Part 2)
  │
  ├─ /api/* ──────────────► FinBoard API(唯一事实来源)
  │
  └─ iframe 跨源 ─────────► OpenCode Web(http://127.0.0.1:<port>/)
                              │ finboard-researcher agent(bash/edit/write 默认拒绝)
                              ▼
                           finboard-mcp(受控工具,只读自动 / 写操作走审批)
                              ▼
                           FinBoard service / repository
```

FinBoard 网关是 OpenCode Web 的**控制面**:
- **不透传** OpenCode 流量(浏览器 iframe 直接跨源访问 OpenCode 根 URL);
- **托管** 隔离的 `opencode web` 子进程(独立工作目录 + 严格环境白名单 + 127.0.0.1 绑定);
- **签发** 访问凭证(为已授权 `conversation` 返回 Web URL + basic auth);
- **不暴露** 未授权端口:OpenCode 由 basic auth 保护,凭证仅对 ACTIVE 会话签发。

### 进程级隔离

`OpenCodeProcessManager` 启动 `opencode web` 时:

1. **工作目录隔离**:在 `opencode_workdir`(默认 `.opencode/workspace`)运行,与
   FinBoard 仓库分离;
2. **环境变量严格白名单**:子进程只继承 `PATH` / `HOME` / `SystemRoot` 等系统必需变量,
   **绝不**继承 FinBoard 的 DB 密码 / broker 凭证 / API Key;额外变量通过
   `opencode_env_overrides`(如 LLM provider Key)显式注入;
3. **网络隔离**:强制 `127.0.0.1` 绑定,不暴露公网;
4. **basic auth**:`OPENCODE_SERVER_PASSWORD`(配置空则启动时生成强随机密码)。

### 端点契约

| 方法 | 路径 | 说明 | 关闭时 |
| --- | --- | --- | --- |
| `GET` | `/api/opencode/status` | 进程运行状态快照(脱敏,不含密码) | 503 |
| `GET` | `/api/opencode/health` | 代理健康探测 | 503 |
| `POST` | `/api/opencode/access` | 为 `conversation_id` 签发访问凭证 | 503 |

`POST /access` 流程:查 conversation → 授权校验(存在 + `ACTIVE`)→ 返回
`{web_url, username, password, opencode_session_id, agent_name}`。前端 iframe 用 basic
auth URL 嵌入:`http://<username>:<password>@<host>:<port>/`。

授权失败:`404`(会话不存在)/ `403`(非 ACTIVE 状态)。

### 会话状态机

```
ACTIVE ──interrupt──► INTERRUPTED ──resume──► ACTIVE
   │                       │
   ├──completed──► COMPLETED
   ├──failed─────► FAILED
   └──────────────► ORPHANED(OpenCode session 被外部删除 / 进程退出)
```

只有 `ACTIVE` 会话能签发访问凭证。`reconcile_orphaned` 检查中断 / 活跃会话的 OpenCode
session 是否仍存在,失联则标记 ORPHANED。

### 回滚

`opencode_web_enabled = false`(默认)→ 网关端点返回 503、不启动子进程、前端( #111)
隐藏入口。现有 `ResearchAssistant` / REST 入口与研究产物 / 审计不受影响。

### 上游版本约束

OpenCode v1.18.15 **不支持** `--base-path` 子路径部署(上游 PR #28326 审核中)。因此
本网关采用 **iframe 跨源嵌入**(非子路径反向代理)。待上游 base-path 支持合并后,可
升级为同源子路径反代,届时只需调整网关转发逻辑,上层契约不变。版本锁定:
`opencode_binary` 指向固定版本二进制;`which_opencode()` 校验可执行文件存在。

## 运行

```bash
# 方式 A:FinBoard 托管子进程(开发推荐)
FINBOARD_OPENCODE_WEB_ENABLED=true \
FINBOARD_OPENCODE_MANAGE_PROCESS=true \
FINBOARD_OPENCODE_WEB_CORS_ORIGINS=http://localhost:5173 \
uv run uvicorn finboard_api.app:app

# 方式 B:外部已启动 opencode web,FinBoard 仅连接
opencode web --port 4097 --hostname 127.0.0.1 --cors http://localhost:5173
FINBOARD_OPENCODE_WEB_ENABLED=true \
FINBOARD_OPENCODE_MANAGE_PROCESS=false \
FINBOARD_OPENCODE_WEB_PORT=4097 \
uv run uvicorn finboard_api.app:app
```
