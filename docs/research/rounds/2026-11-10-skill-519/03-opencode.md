# 03 外部 OpenCode(已实测)

`opencode v2.0.32`,Windows,独立于 FinBoard 内置托管容器(那是 #520 要退役的
`finboard-opencode`)。

## 配置

项目级 `opencode.json`(放在 scratch 工程根目录;`headers` 从环境变量读 token):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "finboard": {
      "type": "remote",
      "url": "http://127.0.0.1:8781/mcp",
      "enabled": true,
      "oauth": false,
      "headers": { "Authorization": "Bearer {env:FINBOARD_MCP_TOKEN}" }
    }
  }
}
```

等价命令:`opencode mcp add finboard --url http://127.0.0.1:8781/mcp
--header "Authorization=Bearer {env:FINBOARD_MCP_TOKEN}"`。

非交互运行:

```powershell
$env:FINBOARD_MCP_TOKEN = "<topic-writer token>"
cd C:\Users\28491\Desktop\Lab\oc519-scratch    # 仓库外的 scratch 工程(只放 opencode.json)
$prompt = Get-Content ...\prompt-opencode-writer.txt -Raw
opencode run --standalone --format json -m opencode/qwen3.8-flash $prompt
```

## 结果

### A. topic-writer:读 context + 追加轮次

OpenCode 把 MCP 工具包在它自己的 Code Mode `execute` 工具里(见日志
`tools.finboard.finboard_topic_read`),回执仍是 FinBoard 的 `ToolEnvelope`:

```text
finboard_topic_read(topic_id="RT-…", view="context")
  → OP-6cc4d7896afb status=ok;active;goal.version=v1;最近轮次=1
    (bounds.total_rounds=1,正是 Codex 写入的 RE-391bab58c76648a581ed0c6b)
finboard_topic_write(operation="append", ..., idempotency_key="skill-519-opencode-round-1")
  → OP-809844b34e4b status=ok
    entry_id=RE-dacddc0cc903469faac1c34d
    checksum=b6ab411f…0f9c0a
```

> 读到的"最近轮次=1"来自**另一个客户端、另一个进程、另一个连接**在几分钟前写入的
> 轮次 —— 这就是「用 `RT-` 交接 / 新会话续接」的可验证证据。

### B. readonly:读成功、写被拒

```text
finboard_topic_read(...)   → OP-03fbc752c87d status=ok(revision=1,active,最近 2 轮)
finboard_topic_write(append, idempotency_key="skill-519-opencode-readonly-should-fail")
  → OP-8fb1004e505a status=denied / error.kind=permission_denied
    客户端缺少所需作用域:research.write(已授予:research.read)
```

OpenCode 总结(原样):

> 1. 读回执:status=`ok`(operation_id `OP-03fbc752c87d`)…
> 2. 写回执:status=`denied`(operation_id `OP-8fb1004e505a`),error.kind=`permission_denied`;
>    服务端说明为"客户端缺少所需作用域 research.write,已授予 research.read",retryable=false。
> 3. 服务端确实拒绝了写入:readonly 凭据无法追加轮次,轮次未产生任何业务写入,越权拒绝(#519)符合预期。

## 踩坑(Know-how)

1. **managed service 端口被占**:默认 `opencode` 走后台 managed service(本机
   `127.0.0.1:45278`),被其它进程占用时直接失败
   (`Error: Managed service port 45278 … already in use`)。用 `--standalone`
   起私有 server 即可绕开,不要为此改全局 `opencode service set port`(会影响别人)。
2. **项目配置的发现顺序**:把 `opencode.json` 放在 `FinDashboard-wt4/.tmp/...` 里
   **不生效** —— opencode 按项目根(仓库)发现配置,最终加载的是仓库自带的
   `.opencode/opencode.json`(那里 `finboard` 指向 `host.docker.internal:8765`),
   会话里因此一个 `finboard_*` 工具都没有。把 scratch 工程放到仓库外
   (`C:\Users\28491\Desktop\Lab\oc519-scratch`)后立刻连上。
3. **模型抖动**:`opencode/qwen3.8-flash` 在一次只读场景返回
   `provider.invalid-request: Upstream request was not valid JSON`(exit 1),
   换 `opencode-go/deepseek-v4.1-flash` 重跑即成功。客户端侧模型/网关问题,
   与 FinBoard 无关;证据里保留了失败记录与被替换后的成功记录。
4. **能力探测 ≠ 连接状态**:`list_mcp_resources` 只列出暴露 resources 的 server,
   `finboard` 不出现在里面不代表没连上;要看 `search(namespace="finboard")` 或直接
   调用工具。

## 日志可用性

- **机器可读**:`opencode run --format json` 输出 JSONL(事件 `step_start` /
  `tool_use` / `text` / `step_finish` / `error`);工具调用在 `tool_use` 的
  `state.input.code` 与 `state.output` 里(Code Mode 包装)。
- **服务端日志**:`%LOCALAPPDATA%\opencode\log` 或 `~/.local/share/opencode/log`;
  会话与消息落在 `~/.local/share/opencode/opencode.db`(sqlite)。
- `--print-logs` 可把 server 日志打到 stderr(需配合 `--standalone`)。
