# 里程碑分支别名与 dev 集成时点

## 主题
用户口中的「m/opencode-research-mcp」是别名,仓库实际分支为
`m/opencode-research-agent`;以及 2026-08-17 里程碑暂时并入 dev 的事实。

## 结论 / 事实
- 研究/OpenCode/MCP/回测方向的里程碑分支实名 **`m/opencode-research-agent`**
  (PR #178-#181、#191 的 base 全是它);仓库中**不存在** `m/opencode-research-mcp`,
  另有一个较早的 `m/research-backtest`。按别名照搬会找不到分支甚至误建同名分支。
- 2026-08-17 应用户要求,里程碑经 PR #192(merge commit `7e703bd`)「暂时」并入
  dev;当时 dev 是里程碑的直接祖先(落后 180 commits,纯集成无冲突)。**里程碑
  分支保留**,研究类 feat 分支仍从里程碑切,工作流不变。

## Why
用户常用别名指代里程碑,别名与实名不对应是不读对话就无法得知的上下文;
dev 已含里程碑内容(2026-08-17 起),若仍凭旧印象认为「dev 落后于里程碑」
会对 diff 基线做出错误判断。

## How to apply
- 见到「m/opencode-research-mcp」一律映射到 `m/opencode-research-agent`。
- 判断 dev 与里程碑的先后关系先 `git fetch` 再 `git merge-base` 现算,不凭记忆。
