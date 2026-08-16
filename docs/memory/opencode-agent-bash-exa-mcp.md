# OpenCode finboard-researcher:bash 放行 + exa 搜索 MCP(#182)

## 主题
2026-08-16 按用户要求给 `finboard-researcher` 放行 `bash`(只读轮询用途)并接入
exa 搜索 MCP,配套把容器项目配置挂载补成 `:ro`。issue #182。

## 结论 / 事实
- **OpenCode 容器镜像无 JS 运行时**:`ghcr.io/anomalyco/opencode` 是极简 busybox
  镜像,容器内**没有 node / bun / deno / python**(实测 `which node npx` 为空,
  python3 也没有,只有 wget)。因此 `type: "local"` MCP(如 `npx @exa-labs/exa-mcp-server`)
  在 web 容器模式下**跑不起来** —— 外部搜索 MCP 只能用 `type: "remote"`。
- **exa 官方托管端点** `https://mcp.exa.ai/mcp`(streamable-http,`exa-search-server`
  v3.2.1)匿名可用、带限速;已验证容器可出网访问(wget 返回 405 说明端点可达)。
  API key 注入方式:URL `?exaApiKey=...` 或 header `x-api-key`(README 未提
  EXA_API_KEY 环境变量)。若走 header,key 需先进容器环境(经 `opencode_env_overrides`,
  见 config.py 的 `OPENCODE_ENV_OVERRIDES` 逗号分隔 KEY=VAL)。
- **OpenCode MCP 工具命名**:`<server>_<tool>` 前缀式(文档原文:"MCP server tools
  are registered with server name as prefix"),permission 里用 `exa_*` / `finboard_*`
  匹配;agent permission 是 allowlist,`"*": "deny"` 下不加显式规则工具会被拒。
- **`.opencode` / `.agents` 挂载此前无 `:ro`**:process_manager.py 的注释一直声称
  "容器只读这些项目级配置",但 `-v` 实际是 rw。放行 bash 后这会成为自提权路径
  (agent 可改 opencode.json / agent md / skill 文件),已补 `:ro`(#182)。
- 容器配置改动(agent permission / MCP 注册)要 **重启容器** 才生效(opencode 启动
  时读配置);会话历史在 named volume 不丢。

## Why
用户需要 agent 轮询任务进度/HTTP 端点(如 wget/sleep 循环)与外部资料搜索;
原有 #157 纯只读 allowlist 无法满足。容器无 JS 运行时这一事实决定了 local MCP
不可行,只能 remote 注册。`:ro` 是放行 bash 的前提,否则 agent 能改写仓库配置。

## How to apply
- 给该 agent 加搜索能力:在 `.opencode/opencode.json` 的 `mcp` 注册 remote 服务
  (参考 exa 条目),在 `.opencode/agent/finboard-researcher.md` 与 opencode.json
  的 `agent.permission` 同时加 `<server>_*: allow`(两个文件都要改,md 是 agent
  定义,json 是运行时渲染源);`.opencode/runtime/opencode.json` 是 gitignore 的
  渲染产物,下次容器 start 会从源自动重生成。
- 容器内无 node,任何 local MCP 都不可行 —— 新 MCP 一律按 remote 端点设计。
- 外部服务 key 进容器必须经 `OPENCODE_ENV_OVERRIDES`(FinBoard 自身凭证仍不进
  容器);配好后如用 header 注入再在 MCP 配置写 `"x-api-key": "{env:EXA_API_KEY}"`。
- 改完 agent 权限记得重启 `finboard-opencode-web` 容器并查日志确认 MCP 连接成功。
