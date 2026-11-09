# 研究操作审计运维手册（#518）

研究域统一操作账本：`research_audit_events`。目标是用一条契约证明
「谁（actor/client）在哪个通道（source）对哪个课题/运行/任务做了什么、结果如何」，
并让 Web、MCP、worker 三条路径产生**可串联**的记录。

边界（红线不变）：

- 只服务研究域；**不写**实盘 `orders` / `fills` / `positions` / `audit_logs`。
- 实盘审计仍使用独立账本与访问边界；本表不替代、不镜像实盘审计。
- 原始参数、代码正文、研究记忆正文、未脱敏对话**永不入库**：只存脱敏摘要
  （单值 ≤120 字、整包 ≤4 KiB）与 sha256 digest。

## 1. 数据字典

`research_audit_events`（无外键、无级联；回滚只删表）：

| 列 | 类型 | 语义 |
|---|---|---|
| `id` | BIGSERIAL PK | 行号（游标第二排序键） |
| `audit_id` | VARCHAR(32) UNIQUE | `RA-<16hex>`，对外唯一标识 |
| `occurred_at` | TIMESTAMPTZ | 事件时间（UTC；服务端写入时的默认时钟） |
| `actor` | VARCHAR(128) | 服务端身份：`user:api` / `agent:MC-…` / `worker:<id>` |
| `actor_type` | VARCHAR(16) | `human` / `agent` / `worker` / `system`（worker 执行器用 `worker`） |
| `source` | VARCHAR(16) | `web` / `mcp` / `worker` / `system` |
| `client_id` | VARCHAR(32) NULL | #517 的 `MC-<12hex>`；Web 无则 NULL |
| `topic_id` | VARCHAR(32) NULL | 课题 `RT-…` |
| `entry_id` | VARCHAR(32) NULL | 轮次 `RE-…`（append 时有值） |
| `operation` | VARCHAR(64) | 见 §2 操作名 |
| `entity_type` | VARCHAR(32) NULL | `research_topic` / `research_run` / `background_job` / `validation_experiment` / `research_code_artifact` |
| `entity_id` | VARCHAR(64) NULL | 实体 ID（与 `correlation` 互为补充） |
| `outcome` | VARCHAR(16) | `succeeded` / `failed` / `rejected` / `denied` |
| `result_code` | VARCHAR(64) NULL | 细业务码（如 `topic_revision_conflict`、`already_unsealed`、`idempotent_hit`） |
| `correlation` | JSON | 关联 ID：`run_id` / `job_id` / `operation_id` / `experiment_id` / `artifact_id` / `replay_of_run_id` / `result_checksum` |
| `params_summary` | JSON | 已脱敏 + 截断的参数摘要（敏感键整值替换 `***REDACTED***`） |
| `params_digest` | VARCHAR(64) | 对**脱敏后、截断前**摘要的 canonical JSON sha256 |
| `error_summary` | TEXT NULL | 脱敏后的错误文本（≤1000 字） |
| `idempotency_key` | VARCHAR(128) NULL | 业务幂等键；**`trust=client_declared` 时存 `client_event_id`**（去重键） |
| `trust` | VARCHAR(16) | `server_observed`（默认）/ `client_declared` |

索引：

- `ix_research_audit_events_occurred`：`(occurred_at DESC)`（列表默认路径）
- `ix_research_audit_events_topic_occurred`：`(topic_id, occurred_at DESC)`
- `ix_research_audit_events_client_occurred`：`(client_id, occurred_at DESC)`
- `ix_research_audit_events_entity`：`(entity_type, entity_id)`
- `ix_research_audit_events_outcome`：`(outcome)`
- `uq_research_audit_client_event`：`(client_id, idempotency_key)`
  **部分唯一**，仅 `trust='client_declared' AND idempotency_key IS NOT NULL`
  —— 客户端声明事件的 `(client_id, client_event_id)` 去重

## 2. 写入点与操作名

写入统一在共享服务层完成（Web 与 MCP 自动同轨迹），worker 在自己的执行器收口：

| 操作名 | 触发点 | source | 成功/拒绝语义 |
|---|---|---|---|
| `run.submit` | `ResearchRunSubmissionService.submit`（REST `POST /api/research/runs`、MCP `finboard_run_queue`） | web / mcp | 成功与 run 行同事务；`result_code=created|idempotent_hit`；拒绝 `rejected`（统一错误码） |
| `run.replay` | `ResearchRunSubmissionService.replay` | web / mcp | 同上；`correlation.replay_of_run_id` |
| `job.enqueue` | `enqueue_job_core(..., context=ctx)`（run 提交双写 job 时） | web / mcp | 与 job 行同事务；`created|idempotent_hit` |
| `job.cancel` | REST `POST /api/jobs/{id}/cancel`、MCP `finboard_job_cancel`（研究域 kind） | web / mcp | 真正发出取消 → `succeeded` + `cancel_requested`；终态/排队 no-op → `rejected` + `not_running`（HTTP/MCP 仍返回当前状态，协作式取消语义） |
| `topic.create` | `workspace_write`（`create` / `draft_create`） | web / mcp | 成功与课题行同事务；`created|draft_created|idempotent_hit` |
| `topic.update` | `workspace_write(update)` | web / mcp | 版本冲突 → `rejected` + `topic_revision_conflict` |
| `topic.activate` | `workspace_write(activate)` | web / mcp | 同上 |
| `topic.transition` | `workspace_write(transition)` | web / mcp | `result_code=<action>`；非法转移 → `illegal_transition` |
| `topic.append` | `workspace_write(append)` | web / mcp | `correlation.entry_id` |
| `experiment.unseal` | MCP `finboard_validation_experiment_run`（拒绝/入队）+ `ValidationExperimentExecutor`（worker 揭盲） | mcp / worker | 已揭盲 → `rejected` + `already_unsealed`；揭盲成功 → `succeeded` + `unsealed`（worker 侧 `actor=worker:validation_experiment`） |
| `research_code.submit` | MCP `finboard_research_code_submit` | mcp | 与 artifact 行同事务；`draft_created` |
| `research_code.promote` | MCP `finboard_research_code_promote` | mcp | 晋级门失败 → `rejected` + `promotion_gate_failed`；成功 `promoted` |
| `run.worker.finished` | `ResearchRunExecutor` 终态收口 | worker | `actor=worker:research_run`、`actor_type=worker`；`correlation.run_id/job_id/result_checksum`，进程 id 在 `correlation.worker_process_id`；非 succeeded 即 `outcome=failed` |
| `client.<event_type>` | `POST /api/research/audit/client-events` | mcp | `trust=client_declared`，永不覆盖服务端事实 |

`operation` 之外不放自由文本操作名；新增写路径必须在此表登记，否则读取方无法解释。

## 3. 事务语义（关键不变量）

- **成功路径**：`ResearchAuditRecorder.record(session, event)` 在调用方 session
  **只 flush、不 commit**，由业务层唯一一次 `commit()` 一起提交。
  审计插入失败 → 异常向上抛 → 整个业务事务回滚（issue #518 明确要求
  “审计持久化失败、写回滚”）。因此不存在「业务成功但无审计」的静默窗口。
- **拒绝/失败路径**：业务事务已回滚，`record_detached(event)` 用**独立会话**
  写入并 commit；自身失败只记 `research.audit.detached_failed` 日志，
  **绝不**掩盖原始错误（调用方仍看到原始异常/HTTP 码）。
- **幂等**：重复提交命中 `idempotency_key` 时记 `result_code=idempotent_hit`；
  同幂等键的第二次成功写仍会新增一条审计行（审计是事件流，不是状态表）。
- **顺序**：`occurred_at` 由服务端时钟；游标为 `(occurred_at, id)` 二元组，
  同一时刻多行也稳定分页（`id DESC` 兜底）。
- **客户端声明事件**：`record_client_declared` 先按 `(client_id, client_event_id)`
  查重，再由部分唯一索引兜底并发；重复上报返回原 `audit_id`（`created=false`）。

## 4. 读取契约

REST（`finboard-api/routes/research_audit.py`）：

```text
GET /api/research/audit?actor=&client_id=&source=&topic_id=&entry_id=&entity_id=
                   &operation=&outcome=&from=&to=&cursor=&limit=   # limit ≤ 200
GET /api/research/audit/{audit_id}
GET /api/research/audit/coverage
POST /api/research/audit/client-events        # 默认关闭,见 §5
```

- 列表返回 `{items, limit, has_more, next_cursor}`；`next_cursor` 是不透明
  base64（`occurred_at` + `id`），只能原样回传。
- 单条/列表只返回脱敏摘要与 digest，**没有**原始参数接口（原始参数从未入库）。
- `coverage` 返回 `{from, to, total, server_observed_from, client_declared_from,
  gaps, retention_days, auto_purge:false, note}`；`from` 是最早一条记录，
  空表时为 `null`。

MCP：`finboard_audit_query`，scope **`audit.read`**（#517）。该 scope 只发给
运维/审计客户端，不进入 `readonly` / `topic-writer` / `data-operator` 预设。
参数与 REST 同源：`audit_id?` / `actor?` / `client_id?` / `source?` /
`topic_id?` / `entity_id?` / `operation?` / `outcome?` / `from?` / `to?` /
`limit?`（1..200）。

## 5. 客户端声明事件（可选）

`POST /api/research/audit/client-events` 只在
`FINBOARD_RESEARCH_AUDIT_CLIENT_EVENTS=true` 时注册（默认 `false` → 404，
避免默认扩大攻击面）。鉴权复用 #517 的 MCP 凭据（`Authorization: Bearer`，
只存 sha256 摘要），并要求 `research.write` scope。

请求白名单（`extra="forbid"`，未知字段 422）：

```json
{"event_type": "tool_call|external_call|run",
 "client_event_id": "≤128",
 "tool_name": "≤128",
 "started_at": "ISO8601", "ended_at": "ISO8601",
 "outcome": "ok|error|denied|rejected",
 "correlation": {"topic_id": "RT-…"},
 "summary": "≤2048 字"}
```

落库：`trust=client_declared`、`operation=client.<event_type>`、
`idempotency_key=client_event_id`；`params_summary` 只含 `event_type` /
`tool_name` / 起止时间 / `summary[:120]`（脱敏后）。**不接收**模型推理、
对话正文、凭据；服务端事实（`server_observed`）永不因上报而改变。

## 6. 保留与清理

- `FINBOARD_RESEARCH_AUDIT_RETENTION_DAYS`（默认 `365`）只声明保留策略；
  **v1 不自动清理**（没有定时任务、没有启动清理）。
- 手动清理：

```bash
finboard research audit purge --before 2025-11-09           # 默认 dry-run,只计数
finboard research audit purge --before 2025-11-09 --no-dry-run
```

`--before` 按 UTC 日期解释：删除 `occurred_at < YYYY-MM-DD 00:00:00Z` 的行。
dry-run 不写库。清理不可撤销，执行前请先备份或导出。

## 7. 历史覆盖缺口（如实声明）

- **迁移/接入之前**的研究写操作没有审计行：`coverage.from` 之前是
  「无审计记录」，不是「无操作」。UI/报告必须显示「此前无审计记录」。
- `gaps` 常量包含：`research_writes_before_518`、`mcp_tool_calls_before_518`、
  `client_declared_events_not_backfilled`。
- `mcp_audit_events`（#157）仍是 MCP **工具级**调用审计（延迟/失败/参数摘要），
  与本表并存：工具级延迟看 `mcp_audit_events`，业务事实与结果看
  `research_audit_events`。两者 `operation_id` 可关联但本版不强制回填。
- 本版未接入的写路径（明确缺口，**不得**按已覆盖对外表述）：
  `run.cancel`；`research_memories` create / correct / forget / confirm /
  archive；`validation experiment` create / add_trial / reject / delete
  （只有 `experiment.unseal` 已接入）；`strategy draft / publish / rollback`；
  `factor experiment create`；`simulation session start`；
  `data backfill submit`。
- **兜底规则**：其余研究写工具**默认视为未接入审计**——在 §2 逐个登记前
  一律按缺口处理，例如 `finboard_research_code_rollback`、
  `feature_snapshot_create` / `feature_snapshot_job_start`、
  `factor_series_build`、`backtest_run` / `backtest_grid_submit` /
  `backtest_history_delete`、`sim_session_*`、`preset_*` 等。这些路径的业务表
  仍有自己的状态记录，但没有统一研究审计行；新增时按 §2 登记操作名并复用
  同一 recorder。
- 审计记录不随业务对象删除而清理（无外键）：课题/运行清理后审计仍保留，
  用于事故取证。

## 8. 常见排查

```sql
-- 某次 run 的完整链路(提交 → 入队 → worker 终态)
SELECT audit_id, occurred_at, source, actor, operation, outcome, result_code, correlation
FROM research_audit_events
WHERE correlation->>'run_id' = 'RR-…'
ORDER BY occurred_at, id;

-- 某客户端最近的操作
SELECT occurred_at, operation, outcome, topic_id, entity_id, result_code
FROM research_audit_events
WHERE client_id = 'MC-…'
ORDER BY occurred_at DESC, id DESC
LIMIT 50;

-- 拒绝/失败热点
SELECT operation, result_code, count(*)
FROM research_audit_events
WHERE outcome IN ('rejected', 'failed', 'denied')
GROUP BY 1, 2 ORDER BY 3 DESC;
```

写入失败症状：

- `research.audit.detached_failed`：拒绝路径补记失败（原始错误仍已返回）；
  检查 DB 可用性与 `research_audit_events` 权限。
- 成功路径审计失败会以 500/503 暴露并回滚业务——这是**预期**的 fail-closed，
  不要改成“吞掉审计错误继续提交”。
