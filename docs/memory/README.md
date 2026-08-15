# 记忆索引

按 AGENTS.md「记忆（Memory）规范」维护:记忆文件统一存放在本目录,
本文件是唯一索引;禁止把记忆写入任何 coding 框架自有的路径。

- [Issue #157 状态](issue-157-status.md) — #66 端到端交付缺口修复已实现,PR #158 待合并;合并后需跑迁移 a9d4e6f8b1c2
- [Issue #117 状态](issue-117-status.md) — 统一任务队列 5 个 sub-issue 已全部合并,但 #117 未关:还差 README 队列契约/Worker 启动文档 + 5k 基准测试
- [Issue #137 状态](issue-137-status.md) — 数据写操作 MCP 工具(12 个)已合并;bulk_download_status 因 #117 已合并而下线
- [WSL PostgreSQL 空闲自动关机](wsl-postgres-keepalive.md) — 本机集成测试卡住的根因;跑 DB 测试前须唤醒并保活 WSL VM(附本机 pytest tmp_path / mypy 路径坑)
- [PostgreSQL 僵尸锁卡死测试的根因与防护](pg-lock-hygiene.md) — idle-in-transaction 僵尸事务让测试无限等锁;conftest 已设 lock_timeout,本地角色已设 15min 自动回收,附 pg_stat_activity 排查命令
- [OpenCode Web 工作台:SPA 状态与容器环境踩坑](opencode-web-state-and-home.md) — 项目/最近会话在浏览器 IndexedDB,服务端无法预置;HOME/XDG 决定文件选择器与数据落点;坏路径会话清理方法
