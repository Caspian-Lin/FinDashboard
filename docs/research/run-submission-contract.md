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

服务内的门控顺序与函数调用是既有实现的原样搬移（策略版本 → 发布可用性 → 能力 →
data_scope → 快照/序列 → 预置因子 → screen 绑定 → single_shot 快照 → 决策日历 →
用户因子 → 序列覆盖 → 多期特征 → user_code → 换发布/锚定 → screen 证据 → 候选池 →
组合可行性 → 政策覆盖 → manifest）。

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
  `actual_release_ids` 等结构化字段，旧客户端按字符串读取仍可用）；
- MCP：`McpToolError(exc.mcp_kind, f"{message}({code})")` —— MCP 信封的
  `error` 只有 kind / message / retryable，没有 details 字段。

`ValueError → invalid_argument` 兜底仍保留给尚未迁移的老路径，新代码不得依赖它
区分冲突。

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

## 7. 适配层等值测试

`tests/integration/test_research_run_submission_equivalence.py`（真实 PostgreSQL）：

- 成功：同一 payload 两条通道的 `input_checksum`（与运行身份无关）一致，
  同键跨通道命中同一 `RR-`；
- 错误：未发布策略 / 发布不匹配 / scope 漂移 / 候选池为空 / 死分区非空，
  `(status_code, detail.code)` 与 `error.kind` 同源且零写入；
- 幂等：同键两次 → 1 run + 1 job；
- 并发：`asyncio.gather` 两路同键 → 单行（撞唯一索引后重读既有行）；
- 重放：REST 与 MCP 对同一源 run 产出同一 manifest 与同一血缘字段。

## 8. 边界

- 只写 `research_runs` / `background_jobs`（研究域），不触实盘
  `orders` / `fills` / `positions` / `audit_logs`；
- 不新增执行能力：提交只登记 `queued`，执行仍由离线 worker 调
  `ResearchRunCoordinator`；
- `finboard_backtest_run(strategy_spec=…)` 只是路由到同一服务的轻入口，不把
  no-code 规格编译进事件驱动 `BacktestEngine`。
