# AGENTS.md

可实盘交易的量化系统。实盘交易涉及真实资金，改动须谨慎，不要在未确认风险的情况下改动下单 / 持仓 / 风控相关逻辑。

## 项目设计

完整设计文档见 `phase1_doc.md`（生产级量化交易系统开发计划）。以下为对 agent 最关键的红线与约束。

### 架构选型（第一阶段硬约束）
- 技术栈：Python 交易进程 + PostgreSQL
- 形态：**模块化单体**；不引入微服务 / Kafka / Kubernetes / 分布式事务
- 范围：单账户、单券商接口、单市场（A股或期货二选一）；在链路稳定前不抽象多券商适配层
- 模块划分：Broker Adapter / Trading Gateway / Order Manager / Position Manager / Account Manager / Risk Manager / Strategy Runner / Reconciliation

### 阶段优先级
- **P0/P1（已完成）**：端到端真实交易链路（MockBroker + QMT 适配器）—— 查询账户 / 发单 / 撤单 / 接收回报 / 持仓核对 / 重启恢复。issue #1-#5。
- **P2（已完成）**：重启恢复完善（UNKNOWN 状态修复 + 本地↔券商订单匹配）—— issue #9。
- **P3（已完成）**：基础风控 + 人工交易控制台（PreTradeChecker / KillSwitch / FastAPI + React）—— issue #12。
- **P4（已完成）**：连续实盘运行基础设施 —— 故障注入测试 + 可观测性增强（#13）、定时任务调度（#14）。
- **P5（大部分完成）**：策略框架 + 行情事件 —— 策略运行器（#11）、行情数据接入（#10）已实现；策略状态持久化待验证。
- **下一阶段**：实盘环境验证（QMT 真机调试 + 标的元数据 + 连续运行验证）→ 第一个真实策略 → 小资金实盘。
- **P6 及以后禁止提前开工**：TWAP / VWAP / 冰山单 / 智能拆单、跨账户路由、复杂回测、因子系统、LLM 接入。

### 交易安全红线（改动这些逻辑必须先与用户确认风险）
- `client_order_id` 必须由本地生成且全局唯一，用于防重复下单 / 关联券商订单 / 状态恢复 / 排查异常
- **下单请求超时后禁止无条件重试**（这是文档明确指出的最危险场景）：订单须先进入 `UNKNOWN` 状态，查券商委托记录确认原订单不存在后才能重发；查询类请求可重试。重启恢复（`RecoveryEngine`）在 `kernel.start()` 中自动执行：逐订单查券商 → 修复状态 → UNKNOWN + 券商无记录 → REJECTED（不自动重发）
- 订单必须走完整链路：`Strategy → Order Intent → Risk Manager → Order Manager → Broker Adapter → Broker API`。**策略不得直接调用券商接口**
- 持仓的最终真实来源是**券商查询**；本地持仓仅用于实时响应 / 策略计算 / 风控 / 异常检测。**禁止策略直接修改持仓数量**，持仓只能由成交 / 公司行为 / 交割 / 人工校准等事件驱动
- 系统重启后必须先完成本地 ↔ 券商核对，核对通过前**禁止发送新订单**（恢复流程见 `phase1_doc.md` §5.4）
- Kill Switch（暂停新单 / 仅允许减仓 / 撤全部活动订单 / 暂停某策略某账户 / 全局停止）必须由**交易内核**执行，不能只依赖网页按钮

### LLM / Agent 边界
LLM（包括本 agent 自身）**不允许**：直接连接实盘账户、直接发送订单、修改账户持仓、绕过风控、在实盘运行时动态生成代码并立即执行。LLM 的产出必须经过完整流程才可上实盘：`研究 → 回测 → 样本外 → 行情回放 → 模拟交易 → 影子交易 → 小资金实盘 → 扩大资金`。

## Git 工作流

### 分支模型
- `main` — 稳定发布
- `dev` — 集成分支，所有新特性从这里切出
- `m/<里程碑>` — 里程碑分支
- `feat/<issue-slug-id>` — 功能分支，从 `dev` 切出

### 开发流程（严格按序）
0. 在github仓库创建issue和对应里程碑（可选）
1. 从最新的 `dev` 切出 `feat/<issue-slug-id>`
2. 本地完成开发，**本地须通过 CI**
3. 用 `gh pr create` 创建 PR（目标分支为对应里程碑或 `dev`）
4. **等待用户确认合并** — 这是整个流程中唯一需要用户确认的步骤
5. PR 合并后会自动关闭对应 issue，不要手动关

feat 分支只能通过 PR 合并进里程碑或 `dev`，禁止直接 push 合并。

### 提交信息
- 格式：`<分类>: <修改点描述>`，分类如 `feat` / `fix` / `refactor` / `docs` / `chore` 等
- 描述要点到修改层面即可，不要罗列具体代码行
- **禁止添加任何 `Co-Authored-By` 署名**（包括 Claude / Cursor / ChatGPT 等所有 AI 助手）

### Issue 规范

所有进入开发的事项必须以 GitHub issue 形式登记，字段缺失的 issue 不予开工：

- **背景**：为什么做这件事（业务 / 技术 / 风险驱动力）。一句话能说清的不要写两段。
- **问题 / 需求描述**：具体要解决什么。现状（现有代码、日志、数据）是什么，期望状态是什么。触及交易安全红线的必须明确标出。
- **相关 issue / PR**：列出前置、后继、重复、相关项，用 GitHub 关键字关联：`blocked by #N` / `blocks #N` / `related to #N` / `duplicate of #N`。
- **验收标准（Acceptance Criteria）**：以可勾选清单形式给出 pass/fail 条件，至少覆盖：
  - 功能点（映射到 `phase1_doc.md` §3.4 端到端验收步骤中相关的条目）
  - 测试要求（单元 / 集成 / 故障注入）
  - 文档变更（AGENTS.md / README / 接口契约）
  - 风险要求（是否触及交易安全红线、是否需要用户确认）

### PR 规范

PR 是变更进入 `dev` / 里程碑分支的唯一入口。PR 描述必须包含以下字段：

- **实现情况**：逐条对照 issue 验收标准说明完成情况；未完成项必须显式标出并说明原因，不能偷偷漏掉。
- **关键决策**：实现过程中做出的技术选型、取舍与理由。为什么 A 方案而不是 B 方案、跳过某项检查的原因、新引入的依赖、对外接口的变更。
- **Know-how / 坑位**：沉淀开发过程中的经验 —— 调试技巧、踩到的坑及根因、规避方式、参考链接。目标是让后续接手的人能快速复用、不重蹈覆辙。
- **测试与验证**：本地执行了哪些命令（lint / typecheck / test 的具体命令与用例），结果如何；CI 状态。
- **风险与回滚**：是否触及交易安全红线（`client_order_id` 唯一性 / 下单超时不重试 / 持仓真实来源 / 重启恢复 / Kill Switch）？触及的话是否已与用户确认？回滚方案（迁移 down / feature flag / 配置回退）是什么？

PR 标题遵循提交信息规范（`<分类>: <修改点描述>`），并在描述中用 `Closes #N` 让合并后自动关闭对应 issue。

## 常用工具
- `gh` — 创建 PR、issue、查看 CI 等 GitHub 操作的首选方式

## 当前仓库状态

P0-P4 已完成（issue #1-#14 全部关闭）。P5 大部分完成（策略框架 + 行情事件已实现，策略状态持久化待验证）。当前进入**实盘环境验证**阶段：QMT 真机调试 + 标的元数据 + 连续运行验证 → 第一个真实策略 → 小资金实盘。

### 构建 / 测试 / lint / typecheck 命令

```bash
# 运行全部测试（需 PostgreSQL 运行在 127.0.0.1:5432）
uv run pytest tests/ -v

# 仅单元测试（不需 DB）
uv run pytest tests/unit/ -v

# 仅集成测试（需 DB）
uv run pytest tests/integration/ -v

# Lint
uv run ruff check packages/ tests/

# Type check
uv run mypy .

# DB 迁移
uv run alembic upgrade head
```

### Monorepo 结构

```
packages/
  finboard-shared/     — 领域模型 / 类型 / 异常 / ID 生成
  finboard-persistence/ — SQLAlchemy ORM / Repository / Alembic 迁移
  finboard-broker/      — BrokerAdapter / MarketDataAdapter 抽象 + Mock 实现 + factory
  finboard-broker-qmt/  — QMT (xtquant) 适配器 — 交易 + 行情(xtdata)（Windows-only）
  finboard-core/        — TradingKernel / OrderManager / PositionManager / 状态机 / EventBus
  finboard-risk/        — PreTradeChecker / KillSwitch / RiskConfig
  finboard-reconcile/   — ReconciliationEngine（只读核对）+ RecoveryEngine（状态修复）
  finboard-scheduler/   — asyncio 定时任务调度（盘前检查 / 收盘撤单 / 日终核对 / 连接心跳）
  finboard-app/         — 组装根 / CLI / 配置
  finboard-api/         — FastAPI REST + WebSocket API（人工交易控制台后端）
```
