# Issue #157 状态（#66 端到端交付缺口修复）

**主题**:Issue #157 已实现并开出 PR #158,等待用户确认合并。

**事实**（2026-08-15）:

- 分支 `feat/research-e2e-gap-fixes-157`,PR #158(目标分支 `m/opencode-research-agent`),`Closes #157`。
- 8 项修复:Agent 定义对齐 #122 写操作自主执行、权限 allowlist(`*` deny + read/glob/grep/skill/finboard_*)、
  移除 OpenCode Web basic auth(用户决策:单用户明文 URL,127.0.0.1 为唯一边界)、
  `opencode_mcp_remote_url` 渲染接线、内嵌 MCP 容器模式绑 0.0.0.0、ResearchRun job 前端轮询、
  Web 报告导出(CSV/Markdown)、`mcp_audit_events` 审计持久化 + `/api/mcp/audit`、
  文档同步(README / PRODUCT.md / finboard-mcp README / Skill 因子工具数 11→12 / AGENTS.md)。

**Why**:PR 合并前用户需确认(AGENTS.md Git 工作流唯一需要用户确认的步骤)。

**How to apply**:

- 合并后需执行数据库迁移 `alembic upgrade head`(新增 `mcp_audit_events` 表,迁移 a9d4e6f8b1c2)。
- 本地跑集成测试需先唤醒并保活 WSL PostgreSQL,见 [[wsl-postgres-keepalive]]。
- 后续新增/修改研究功能时,工具数同步契约测试会强制要求同步 `_INSTRUCTIONS` / `SKILL.md`(#123)。
