# DeepSeek Harness 接入(instructions 上限坑)

## 主题

外部 Harness(以 DeepSeek Harness 44.7.0 为例)接入 FinBoard MCP 时,「连上了但
0 个工具」这类静默失败的真实成因与判断方法。配置步骤本身见
`docs/agent-integration.md` §5,这里只记不读源码就看不出来的部分。

## 结论 / 事实

1. DSH 的 MCP 桥有个 **`maxInstructionBytes`,默认 32768 字节**。FinBoard 的
   `_INSTRUCTIONS` 约 43 KB(2026-11-10 实测 43,087 字节),**超限**。
2. 超限的失败点**在连接之后**:`server/discover` 返回 200、`subscriptions/listen`
   返回 200 并 ack,桥读完 instructions 才抛错 → 判定连接失败 → 退避重连死循环。
   外部只能看到 `mcp-client(finboard): server is disconnected`,**没有任何 401 /
   协议错误**,所以要设 `maxInstructionBytes: 262144`(或压低 `_INSTRUCTIONS`)。
3. DSH 的 skill provider 要求 `SKILL.md` frontmatter 同时有 `name` + `description`
   (kebab-case),缺 `name` 会被静默丢弃;且 provider(`@deepseek-ai/dsh-skill-filesystem`)
   在 profile 里**默认关闭**。
4. 模型可见工具名是 `mcp__finboard__finboard_*`(双前缀),`finboard_*` 只是线上
   真实注册名。

## Why

- 服务端 access log 全是 200、鉴权通过、DB 里 `last_used_at` 也在动,唯一异常是
  客户端侧一个上限——只看服务端或只看「连接状态」都会误判成协议不兼容(实测就
  先怀疑了 MCP draft `2026-07-28` 的订阅语义,方向错误)。
- 判断方法:在 loopback 上串一个 TCP tap(`127.0.0.1:8766 → 8765`)打印双向字节,
  能看到 `discover → subscriptions/listen → 立刻 close → 再 discover` 的循环;
  再把 `app.asar` 里的 `dsh-mcp-client/lib/index.js` 抽出来,grep
  `maxInstructionBytes` 即见 `DEFAULT_MAX_INSTRUCTION_BYTES = 32768`。

## How to apply

- 新增任何 Harness 接入时,**先问客户端对 `instructions` 是否有字节上限**;FinBoard
  的 `_INSTRUCTIONS` 只增不减,这类上限迟早撞上,提前在文档写死配置项。
- 看到「连上但 0 工具」时,先量 `len(_INSTRUCTIONS.encode())`,再去翻客户端上限,
  不要先怀疑协议版本。
- 给 DSH 加 MCP 一律走 `~/.dsh/profiles/<profile>/cordis.patch.yml` 的
  `@deepseek-ai/dsh-mcp-client` insert 条目;token 放
  `~/.dsh/secrets/finboard-mcp.token`(ACL 收本用户),用 `!!js` 读,不写进 patch。
- 相关:`docs/agent-integration.md` §2/§5/§8;issue #542。
