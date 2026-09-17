# 里程碑分支别名与 dev/main 集成时点

## 主题
用户口中的「m/opencode-research-mcp」是别名,仓库实际分支为
`m/opencode-research-agent`;以及 dev / main / 里程碑三者的集成时点。

## 结论 / 事实
- 研究/OpenCode/MCP/回测方向的里程碑分支实名 **`m/opencode-research-agent`**
  (PR #178-#181、#191 的 base 全是它);仓库中**不存在** `m/opencode-research-mcp`,
  另一个里程碑是 **`m/research-backtest`**。按别名照搬会找不到分支甚至误建同名分支。
- 集成时点:
  - 2026-08-17 应用户要求,里程碑经 PR #192(merge commit `7e703bd`)「暂时」并入
    dev;当时 dev 是里程碑的直接祖先(落后 180 commits,纯快进无冲突)。
  - 2026-09-17 dev 快进至 `m/research-backtest` 末端 `4a2dd03`,main 合并 dev
    (merge commit `2befdcb`,第二父提交即 `4a2dd03`),**dev / main / 里程碑
    三者同树**,「dev 落后里程碑 180 commits」的旧关系自此不再成立。
- 里程碑分支保留,研究类 feat 分支仍从里程碑切,工作流不变。

## Why
用户常用别名指代里程碑,别名与实名不对应是不读对话就无法得知的上下文;
dev / main 与里程碑的先后关系随合并推进而变化(2026-08-17 dev 落后 180 commits,
2026-09-17 起三者同树),凭旧印象判断会对 diff 基线与 PR base 选择做出错误决策。

## How to apply
- 见到「m/opencode-research-mcp」一律映射到 `m/opencode-research-agent`。
- 判断 dev / main / 里程碑的先后关系先 `git fetch` 再 `git merge-base` 现算,
  不凭记忆;三者同树期间从 dev、main 或里程碑切 feat 分支起点等价(仍按惯例
  从里程碑切)。
