# 研究知识沉淀(docs/research,#268)

本目录是跨会话研究知识的 **canonical 事实源**,供三方共享:用户、主 coding
agent、OpenCode 研究 agent(容器内只读挂载在 `/workspace/docs/research/`)。

## 为什么存在

2026-09-01 盘点(#268 背景):研究会话每开一轮都重新读取、重复造轮子——
证据矩阵每轮重拼、已证伪假设可能被重测。根因是知识只存在于三个不共享的
载体(`research_memories` 自由文本、run/experiment 机器结果、OpenCode 会话
卷聊天记录),没有结构化的「计划 / 结论」层。本目录补上这一层。

## 三件套职责

| 文件 | 职责 | 更新频率 |
|------|------|----------|
| `ROADMAP.md` | 研究目标阶梯、路线 A-D 状态、当前证据门、数据边界 | 目标 / 路线变更时 |
| `FINDINGS.md` | 结论注册表(结论 / 置信度 / 证据 ID / 失效条件 / 状态) | 每有新结论或结论状态翻转时 |
| `rounds/YYYY-MM-DD-<slug>.md` | 轮次日志(目标 / 动作 / 证据 / 结论+置信度 / 开放问题) | 每轮研究收尾时 |

`rounds/_TEMPLATE.md` 是轮次日志模板,新轮次复制改名填写。

## 更新流程(canonical 文档走 PR)

- **文档是 canonical**:修改本目录一律走 PR(版本化、人类可审)。
- **记忆是指针**:OpenCode 研究 agent 对本目录**只读**(容器 `:ro` 挂载,
  目录缺失时容器跳过挂载)。agent 每轮收尾按固定模板写 `research_memories`
  (tag `research-round`)并在回答附「轮次摘要」,由用户 / 主 coding agent
  转录进 `rounds/`。**文档与记忆冲突时,以文档为准。**
- 研究侧的会话启动 / 收尾协议见
  `.agents/skills/finboard-opencode-research/SKILL.md`(启动必读 ROADMAP +
  FINDINGS + 记忆索引;未查结论索引禁止重测已证伪假设)。

## 保留记忆 tags(指针层)

`research-plan` / `research-round` / `finding-confirmed` / `finding-refuted`
——`finboard_memory_list(tag=...)` 的检索入口,语义见 Skill
`references/memory.md`。
