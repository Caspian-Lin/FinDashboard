# ResearchRun 提交契约（#513）

> 状态：生效（2026-11-09，#513 落地）
> 适用：`POST /api/research/runs`、`POST /api/research/runs/{id}/replay`、
> MCP `finboard_run_queue` / `finboard_run_replay` /
> `finboard_backtest_run(strategy_spec=…)`
> 代码：`packages/finboard-app/src/finboard_app/research_run_submission.py`、
> `packages/finboard-app/src/finboard_app/research_operation.py`

`ResearchRun` 提交（预检 → 冻结 → manifest → 同事务双写 `research_runs` +
`background_jobs`）只有**一份**实现。REST 与 MCP 都只是薄适配层：解析输入、
调用服务、把统一业务错误映射成 HTTP / MCP 错误。本文件冻结这条链路的对外契约，
任何改动都要先改这里再改代码。

## 1. 分层与职责

```text
finboard-api  routes/research_runs.py    解析 → service → 映射 HTTP → 唯一一次 commit
finboard-mcp  tools/runs.py              解析 → service → 映射 McpToolError → commit
finboard-mcp  tools/backtest.py          strategy_spec 形态复用同一 service
finboard-app  research_run_submission.py 预检 / 冻结 / manifest / run+job 双写（不 commit）
finboard-app  research_operation.py      操作上下文 + 统一错误分类
finboard-app  background_jobs.py         enqueue_job_core → (job_row, created)（不 commit）
```

禁止在路由或 MCP handler 里重新编排门控；新增门控一律加在服务里，两条通道自动同源。

## 2. 输入契约

`ResearchRunSubmitInput`（原 `ResearchRunQueueIn`，`finboard_api.research_run_schemas`
保留同名 re-export）与 `ResearchRunReplayInput`（原 `ResearchRunReplayIn`）是唯一
输入 DTO，字段、默认值、校验器与历史 schema 逐项一致：

| 字段 | 约束 |
|---|---|
| `idempotency_key` | 8–128 字符；`RR-` 由它确定性派生（`RR-` + sha256 前 24 hex） |
| `strategy_id` / `strategy_version` | 已发布规格 + 版本号 |
| `dataset_release_ids` | 必须与策略验证计划的 `dataset_release_ids` 完全一致 |
| `factor_snapshot_ids` | single_shot 的决策时点来源；被 factor series 覆盖的因子除外 |
| `factor_series_ids` | 内容寻址 `FS-` 序列；锚定发布必须 ⊆ 本次冻结清单 |
| `parameters` | `decision_schedule` / legacy `rebalance_frequency` 二选一，形状同源 |
| `validation_config` / `execution_config` | 死分区：非空即拒 |
| `portfolio_config` / `risk_config` / `fee_config` / `benchmark_config` | 分区键位不可混 |
| `data_scope` + `data_scope_resolution_id` | 成对出现；提交期重算比对，漂移即 409 |
| `code_version` / `initial_capital` / `requested_by` | 冻结进 manifest |
| `actor_type` | `human` \| `agent`；`llm` 在 schema 层拒绝 |

**身份只来自服务端上下文**（`ResearchOperationContext`）：

- REST：`for_web(request_id, actor_type=body.actor_type)`，默认 `human`；
- MCP：`for_mcp()`，恒 `agent`（与 `requested_by=agent:mcp` 对齐，#312）；
- worker：`for_worker(worker_id)`，归属 `worker:<id>`。

`requested_by` 仍是调用方声明字段（写进 manifest 并参与 checksum），与 `actor`
分离。任何 payload / query 都不能覆盖 `actor` / `actor_type` / `client_id`。

## 3. 冻结与字节稳定

服务内的门控顺序与函数调用以 **REST 路由为基准**原样搬移（策略版本 → 发布可用性 →
能力 → data_scope → 快照/序列 → 预置因子 → screen 绑定 → single_shot 快照 →
决策日历 → 用户因子 → 序列覆盖 → 多期特征 → user_code → 换发布/锚定 → screen 证据 →
候选池 → 组合可行性 → 政策覆盖 → manifest）。MCP 旧顺序有两处不同，已按 REST 对齐，
差异见 §7.2（只影响错误文案/`code` 优先级）。

`ResearchRunManifest` 的字段顺序、取值与 None 省略规则**不得改动**：

- `strategy_version` 为 None 时省略（旧 manifest checksum 可校验）；
- `replay_source_status` 为 None 时省略（#305）；
- `factor_series` 为空时省略（#360）；
- `data_scope` 为 None 时省略（#510）。

护栏：`tests/unit/test_research_run_manifest_golden.py` 用 6 组代表性 payload
（最小 / 全参数 / data_scope / user_code / factor_series / replay）锁定
`run_id` 与 `manifest_checksum`；任何字段增删、顺序变化或取值漂移都会失败。
语义变更必须走 `schema_version`，不允许悄悄改既有 `as_dict` 输出。

## 4. 幂等

- `research_runs.idempotency_key` 是**普通唯一索引**（不是部分索引）：同键必须对应
  同一份 manifest，命中即返回原行（`created=false`），内容不同即冲突。
- `background_jobs` 与 run 共用同一 `idempotency_key`：任一系统命中都不重复创建；
  run 行已有 `job_id` 时不补建 job（既有语义，#513 不改变）。
- 跨通道幂等是等值的强证明：REST 与 MCP 用同一键提交时，manifest checksum 只要
  有一位不同就会撞上「相同身份不同 manifest」冲突。
- 终态失败后重试**不重用旧键**（唯一约束不允许），走 replay：新 `RR-` +
  `replay_of_run_id` + `replay_source_status`。

## 5. 错误分类

业务层只抛 `ResearchOperationError` 家族；映射只发生在两个适配层：

| category | HTTP | MCP `error.kind` | retryable | 典型场景 |
|---|---|---|---|---|
| `invalid_input` | 422 | `invalid_argument` | false | 门控失败、候选池为空、快照/序列缺失、死分区非空 |
| `not_found` | 404 | `not_found` | false | 策略版本 / 源 run 不存在 |
| `conflict` | 409 | `conflict` | false | 幂等键内容冲突、未发布规格、不可重放状态 |
| `precondition_failed` | 409 | `conflict` | false | `data_scope_resolution_id` 漂移 |
| `permission_denied` | 403 | `permission_denied` | false | 只读客户端写 |
| `payload_too_large` | 413 | `invalid_argument` | false | 载荷超限 |
| `unavailable` | 503 | `unavailable` | true | 依赖暂不可用 |

细业务码（`strategy_not_published` / `dataset_release_mismatch` /
`scope_release_drift` / `universe_empty` / `policy_override_invalid` …）放在
`code` 上：

- REST：`HTTPException(status_code=exc.http_status, detail=exc.to_detail())`，
  `detail = {"code", "message", **details}`（`details` 保留
  `actual_release_ids` 等结构化字段）；
- MCP：`McpToolError(exc.mcp_kind, f"{message}({code})")` —— MCP 信封的
  `error` 只有 kind / message / retryable，没有 details 字段。

`ValueError → invalid_argument` 兜底仍保留给尚未迁移的老路径，新代码不得依赖它
区分冲突。

### 5.1 对外行为变更：REST `detail` 从字符串变为结构化体

#513 之前 `POST /api/research/runs` 与 `/replay` 的 4xx `detail` 多数是**纯字符串**
（只有 #510 的 scope 错误已是 `{code, message, …}`）。收敛后所有研究运行提交错误
统一为对象：

```json
{"detail": {"code": "universe_empty", "message": "运行数据发布候选池为空,拒绝入队: …"}}
```

- **这是对外契约变更**，不做字符串兼容：把 `detail` 当字符串用的客户端会拿到对象。
  典型症状是界面显示 `[object Object]` 或断言 `"xxx" in detail` 抛 `TypeError`。
- 迁移建议：读 `detail.message` 展示；按 `detail.code`（而不是文本）分支；
  `detail` 其余键是结构化补充（`actual_release_ids` / `expected_release_ids` 等），
  可能为空，不要假定存在。
- Web 端 `web/src/lib/api.ts` 的 `errorMessage()` 已优先取 `detail.message`，
  `ResearchRuns.tsx` 无需改动；自建客户端 / 脚本 / 外部 harness 需要自查。
- MCP 侧无对应破坏：`error.kind` 一直是枚举，细码只是追加在 `message` 尾部。

## 6. 事务所有权

服务**只 flush，不 commit**。调用方持有唯一一次提交：

```python
async with app.session_maker() as session:
    outcome = await ResearchRunSubmissionService(session).submit(body, ctx)
    await session.commit()          # 唯一提交点
```

失败时调用方 `rollback()`。这条契约是 #515 的前提：课题链接 + run + job + 审计
必须在同一事务内原子生效，服务内部再 commit 就无法回滚。

故障注入护栏：`tests/integration/test_research_run_submission_equivalence.py` 在
run 行写入后让 job 入队抛错，断言 `research_runs` / `background_jobs` 两表零残留。

## 7. REST 与 MCP 收敛后的行为差异

收敛前 REST 与 MCP 是两份编排，以下差异随 #513 一并消除。它们是**对外行为变化**，
按影响面从大到小列出（REST 行为始终是基准，MCP 向 REST 对齐）。

### 7.1 bars 主发布（修 bug，影响 MCP 成功/失败）

- **旧 MCP**：候选池与组合可行性预检用 `releases[0].instruments`（`dataset_release_ids`
  的第一项）。
- **新实现（与 REST 一致）**：先用 `_bars_release()` 选出唯一的 bars 主发布，再用
  `primary.instruments` / `primary.end_date` 评估。
- **可见差异**：联合发布（bars + daily_metrics / financial_indicators）且**研究数据
  发布排在前面**时，旧 MCP 会用研究发布的窄宇宙做评估。典型症状：研究发布只覆盖
  少量标的 → 旧 MCP 误报 `422 invalid_argument`（`universe_empty`，或
  `portfolio_infeasible`：静态候选池 < `ceil(1/0.35)=3`），而 REST 同一 payload 成功。
  修复后两条通道一致。
- 回归护栏：`tests/integration/test_research_run_submission_equivalence.py::
  test_split_release_uses_bars_primary_pool`（`dataset_release_ids=[daily, bars]`，
  daily 宇宙更窄，两通道都必须成功且冻结清单顺序不变）。

### 7.2 错误优先级（只影响 message/code，status/kind 不变）

同一 payload 同时命中两个门控时，先报哪一个取决于门控顺序。收敛后以 REST 顺序为准，
MCP 的**错误文案 / `code`** 可能变化；HTTP 状态码与 MCP `error.kind` 不变（都是
`invalid_input` → 422 / `invalid_argument`）：

- **single_shot 缺快照 vs screen 绑定错配**：旧 MCP 先报缺快照
  （`snapshot_required`），新实现先做 screen 绑定实绑校验（`screen_binding_invalid`）。
- **bars 主发布基数 vs 用户因子（`u_`）门控**：旧 MCP 先算用户因子名单
  （`user_factor_not_active`），新实现先校验「恰好一个 bars 主发布」
  （`bars_release_ambiguous`）。

只按 `kind` + `code` 分支的客户端不受影响；依赖错误文本做断言的脚本需要复核。

### 7.3 MCP replay 的入参校验（新增拒绝路径）

- **旧 MCP** `finboard_run_replay` 直接接受 `idempotency_key` / `requested_by`
  原始字符串，无长度/非空校验（空串也能进 manifest）。
- **新实现**复用 `ResearchRunReplayInput`：`idempotency_key` 必须 8–128 字符、
  `requested_by` 非空，否则 `invalid_argument`（422 语义）。与 REST
  `/runs/{id}/replay` 完全同规。

## 8. 适配层等值测试

`tests/integration/test_research_run_submission_equivalence.py`（真实 PostgreSQL）：

- 成功：同一 payload 两条通道的 `input_checksum`（与运行身份无关）一致，
  同键跨通道命中同一 `RR-`；
- 错误：未发布策略 / 发布不匹配 / scope 漂移 / 候选池为空 / 死分区非空，
  `(status_code, detail.code)` 与 `error.kind` 同源且零写入；
- 幂等：同键两次 → 1 run + 1 job；
- 并发：`asyncio.gather` 两路同键 → 单行（撞唯一索引后重读既有行）；
- 重放：REST 与 MCP 对同一源 run 产出同一 manifest 与同一血缘字段；
- 联合发布顺序：`dataset_release_ids` 把研究数据发布排在 bars 前面时，两通道都按
  bars 主发布评估（§7.1）。

## 9. 边界

- 只写 `research_runs` / `background_jobs`（研究域），不触实盘
  `orders` / `fills` / `positions` / `audit_logs`；
- 不新增执行能力：提交只登记 `queued`，执行仍由离线 worker 调
  `ResearchRunCoordinator`；
- `finboard_backtest_run(strategy_spec=…)` 只是路由到同一服务的轻入口，不把
  no-code 规格编译进事件驱动 `BacktestEngine`。
