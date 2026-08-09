---
description: FinBoard 研究 Skill —— 指导 OpenCode 研究 Agent 的工作流、工具选择、权限边界与来源引用。用于量化研究、因子分析、回测/模拟盘分析、研究记忆。不用于实盘交易或代码编辑。
---

# FinBoard 研究 Skill

本 Skill 指导 `finboard-researcher` agent 通过 `finboard.*` MCP 工具进行受控
量化研究。

> **权限由 OpenCode 配置 + FinBoard 服务端策略共同控制,本 Skill 不授予任何
> 工具权限。** Skill 只描述「应该做什么」,工具是否可调用由
> `.opencode/opencode.json`(agent permission)与 FinBoard MCP 服务端策略决定。

## 核心原则(HARD RULES)

1. **只读直接调用** —— 数据 / 因子 / ResearchRun / 模拟盘查询 / AI 问答可直接调用。
2. **研究写操作可自主执行**(#122) —— 创建因子 / 快照 / 策略 / 运行回测 / 发布数据 /
   启动模拟盘等研究写操作 agent 可通过 MCP **自主执行**,无需人工审批。
   不触及交易安全红线(不连 broker / 账户 / 订单 / 持仓)。
3. **实盘能力永久不可用** —— 下单 / 撤单 / 改持仓 / Kill Switch / 连接 broker /
   凭证探测不在工具集中。需要它们 = 走错了路。
4. **不生成代码** —— 策略是无代码版本化规格,禁止生成 Python / 模块路径 /
   可执行表达式。

## 工具选择(快速参考)

当前已实现 14 个工具。标注 ✅(可用) / 🔒(planned,对应 issue 尚未实现):

| 场景 | 工具 | 状态 | 权限 |
|------|------|------|------|
| 查询 ResearchRun | `finboard.run.list` / `.get` / `.artifacts` | ✅ | 只读 |
| 金融问答 / 因子假设 / 策略草案 | `finboard.ai.ask` / `.propose_*` | ✅ | 草案(可追溯) |
| 记住 / 查询 / 纠正研究记忆 | `finboard.memory.*`(7 个) | ✅ | 直接执行 |
| 数据查询(instruments/datasets/releases/cache/quality) | 🔒 #124 | 🔒 | 自主执行 |
| 因子(catalog/snapshot/signal/experiment) | 🔒 #125 | 🔒 | 自主执行 |
| 策略规格(registry/template/validate/draft/publish) | 🔒 #126 | 🔒 | 自主执行 |
| 回测 + 模拟盘 + 研究运行 | 🔒 #127 | 🔒 | 自主执行 |
| portfolio 计算(allocate/sizing/feasibility/attribution) | 🔒 #128 | 🔒 | 自主执行 |

> 详细工具契约见 `references/tools.md`;扩展计划见
> `packages/finboard-mcp/ROADMAP.md`。

## 研究工作流(简版)

1. **理解问题** —— 查询相关数据 / ResearchRun / 模拟盘。
2. **形成假设** —— 用 `finboard.ai.propose_hypothesis` 生成结构化因子假设草案。
3. **记忆上下文** —— 用 `finboard.memory.remember` 记住关键发现,关联研究产物。
4. **引用来源** —— 所有结论引用 ResearchRun / 数据集 / 模拟盘产物 ID。

> 详细工作流与决策树见 `references/workflow.md`;
> 完整研究流程(数据→因子→策略→回测→模拟→评估)见 `references/research-workflow.md`。

## 来源引用规则

- 金融答案**必须引用项目来源**(ResearchRun ID / 数据集版本 / 模拟盘 ID)。
- 数据不足时明确声明「数据不足」,**绝不编造数字**。
- 区分 `DraftStatus`:`proposed` 是人工评审起点,**不是结论**。

## 记忆使用规则(简版)

- 用 `finboard.memory.remember` 记住跨会话需要的研究上下文。
- `source_refs` 关联研究产物(只引用,**不修改产物本身**)。
- 发现错误用 `finboard.memory.correct`(形成纠正链),**不要直接删除**。
- 过时但仍有参考价值的记忆用 `finboard.memory.archive`,仅在确需移除时用
  `finboard.memory.forget`(软删除,保留审计)。

> 详细记忆规则与生命周期见 `references/memory.md`。

## 参考文档索引

| 文档 | 内容 |
|------|------|
| `references/tools.md` | 14 个 MCP 工具完整契约(参数 / 返回 / 场景) |
| `references/workflow.md` | 研究工作流决策树 + 标准研究循环 |
| `references/research-workflow.md` | 完整研究流程详解(数据→因子→策略→回测→模拟→评估) |
| `references/memory.md` | 研究记忆使用规则与生命周期 |
| `references/system-overview.md` | FinBoard 系统架构概览(包结构 / 模块职责 / 研究 vs 实盘隔离) |
