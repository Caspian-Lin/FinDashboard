# finboard-opencode

OpenCode 研究运行时集成 —— **Web 研究工作台进程托管与控制面网关**(#118 / 重构 #121)。
FinDashboard 是业务工具 / 权限 / 数据 / 审计 / 产物的唯一事实来源;OpenCode 是受控研究
运行时,通过 `finboard-mcp` 访问研究能力,**不连接实盘 broker / 账户 / 订单 / 持仓 / 风控**。

> **#121 重构**:FinBoard 不再维护独立研究会话投影层(`agent_conversations` /
> `agent_events` 表已删除)。OpenCode 自身管理会话 / 历史 / 恢复;FinBoard 只负责隔离
> 实例的进程托管与访问凭证签发。

## 模块

| 模块 | 职责 |
| --- | --- |
| `runtime.py` | `OpenCodeRuntimeClient` —— `opencode` 的 async HTTP + SSE 客户端 |
| `process_manager.py` | `OpenCodeProcessManager` —— `opencode web` Docker 容器生命周期(启动 / 停止 / 健康 / 就绪) |
| `access.py` | `AccessCredentialIssuer` —— 签发访问信息(网关启用即签发明文 `web_url`,#157 无凭证;#121 无 conversation 绑定) |

## Web 研究工作台网关(#118)

### 架构

```
浏览器(iframe 直连)
  │
  ├─ /api/opencode/* ────► FinBoard API(控制面:状态/健康/凭证签发)
  │
  └─ iframe 跨源 ─────────► OpenCode Web(http://127.0.0.1:<port>/)
                              │ finboard-researcher agent(bash/edit/write 默认拒绝)
                              ▼
                           finboard-mcp(受控工具,研究写操作自主执行 #122)
                              ▼
                           FinBoard service / repository
```

FinBoard 网关是 OpenCode Web 的**控制面**:
- **不透传** OpenCode 流量(浏览器 iframe 直接跨源访问 OpenCode 根 URL);
- **托管** 隔离的 `opencode web` Docker 容器(版本锁定镜像 + 严格环境白名单 + 127.0.0.1 绑定);
- **签发** 访问信息(网关启用即签发明文 `web_url`,#157 移除 basic auth;#121 重构后不绑定 conversation);
- **不暴露** 未授权端口:宿主机侧 `127.0.0.1` 绑定是唯一网络边界(单用户模型)。

### 容器级隔离

`OpenCodeProcessManager` 启动 `opencode web` 时:

1. **版本锁定镜像**:`ghcr.io/anomalyco/opencode`,不继承 FinBoard 的 DB 密码 / broker 凭证 / API Key;
2. **环境变量严格白名单**:`-e` 只注入 LLM key / MCP token 与 HOME / XDG 目录语义,FinBoard 自身凭证不进容器;
3. **网络隔离**:强制宿主机侧 `127.0.0.1` 绑定,不暴露公网;
4. **会话数据持久化**:命名卷 `opencode-data` → `/root/.local/share/opencode`(会话 DB)、
   `opencode-config` → `/root/.config/opencode`(auth),容器重建不丢数据。容器注入
   `HOME=/workspace` 与 `XDG_*_HOME` 钉死数据落点:opencode 会在 XDG 数据根下追加
   `opencode/` 子目录,因此 XDG 值指向 volume 挂载点的**父目录**,最终落点正好是
   挂载点;`HOME=/workspace` 让文件选择器 / 打开项目对话框直接落在工作目录;
5. **容器内 MCP 访问**:通过 `host.docker.internal:8765` 跨网络访问宿主机 `finboard_mcp`(streamable-http + Bearer 鉴权)。

### 端点契约

| 方法 | 路径 | 说明 | 关闭时 |
| --- | --- | --- | --- |
| `GET` | `/api/opencode/status` | 进程运行状态快照(脱敏,不含密码) | 503 |
| `GET` | `/api/opencode/health` | 代理健康探测 | 503 |
| `POST` | `/api/opencode/access` | 签发访问信息(网关启用即签发) | 503 |

`POST /access`(#121 重构后不绑定 conversation;#157 移除 basic auth):网关启用即返回
`{web_url, agent_name}`(明文 URL,无凭证字段)。前端 iframe 与「新窗口打开」共用
同一 `web_url`。

### 会话历史与恢复(#112)

- **会话所有权**:OpenCode 自身管理会话 / 历史 / 恢复(#121 删除 FinBoard 侧会话
  投影层),会话 DB 持久化在命名卷 `opencode-data`,容器重建后可恢复历史会话。
- **回放原语**(`OpenCodeRuntimeClient`,供审计 / 断线续传场景):
  - `GET /session/{id}/history?after=<seq>` —— 从游标续取会话历史;
  - `GET /session/{id}/event`(SSE) —— durable 事件流,`after_seq` 游标重放已完成
    事件后继续推送,调用方持久化 `seq` 作为断线续传游标。
- **工作区状态在浏览器侧**:OpenCode Web 的项目列表 / 最近会话存于浏览器
  IndexedDB(按 origin + iframe 分区隔离),服务端无法预置。首次打开(iframe 与新
  窗口各自独立)显示空白属正常:点击「添加项目」→ 搜索框输入 `/` → 点击 `~`
  (主目录,即容器内 `/workspace`)即可恢复历史会话;打开一次后浏览器自动记住。
  容器注入的 `HOME=/workspace` 使文件选择器直接从工作目录开始,`~` 直达 `/workspace`。

### 回滚

`opencode_web_enabled = false`(默认)→ 网关端点返回 503、不启动容器、前端(#111)
隐藏入口。现有 REST 入口与研究产物 / 审计不受影响。

### 上游版本约束

OpenCode v1.18.15 **不支持** `--base-path` 子路径部署(上游 PR #28326 审核中)。因此
本网关采用 **iframe 跨源嵌入**(非子路径反向代理)。待上游 base-path 支持合并后,可
升级为同源子路径反代,届时只需调整网关转发逻辑,上层契约不变。

## 运行

```bash
# 方式 A:FinBoard 托管 Docker 容器(开发推荐)
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
