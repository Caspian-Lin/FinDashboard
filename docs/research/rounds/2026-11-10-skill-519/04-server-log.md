# 04 服务端日志摘录(越权拒绝与审计)

来源:worktree 的 `.tmp/skill519/mcp-8781.log`(structlog + uvicorn access log)。

## 启动(只认按客户端凭据)

```text
2026-11-10 00:51:49 [info] mcp.http_starting  host=127.0.0.1 legacy_token=False port=8781 transport=streamable-http
2026-11-10 00:51:49 [info] mcp.starting       audit_persist=False default_identity=None readonly_only=False transport=streamable-http
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8781 (Press CTRL+C to quit)
```

`legacy_token=False` = 本次没有 `FINBOARD_MCP_AUTH_TOKEN`,令牌全部来自
`mcp_clients`。

## readonly 越权被拒(OpenCode 客户端)

```text
2026-11-10 01:02:57 [info] mcp.tool_called
  arguments={'operation': 'append', 'payload': "{'entry_type': 'round', 'goal_version': 'v1',
  'objective': 'readonly 越权写入测试(OpenCode)', ...",
  'topic_id': 'RT-9dac7584b738450bb1ac42f1',
  'idempotency_key': 'skill-519-opencode-readonly-should-fail'}
  caller=MC-e82515fe8725 error_kind=permission_denied latency_ms=0
  operation_id=OP-8fb1004e505a status=denied tool=finboard.workspace.write

WARNING  mcp.scope_denied  execution.py:213
  tool_name=finboard.workspace.write
  caller=MC-e82515fe8725
  detail=客户端缺少所需作用域:research.write(已授予:research.read)
```

要点:

- **审计行能定位到具体客户端**(`caller=MC-e82515fe8725` = `opencode-readonly`),
  且 `status=denied` / `error_kind=permission_denied` / `latency_ms=0`(handler 未
  执行,零业务耗时)。
- 这里的 `tool=finboard.workspace.write` 是**内部 dotted 操作名**:MCP 注册名是
  `finboard_topic_write`,`run_tool` 经别名表映射(见
  `finboard_mcp/scopes.py::_TOOL_NAME_ALIASES`)。所以文档里"工具名用
  `finboard_*`、审计名可用 dotted"不是笔误 —— 两者本来就不是一套命名。
- 同一轮 Codex 的 readonly 越权在 00:58 已产生一致的行(`caller=MC-308e5eccc3bd`
  = `codex-readonly`),两次都只有 WARNING + 审计,没有任何写入副作用。

## 成功写入(对照)

```text
POST /mcp HTTP/1.1 200 OK      # Codex append  → RE-391bab58c76648a581ed0c6b
POST /mcp HTTP/1.1 200 OK      # OpenCode append → RE-dacddc0cc903469faac1c34d
```

审计与访问日志只记录工具名 / caller / 参数脱敏摘要(`payload` 被截断),不含
token、也不含完整正文 —— 与 #518「只存脱敏摘要」一致。

## 分页调用（只读客户端，2026-11-10 01:44）

同一 `MC-308e5eccc3bd`（`codex-readonly`）对 `finboard.workspace.read` 连发三次
`entries=true, limit=1, offset=0/1/2`，`has_more` 依次 true/true/false。服务端访问
日志可对上 `caller` 与 `OP-` 组合，证明分页确实走服务端（不是客户端缓存）：

```text
01:44:29 OP-5796f246f9c5 caller=MC-308e5eccc3bd offset=0 status=ok
01:44:32 OP-fbc4a6ff614f caller=MC-308e5eccc3bd offset=1 status=ok
01:44:35 OP-5cfe2b483489 caller=MC-308e5eccc3bd offset=2 status=ok
```
