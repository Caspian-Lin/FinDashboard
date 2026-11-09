# 02 Codex(已实测)

`codex-cli 0.163.0`,Windows。凭据从环境变量注入,**token 不落配置文件、不进命令行**。

## 配置

正式接法(`~/.codex/config.toml`):

```toml
[mcp_servers.finboard]
url = "http://127.0.0.1:8781/mcp"
bearer_token_env_var = "FINBOARD_MCP_TOKEN"
```

`url` + `bearer_token_env_var` 是 CLI 自己文档化的字段
(`codex mcp add <NAME> --url <URL> --bearer-token-env-var <ENV_VAR>`)。

验收时**不改用户的全局配置**,而是用 `--ignore-user-config` + `-c` 覆盖,把配置限定
在本次进程内(等价于上面的 toml,便于复现且零副作用):

```powershell
$env:FINBOARD_MCP_TOKEN = "<topic-writer token>"   # 明文只在内存里
Get-Content prompt-codex-writer.txt -Raw | codex exec `
  --ignore-user-config --ephemeral --skip-git-repo-check `
  --dangerously-bypass-approvals-and-sandbox `
  -C .tmp\skill519 `
  -c 'mcp_servers.finboard.url="http://127.0.0.1:8781/mcp"' `
  -c 'mcp_servers.finboard.bearer_token_env_var="FINBOARD_MCP_TOKEN"' `
  --json -
```

## 结果

### A. topic-writer:读 context + 追加轮次

```text
finboard_topic_read(topic_id="RT-9dac7584b738450bb1ac42f1", view="context")
  → status=ok;课题 active,goal.version=v1,最近轮次 0
finboard_topic_write(operation="append", ..., idempotency_key="skill-519-codex-round-1")
  → status=ok
    entry_id=RE-391bab58c76648a581ed0c6b
    checksum=bd865f31355b805d6abb7e0266245f0bd7b62225781c76a8e32e7fdf1fc5bc54
    evidence_level=agent_interpretation
```

Codex 自己的总结(原样):

> 读取回执 status=ok;课题状态 active,goal.version=v1,最近轮次数量为 0。
> 追加回执 status=ok;两次调用均未出现 denied。
> 追加轮次 entry_id:RE-391bab58c76648a581ed0c6b。

完整事件流:`logs/codex-writer.jsonl`(`item.completed` 里的 `mcp_tool_call`
带完整 `structured_content`)。

### B. readonly:读成功、写被拒

```text
finboard_topic_read(...)   → status=ok
finboard_topic_write(append, idempotency_key="skill-519-readonly-should-fail")
  → status=denied
    error.kind=permission_denied
    error.message=客户端缺少所需作用域:research.write(已授予:research.read)
    retryable=false
```

Codex 总结(原样):

> 读回执:status=ok,只读工具可用。
> 写回执:status=denied,error.kind=permission_denied。
> 服务端已拒绝写入:凭据仅有 research.read,缺少 research.write。

完整事件流:`logs/codex-readonly.jsonl`。

## 踩坑(Know-how)

1. **多行 prompt 不能直接当参数**。`codex exec "<多行中文字符串>"` 会被 node 侧的
   参数解析拆开,报 `error: unexpected argument '客户端通过' found`。改成
   **stdin**:`... codex exec ... --json -` 并把 prompt 从文件管道进去。
2. **`approval_policy="never"` 会把 MCP 工具调用一并挡掉**:

   ```text
   {"item":{"type":"mcp_tool_call",...,"error":{"message":"MCP tool call requires approval, but approval policy is never"},"status":"failed"}}
   ```

   只加 `-c 'mcp_servers.finboard.default_tools_approval_mode="auto"'` **不够**
   (该字段被 `--strict-config` 接受,但本版本仍要求审批)。非交互验收必须显式
   `--dangerously-bypass-approvals-and-sandbox`(或改用可交互 TUI 手工批准)。
   这是 **Codex 客户端侧**行为,与 FinBoard 服务端作用域无关 —— 绕过审批后
   readonly 仍然被服务端拒绝,正好证明"服务端才是强制点"。
3. **`--ignore-user-config` 不忽略 auth**:`CODEX_HOME` 里的登录态照常使用,所以可以
   在不动用户 `config.toml` 的前提下做隔离验收。
4. **`--ephemeral`** 不落会话文件;需要事后审计会话时去掉它,会话在
   `~/.codex/sessions/`(`--json` 的 stdout 只是实时事件流)。

## 日志可用性

- **机器可读**:`codex exec --json` 输出 JSONL 到 stdout;工具调用是
  `item.started` / `item.completed` 的 `type=mcp_tool_call`,带完整
  `arguments` 与 `structured_content`(即 FinBoard `ToolEnvelope`)。本轮证据就是
  这条通道。
- **人类可读**:默认 TUI 输出;`-o/--output-last-message <FILE>` 可把最后一条
  回答单独存文件。
- **stderr 噪音**:全局 `config.toml` 里其它 MCP server(如 exa / consensus)的连接
  错误会混进 stderr;`--ignore-user-config` 下仍可能看到。裁剪证据时已剔除。
