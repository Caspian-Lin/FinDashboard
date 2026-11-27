# #520 退役内置 OpenCode:演练记录(2026-11-10)

本目录记录 issue #520「退役内置 OpenCode 托管与 iframe」**实际执行**的演练与
**未执行**项。结论先行:代码删除完整、缺席/深链/网关路由已实测;**升级(已有
容器/卷)与回退未演练** —— 本机 Docker daemon 不可用,不能把「升级不删卷」「回退
重建容器」当作已验证事实。文档与 PR 描述按此如实标注。

- 分支:`refactor/retire-opencode-520`(基于 #519;未 rebase)
- 提交:`67adaa9` / `fdd6ef2` / `bab5d8b` / `39718f5` / `7b36b14`(+ 本修复提交)
- 环境:Windows / Python 3.12 / uv / PostgreSQL(WSL);Docker CLI 存在但 daemon
  未运行(`docker version` → `npipe://./pipe/dockerDesktopLinuxEngine` 不存在)

## 结果总表

| 演练 | 状态 | 证据 / 原因 |
|---|---|---|
| 已有 legacy `FINBOARD_OPENCODE_*` 配置时升级启动 | **已演练(无容器/卷场景)** | 真实 lifespan 启动成功;打印一行 `api.opencode_settings_retired` 并列出 16 个遗留键;不崩溃、不创建 runtime |
| OpenCode 完全缺席(无配置、无 Docker) | **已演练** | `tests/integration/test_issue_520_opencode_retirement.py` 在禁止任何子进程(等价无 Docker)下启动,`/api/health`、`/api/research/runs`、`/api/research/topics` 均 200 |
| 旧深链 `/research/workbench?topic=…` | **已演练** | `AgentConnect.test.tsx`:跳转后保留 `?topic=`,新页显示 `RT-…` 与 `finboard_topic_read(view="context")` |
| 旧网关删除 | **已演练** | 同一集成测试断言 `/api/opencode/status` → 404 |
| 已有旧容器 + 命名卷时升级 | **未演练** | Docker daemon 不可用,无法创建/验证旧容器与卷 |
| 回退到 #520 之前版本 | **未演练** | 需 Docker + 旧版本 checkout/镜像;本机无法执行 |
| 卷只读导出备份命令 | **未执行** | 同上;命令仅文档化 |

## 实际执行记录

### 1. legacy 配置启动(真实 lifespan)

```text
$env:FINBOARD_OPENCODE_WEB_ENABLED="true"
$env:FINBOARD_OPENCODE_IMAGE="ghcr.io/anomalyco/opencode:1.18.15"
uv run python -  # create_app(Settings()) + app.router.lifespan_context
```

输出(节选,原样):

```text
WARNING:finboard_api.app [warning] api.opencode_settings_retired [finboard_api.app]
keys=['FINBOARD_OPENCODE_BASE_URL', ..., 'FINBOARD_OPENCODE_WEB_USERNAME',
'FINBOARD_OPENCODE_WORKDIR'] migration=docs/agent-integration.md removed_in=#520
DRILL_STARTED_WITH_LEGACY_ENV
HAS_OPENCODE_STATE False
ROUTES_WITH_OPENCODE []
```

说明:`.env` 中遗留键也被扫描并列出(只列键名,不输出键值);启动继续,
`app.state` 无 `opencode_runtime`,路由表无 `/api/opencode`。

### 2. 无 OpenCode 配置 + 无 Docker

`Settings(_env_file=None, …)`(无任何 `opencode_*` 字段)构建 app;测试将
`asyncio.create_subprocess_exec` / `subprocess.run` 替换为直接抛
`AssertionError`(等价「机器上没有 Docker / 不能起任何进程」),lifespan 正常
进入并服务研究端点,`/api/opencode/status` 返回 404。

### 3. 深链与导航

`LegacyWorkbenchRedirect` 将整个 query string 带到 `/research/agent-connect`;
组件测试断言「回到课题」链接为 `/research/topics?topic=RT-abc123`;`navGroups`
不含 `/research/workbench`。

### 4. 静态守卫(不等于真实卷演练)

- `tests/unit/test_issue_520_opencode_retirement.py`:包/前端/build 面不得残留
  已退役标识符;代码不得出现 `docker volume rm` / `volume prune`。
- 这些守卫证明**代码路径**不会删除卷、不会再起容器;不能替代「已有卷升级/回退」
  的真实 Docker 演练。

## 未执行项的可复现步骤(供有 Docker 的环境补跑)

1. 旧容器/卷升级:在 #520 之前的版本启动 `FINBOARD_OPENCODE_WEB_ENABLED=true`,
   确认 `docker volume ls` 有 `opencode-data` / `opencode-config` 且容器 running;
   切到本分支,确认 API 启动只打弃用警告、`docker ps` 中旧容器**不被本版本删除**,
   旧卷仍在;备份命令可执行并产出目录。
2. 回退:切回 #520 之前的版本,按旧配置启动,确认容器重建且卷内会话仍在。
3. 记录 `docker volume inspect` 与 `docker inspect` 输出后,把结论补进本文件并
   更新 `docs/dev-guide.md` / `docs/agent-integration.md` 的「未演练」标注。

## 结论

- **已验**:legacy 配置启动告警、无 OpenCode/无 Docker 启动、研究端点可用、
  深链保参、网关 404、代码不删卷/不起容器。
- **未验**:已有容器+卷的升级、回退、真实卷备份。原因均为本机 Docker daemon
  不可用;不得在 PR/文档中写成已完成。
