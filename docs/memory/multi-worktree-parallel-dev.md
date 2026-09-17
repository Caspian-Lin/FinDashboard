# 多 worktree 并行开发:本机落地参数与坑位(#289)

**主题**:多 agent 并行开发环境(git worktree + junction 数据共享 + 独立库/端口)。
规范正文在 AGENTS.md「Git 工作流 → 多 worktree 并行开发(#289)」;本篇记录
2026-09-03 本机 wt2 的落地事实与只看规范不知道的坑。

## 结论 / 事实(2026-09-03)

- wt2 位于 `C:\Users\28491\Desktop\Lab\FinDashboard-wt2`,分支
  `feat/multi-agent-worktree-dev-289`(base `origin/m/research-backtest` = 94f06db;
  本地 `m/research-backtest` 当时落后 8 个 merge,已顺手 fast-forward)。
- `data_cache`(≈680MB)/ `data_releases`(≈2.0GB)是 junction 指向主目录,零拷贝;
  tushare 用量计数文件(`data_cache/tushare_usage.json`)随 junction 共享,
  两个工作区的 RPM / 日预算从此**合并计数**(对全局预算反而更准)。
- wt2 数据库 `findashboard_wt2` / `findashboard_wt2_test`(同一 PostgreSQL 实例;
  账号已有 CREATEDB);测试库表由 conftest `ensure_test_db` + `create_all` 自动建,
  无需 alembic,开发库需 `alembic upgrade head`。
- 端口:API 8002 / web 5174 / MCP 8766(已禁用);OpenCode 相关全关(容器名
  `finboard-opencode-web` 全局唯一,研究运行时锚定主目录)。

## Why

- 行情 parquet 缓存路径在代码里是**硬编码相对常量** `_CACHE_DIR = "data_cache"`
  (finboard-api routes / finboard-mcp tools / CLI 默认值多处),不是 Settings 字段
  → worktree 进程按 CWD 只会看到空目录,让 worktree 指向主目录数据只能 junction,
  改成可配置需另开 issue 动多处代码。
- vite.config.ts 曾硬编码 5173 + 代理 `localhost:8000`:vite 端口被占会**静默 +1**,
  但代理不变 → 第二个 worktree 的前端会代理到第一个的后端,预览到错误数据且无报错。
  #289 已改为 `FINBOARD_WEB_PORT` / `FINBOARD_WEB_API_PORT` 可覆盖,显式指定端口时
  `strictPort` 占用即报错。

## How to apply

- 建 wt3+:直接跑 `scripts/create-worktree.sh 3 <feat分支名>`(幂等,可中断重跑)。
- wt2 日常:`cd FinDashboard-wt2 && make dev API_PORT=8002 WEB_PORT=5174`。
- **删除 junction 只能 `cmd //c rmdir data_cache`;Git Bash `rm -rf` 会穿透链接删掉
  主目录真实数据**(与 [git checkout 覆盖未提交改动](git-checkout-overwrites-uncommitted-work.md)
  同类事故面,均有前科)。
- Git Bash 的 `ln -s` 默认是**复制**,建链接一律 `cmd //c mklink /J`(无需管理员权限)。
- `git worktree list --porcelain` 输出带 `\r`,脚本里匹配前要 `tr -d '\r'`。
