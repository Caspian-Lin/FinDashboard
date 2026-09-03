# OpenCode agent 容器 JSON 工具衍生镜像(issue #313)

## 背景

OpenCode 研究容器(官方 `ghcr.io/anomalyco/opencode`,#118 托管)内无
python/jq、也无 node 运行时,研究 agent 排查 MCP 工具返回的 JSON 只能用
`rg` 硬抠,效率低。agent 权限明令禁止安装软件、挂载全部 `:ro`,容器自身
无法可写地装工具 —— 解法是**构建官方镜像的衍生镜像**(只加装 jq +
python3),经既有 settings `opencode_image` 零代码接入。

## 镜像内容与边界

`docker/opencode-agent/Dockerfile`:

- base:官方 `ghcr.io/anomalyco/opencode`(实测 **Alpine Linux 3.24 +
  apk**,musl libc,x86_64/arm64 双架构);
- 加装:`jq` + `python3`(Alpine 包名;Debian 系的 `python3-minimal` 在
  Alpine 仓库不存在)。apk 版本随 Alpine v3.24 分支仓库解析,不精确 pin
  `-rN`(分支内 point 版本滚动、旧版会被裁剪,精确 pin 会让后续重建失败);
  整体可复现性由 base 镜像 pin 承载;
- **不改** ENTRYPOINT / CMD / USER / ENV —— 官方镜像 `ENTRYPOINT ["opencode"]`
  是 FinBoard 容器命令 `docker run <image> web --hostname 0.0.0.0 ...`
  (`OpenCodeProcessManager.build_docker_run_command`)的依赖,必须原样继承;
- **不触及**:挂载边界(`.opencode` / `.agents` / `docs/research` 一律
  `:ro` + named volume)、容器 env 白名单、#242/#248 模型同步逻辑
  (runtime opencode.json 渲染与镜像内容无关,base 替换对其透明);
- `docker/research-sandbox` 镜像(python:3.12-slim 基,#216)完全独立,
  不受影响。

镜像只在本地 / CI 构建并 smoke,**不推 registry**。

## 构建

```bash
# 推荐(Makefile,#313 新增目标):
make opencode-agent-image            # 构建 finboard-opencode-agent:1.18.15
make opencode-agent-smoke            # jq/python3 版本 + ENTRYPOINT 完好

# 等价手工命令(构建上下文 = docker/opencode-agent/ 自身,勿用仓库根):
docker build -f docker/opencode-agent/Dockerfile \
  -t finboard-opencode-agent:1.18.15 docker/opencode-agent/
```

## 启用(settings 切换,零代码)

FinBoard 托管容器时,把 settings `opencode_image` 指向衍生镜像即可
(pydantic env 前缀 `FINBOARD_`,即 `.env` 或环境变量):

```bash
FINBOARD_OPENCODE_IMAGE=finboard-opencode-agent:1.18.15
```

容器是启动时按 `opencode_image` 创建的 —— 切换镜像后须让旧容器重建:

```bash
docker rm -f finboard-opencode-web   # FinBoard 下次启动会用新镜像重建
# 确认:
docker inspect finboard-opencode-web --format '{{.Config.Image}}'
#   → finboard-opencode-agent:1.18.15
```

然后照常以 Web 容器模式启动 FinBoard(`FINBOARD_OPENCODE_WEB_ENABLED=true
FINBOARD_OPENCODE_MANAGE_PROCESS=true ...`,完整命令见 README「OpenCode 研究
运行时」一节)。

## 版本锁定与升级

- **现状 pin**:Dockerfile `ARG BASE_IMAGE=ghcr.io/anomalyco/opencode:1.18.15`
  (版本 tag;发布 #313 时官方 `latest` 即指向 1.18.15,index digest
  `sha256:59b2582fb5a10b7022d8b3347a9d9c60710d526ad6bfd02eb17a2c8582b35809`)。
- **digest 锁定(更强,可选)**:tag 理论上可被官方重新 push;要不可漂移,
  用多架构 index digest 固定:

  ```bash
  docker buildx imagetools inspect ghcr.io/anomalyco/opencode:1.18.15
  # 取 Digest: sha256:<index-digest>(index 覆盖 amd64/arm64,仍可多架构构建)
  ```

  然后把 `BASE_IMAGE` 改为
  `ghcr.io/anomalyco/opencode:1.18.15@sha256:<index-digest>`(tag + digest
  并存,可读性与不可漂移兼得)。
- **升级流程**(官方发新版时):
  1. 改 Dockerfile `ARG BASE_IMAGE` 到新版本 tag(如需强锁定,按上式补
     digest);
  2. `make opencode-agent-image && make opencode-agent-smoke`;
  3. 同步 bump Makefile `OPENCODE_AGENT_IMAGE ?= finboard-opencode-agent:<新版本>`
     与 `.env` 的 `FINBOARD_OPENCODE_IMAGE`;
  4. `docker rm -f finboard-opencode-web` 重建容器;
  5. 跑下面的「人工回归步骤」,通过后提交(`chore: bump opencode agent base image (#313)` 风格)。

## 人工回归步骤(opencode web 全功能)

MCP 连接、会话、iframe 回归依赖主目录运行时(OpenCode 全局锚定主目录,
worktree 的 `.env` 已关闭 OpenCode),在**主目录**按以下步骤验证:

1. 构建并 smoke:`make opencode-agent-image && make opencode-agent-smoke`;
2. 主目录 `.env` 设 `FINBOARD_OPENCODE_IMAGE=finboard-opencode-agent:1.18.15`,
   `docker rm -f finboard-opencode-web`;
3. 以 Web 容器模式启动 FinBoard(`make dev` 或 README 中的 uvicorn 命令);
4. **容器与镜像**:启动日志无 `opencode_*` WARNING;
   `docker inspect finboard-opencode-web --format '{{.Config.Image}}'` 显示
   衍生镜像;
5. **MCP 连接**:`GET /api/opencode/status` 显示容器 running 且内嵌
   finboard-mcp running;前端 `/research/workbench` 顶部 banner 的 FinBoard
   MCP 状态正常(非 #250 的 `failed`);
6. **会话**:工作台 Tab(iframe)打开 opencode web UI,新建会话发一条消息,
   agent 能正常列出/调用 `finboard_*` 工具(说明 #250 启动顺序约束未被破坏);
   在会话内让 agent 执行 `jq --version && python3 --version`(bash 权限 #182
   只读轮询语义内)确认容器内工具可用;
7. **会话持久化**:重启 FinBoard(容器重建)后会话历史仍在(named volume
   `opencode-data` 不受镜像切换影响);
8. **iframe**:前端 iframe 正常跨源加载,「新窗口打开」可用。

## 回滚

settings 换回官方镜像名即回滚,零代码改动:

```bash
FINBOARD_OPENCODE_IMAGE=ghcr.io/anomalyco/opencode:latest   # 或删除该覆盖
docker rm -f finboard-opencode-web                           # 下次启动重建
```

## CI

`.github/workflows/ci.yml` 的 `opencode-agent-image` job(照 `sandbox-image`
先例):构建镜像 + smoke(`--version` 走默认 ENTRYPOINT 验证 opencode 入口
未被破坏;`jq --version` + `python3 --version` 验证工具),不推 registry。
