# FinDashboard 研究课题与冻结说明书契约

2026-10-03，#498–#500。只操作研究元数据与读侧，保存、发布或更新课题都不会启动运行，不改既有 manifest/checksum、撮合、风控或晋级规则。

## 页面与状态

首页先展示近期课题的问题、工作结论、证据缺口和下一步；课题入口 `/research/topics` 支持建立问题、更新目标版本、暂停、收束与追加轮次。导航分为课题、策略、研究回测、OOS、模拟与研究资料；数据、因子、组合、报告、工作台和快速回测保留在高级研究工具。原深链仍可访问。研究可分支、反复、跨会话续接，页面所在位置不是完成证据。

ResearchRun 是冻结输入后执行的离线研究回放，不等同所有研究活动。因子实验检验信号，OOS实验检验预注册假设，模拟盘观察持续纸面执行；其结果不互相代替。

运行执行模式优先读取 result.execution_mode，无结果时解析 parameters.decision_schedule（daily/weekly/monthly/quarterly/custom），再兼容 rebalance_frequency。冲突或未知形态显示未知/冲突，不猜单次运行；自定义日历只展示首末日、次数与类型。历史无足够证据时保持未知。

实验流程状态和 `oos_outcome` 分开展示与筛选。validated_oos 不等于 supported，not_supported 与 inconclusive 必须显式呈现；缺字段是未知。前端结论筛选明确限定当前获取列表，不能作为全库统计。published/completed 只表示发布/执行，不授予晋级。

## 冻结说明书

`GET /api/research/explanation?run_id=RR-...` 直接取冻结 manifest 的规格；也可指定 strategy_id 与精确 version 阅读版本规则，禁止默认用最新版本解释历史。响应包含 provenance、spec、factors、effective_policies、overrides、schedule、gaps、warnings、mechanism_hypotheses。组合/风险/费用解析复用执行端函数，仍是读侧派生，不构成历史执行复验；旧代码版本的生效情况需费用账本等执行证据。

因子序列元数据按冻结 series ID/checksum 读取，不加载 values 或 parquet。p_ 因子只有冻结 code_commit 与当前目录实现锚匹配才展示公式、窗口和原始偏好；不匹配、缺失或歧义明确缺证据。图中 negate 与正负加权传播实际数值方向；非单调或相反路径混合不猜方向。有效分数方向还须结合买卖规则。经济机制标为来源明确的外部假设，日线不能识别交易对手；8%回撤触发不是最大回撤8%的保证。

`GET /api/research/explanation/{run_id}/decisions` 无 symbol 时列有界决策目录；指定 symbol 与 decision_id 或 business_date 时，投影该标的候选→特征→信号→目标→约束→风险→订单→成交证据，同时保留组合级约束。默认100、最多200、offset最多100000，每条投影16KiB护栏；SQL过滤与投影先于Python加载，statement_timeout120秒、客户端150秒。空阶段不猜成交或拒单原因；响应保留 trace_id/checksum。此处交付 #499 必需下钻；#501 的年度诊断、指标分段、可比性和其他扩展仍独立开发。

## 课题与轮次

课题是跨会话持续更新的研究锚点与逻辑证据目录；当前目录由追加轮次的source_refs组成，`#514` 起可用 `/branches` 看分支聚合、`/refs/check` 逐条核验断链、`/context` 一次取回有界续接上下文；`#515` 起课题经 `research_topic_links` 显式关联研究运行，并可用 `GET /api/research/runs/{run_id}/topics` 反向归属（一个 run 可被多个课题引用）；`#516` 起同一链接表还承载因子实验 / 沙箱运行 / OOS 验证实验，并以 `entry_type=plan|review` 区分计划与复核结论。已有运行、记忆和实验用 `entry_type=evidence` 引用关联，保留原产物和历史状态，一个产物可被多个课题引用。完整操作见 [课题组织与已有证据归档](topic-organization.md)。

课题稳定ID `RT-`，含 title/question、goal（version/criteria/source）、status（draft/active/paused/closed）、conclusion（工作结论）、summary/open_questions/next_step。`status` 自 #514 起是真实列（`draft|active|paused|closed`，带 CHECK 约束），payload 内同值双写，读取以列为权威。更新需 expected_revision，旧版本冲突返回409；每次更新追加目标快照，历史15%/20%与当次15%/10%门可分别记录，系统不自行决定新门。草稿没有 goal，编辑草稿不会改变生命周期状态。

轮次稳定ID `RE-`，只追加，记录 goal_version/objective/action/rationale/outcome/conclusion/confidence/next_step、source_refs、branch、supersedes_id。同课题幂等键相同且内容相同返回原记录，内容不同拒绝；纠正链仅能引用同课题已有记录。可记录 completed/failed/interrupted/rejected/paused，不抹掉失败与断续过程。引用精确ID，策略版本需补 version；checksum可选，缺失会限制核验强度。用户创建者由REST固定为user:api，agent由MCP固定为agent:mcp，payload不能伪造创建者。

`entry_type` 自 #516 起有四个记录类型：`round`（执行后记录）、`evidence`（组织已有产物）、`plan`（只记录设计，`outcome` 必须 completed，**永不触发执行**）、`review`（复核结论，必须带 `document_ref`：**相对 `docs/research`** 的 `.md` 路径 + checksum；`/refs/check` 与 `source_fact` 使用同一解析根，`docs/research/rounds/x.md` 应写成 `rounds/x.md`）。`plan` 与 `review` 的 `evidence_level` 分别为 `research_plan` / `reviewed_conclusion`，与人工记录、agent 解释、机器事实并列而不互相覆盖；`goal`/`draft`/`lifecycle` 快照仍单独展示。

引用只是指针，不级联删除或修改研究产物。`GET /api/research/topics/source` 与 `finboard_source_check` 核验存在性、精确版本、checksum，并展示自动事实。断链/版本缺失/校验不符显式呈现；实验事实复用 `derive_oos_outcome`。引用存在不是假设通过。文档路径只允许研究根目录内 `.md`；文档正文以经PR维护的仓库为准。工作摘要、agent轮次与记忆不能自动覆盖正式文档，冲突保留双方来源。

`GET /api/research/topics` 和 `/{topic_id}/entries` 默认20最多50、offset分页；详情 `/{topic_id}`。POST创建、PUT更新、POST `/{topic_id}/entries` 追加。`/memories` SQL截取每条1200字摘录，显示来源、确认、状态与 supersedes_id；选定记忆详情复用既有记忆API。旧故障描述不自动成为当前事实，空/unknown引用提示无法验证。

FinDashboard 保存研究结构，OpenCode 保存原对话；不恢复对话流量代理、不读取凭证、不自动推送文档PR。

记忆列表和完整详情共用有界阅读区及 Markdown 排版；列表只读1200字摘录，关联证据展开时才核查，详情按选定ID读取正文。创建/更新/确认及轮次时间显示人类可读的北京时间，原ISO保留于time.dateTime/title；历史时间缺偏移时明确“时区未记录”，不猜绝对时刻。

## 课题生命周期与续接上下文（#514）

状态机由服务端强制（`finboard_app.research_topic_lifecycle`）：

```text
draft   --activate(需 goal)--> active
active  --pause--> paused --resume--> active
active|paused --close(reason)--> closed --reopen(reason)--> active
draft   --discard(reason)--> closed
```

- 非法转移返回 409，body 为 `{"detail": {"code": "illegal_transition", "allowed": [目标状态...], "actions": [可用动作...], "current": "...", "action": "..."}}`（`allowed` 恒为目标状态，`actions` 为对应动作名）；版本不一致返回 `code=topic_revision_conflict`、`current_revision`、`your_revision`、`changed_fields`、`current_summary`。REST 用结构化 `detail`，MCP 映射为 `conflict`。
- 每次转移要求 `expected_revision`，服务端 `SELECT … FOR UPDATE` + `populate_existing`，revision 不符即拒绝，旧会话不得覆盖新 revision。
- 每次转移在同一事务追加 `entry_type=lifecycle` 轮次 `{action, from, to, reason, actor, at, topic_revision}`；历史轮次与目标快照不改写、不删除。
- 被放弃的草稿没有 goal，`reopen` 会被拒绝（不能产生无目标版本的 active 课题）；重开真实课题保留原目标与轮次。
- 创建模式内部标记 `created_as` 只存于数据库 payload（供幂等键跨模式校验），公开响应（详情/列表/轮次快照）一律剥离，客户端回传完整 payload 不会因未知字段被拒。
- `POST /topics`：带 goal 建 active（原行为不变）；只有 title/question 建 draft（`goal=null`）；`status` 只接受 `active`（`paused/closed` 返回 422，须先建后转移，避免跳过生命周期审计）；可选 `idempotency_key`，同键同内容返回原课题（HTTP 200，`created=false`），同键不同内容或跨创建模式（create ↔ draft_create）返回 409 `idempotency_conflict`。
- `PUT /topics/{id}` 只更新内容，**永不改变状态**：显式携带与当前相同的 `status` 会被忽略（老客户端全量 PUT 仍可用），不同则 409 `illegal_transition`（`allowed` 列出可用的 transitions 动作，提示走 `/transitions`）。非草稿 PUT 必须带 goal（缺失 422）；草稿 PUT 只接受 title/question/conclusion/summary/open_questions/next_step（无 goal，带 goal 返回 422，提示走 `/activate`），revision 乐观锁不变。
- `POST /topics/{id}/activate`：`{goal, expected_revision}`，可附带 summary/open_questions/next_step/conclusion；goal 的 version/criteria/source 必须完整。已 active 且 goal 相同为幂等 200，不增加 revision；goal 不同则 409，不能借激活覆盖目标。
- `POST /topics/{id}/transitions`：`{action: pause|resume|close|reopen|discard, reason, expected_revision}`；close/reopen/discard 必填原因。

`GET /api/research/topics/{id}/context?rounds=10&refs=10` 返回跨客户端续接载荷：`topic`（含 revision/status/updated_at）、`goal`、`lifecycle`（since + 最近一次转移）、`open_questions`、`next_step`、`recent_rounds`（≤20 条；带 `entry_type` 与对应 `evidence_level`；结论/目标/下一步任一超长都会截到 ≤300 字并置 `excerpted=true`）、`active_links`（#515 起为 join 派生的进行中课题→运行链接，最多 10 条，终态链接走 `/runs` 端点）、`experiment_links`（#516 起为因子实验 / 沙箱运行 / OOS 实验链接，最多 10 条，OOS 以 `oos_outcome` 为首要事实）、`entry_types`（按类型计数，区分计划/执行/复核）、`dangling_refs`（对最近轮次引用逐条 `source_fact`，≤10 条/次）与 `dangling_refs_has_more`/`refs_checked`、`evidence_levels`、`next_actions`（服务端派生建议，不代表执行）、`generated_at`、`revision`、`bounds`。总载荷 ≤64 KiB；任何截取都置 `excerpted=true` 并在 `truncated_sections` 具名，绝不静默截断。`rounds` 上限 20，`refs` 上限 10。列表模式的 `excerpted` 只在确实截断（>1200 字或开放问题 >5 项/单项 >300 字）时为 true。

精确查询与聚合：

- `GET /topics/{id}/entries/{entry_id}`：单条全文 + checksum + refs；跨课题 entry_id 返回 404。
- `GET /topics/{id}/branches`：按 `payload.branch` 聚合 `{branch, count, last_at, last_outcome}`，limit ≤50、offset 分页。
- `GET /topics/{id}/refs/check?rounds=10`：对最近 N 轮（≤20）引用去重后逐条调用既有 `source_fact`，返回 `{ref, status, facts, entry_ids}`；每次最多 20 条，`has_more` + `offset` 续页。
- `GET /topics?status=&sort=updated|created`：默认按 `updated_at` 倒序（新列），`sort=created` 按插入 id 倒序。

Web 侧：新建课题分两步（保存草稿 → 补齐目标激活），草稿可单独编辑（无 goal），生命周期按钮带原因与二次确认，详情展示续接上下文卡片与断链提示；表单打开时固定 `base_revision`，后台刷新不会改写提交版本；409 显示结构化冲突（当前 revision、变化字段）并提供“重新加载最新版本”和（非草稿）“仅保留我的说明（追加轮次）”，不静默覆盖。

MCP 冲突返回 `conflict`，并把结构化 detail 以 `| details={json}` 形式附在 message 中（ToolError 无 details 字段），保留 `allowed`/`current`/`current_revision`/`changed_fields`。

## 课题发起研究运行与链接（#515）

`research_topic_links` 是课题与已有研究产物之间的**只追加**关联账本。链接一经创建即不可变，**不写 state/error 列**：运行状态、任务进度与错误在读取时 join `research_runs` / `background_jobs` 派生，因此 worker 重启、租约重排、超时与重复完成事件都不会产生重复运行或重复链接。链接元数据独立于历史 manifest；一个 run 可被多个课题引用，旧独立入口完全不变。

REST：

- `POST /api/research/topics/{topic_id}/runs`：body = 运行入队字段 + `goal_version`（必填）+ 可选 `operation_id`。仅 `status=active` 且 `goal_version` 等于当前目标版本可发起；201 为新建，200 为同 `operation_id` 的幂等重放（返回原 `RR-`/`BJ-`）。
- `GET /api/research/topics/{topic_id}/runs?limit≤50&cursor`：链接 + 派生 `state`（queued/running/succeeded/failed/cancelled/interrupted）、`progress_done/progress_total/phase`、`error_code/error_summary`、`result_checksum`；`next_cursor` 续页。
- `GET /api/research/topics/{topic_id}/runs/{run_id}`：单条链接详情 + `/research/runs?run=RR-` 深链。
- `POST /api/research/topics/{topic_id}/runs/{run_id}/retry`：走既有 replay 生成新 `RR-`（`replay_of_run_id` 指向旧 run）与新链接，原链接保留；必须提供新的 `operation_id`，`goal_version` 继承原链接且仍须匹配当前目标。既有 replay 守卫不变：completed/interrupted 可重放，cancelled/failed 仍按原错误处置。
- `POST /api/research/topics/{topic_id}/runs/{run_id}/cancel`：代理既有协作式取消（非实盘撤单）；run 状态迁移、job `request_cancel` 与 `topic.run.cancel` 审计在同一事务（coordinator checkpoint 以 `commit=False` 延迟提交）；completed/failed/cancelled 已终态返回 409 `run_terminal`。
- `POST /api/research/runs/{run_id}/link`：`{topic_id, goal_version, operation_id}` 后来关联一个已存在的独立运行，幂等；同一课题/运行重复关联且 `operation_id` 相同返回原链接（200）；请求带不同/未绑定 `operation_id` 返回 409 `link_operation_id_conflict`，不静默丢弃该 id。
- `GET /api/research/runs/{run_id}/topics?limit≤100&cursor`：反向查询引用该 run 的全部课题，分页返回 `has_more` / `next_cursor`，不静默截断。

事务与审计：

```text
topic lock + active/goal 校验
→ ResearchRunSubmissionService.submit（不 commit；run + job + run.submit 审计）
→ research_topic_links 插入（不可变）
→ topic.run.start 审计（同 session）
→ 唯一一次 commit（run + job + link + audit 原子生效）
```

任一环节失败整体回滚；拒绝/失败经独立会话补记 `outcome=rejected|failed`，不掩盖原始错误。成功审计 `correlation` 同时含 `topic_id` / `goal_version` / `run_id` / `job_id` / `operation_id`。同 `operation_id` 跨课题复用返回 409 `operation_id_bound`。链接写入失败映射 `link_conflict`，运行参数非法映射 `invalid_run_payload`。

MCP 同步暴露 6 个工具：`finboard_topic_run_start`（research.run）、`finboard_topic_run_list` / `finboard_topic_run_get` / `finboard_run_topics`（research.read）、`finboard_topic_run_retry` / `finboard_topic_run_cancel`（research.run）。后来关联仅 REST（避免把独立入口的写能力扩散到 agent 工具面）。

Web 课题详情增加“发起研究运行”卡片：只选已发布策略与发布要求、可选快照/因子序列、资金与代码版本；展示关联运行的实时状态/进度/错误/结果 checksum，提供重放与取消，深链到 `/research/runs?run=RR-`。保存课题、发起运行与记录轮次是三件事：运行状态是机器事实，轮次是人类/agent 解释，并排展示而不互相替代。

迁移 `9c4f2a7d1e63` 创建独立 `research_topic_links`（无外键、无级联），down_revision=`0d2c51e14b6a`；回滚前导出该表，downgrade 只删表，不删除任何 run/job/课题/轮次/审计。迁移 `b7d1e94c3a26`（#516，down_revision=`9c4f2a7d1e63`）新增 `research_topic_action_receipts`（同样无外键/无级联，唯一键 `(topic_id, operation_id)`）；downgrade 只删表，历史产物与审计保留。

## 课题内因子 / 代码 / OOS 动作与引用（#516）

**证据类型**（`EvidenceRef.kind`；历史 9 类语义不变，新增 5 类）：

| kind | 事实来源 | version | checksum |
|---|---|---|---|
| `factor_experiment` | `factor_experiments` | — | `dataset_release_checksum` |
| `code_artifact` | `research_code_artifacts` | git commit | artifact checksum |
| `code_run` | `research_code_runs`（`RCR-`） | — | `scores_checksum`（输出） |
| `validation_experiment` | `research_experiments` + `research_trials` | — | `version_checksum` |
| `factor_snapshot` | `factor_feature_snapshots` | `framework_version` | 快照 checksum |

`experiment` 继续指向 `research_experiments`，历史引用不迁移。`source_fact` 只做有界投影：`factor_experiment` 仅在 `result` ≤64 KiB 时给 md5 摘要（更大只给字节数与空摘要），`code_run` 只给 checksum/状态不给 artifact 正文，`validation_experiment` 复用 `derive_oos_outcome` 且 trial 超 500 条 / 1 MiB 时返回 `oos_outcome=unknown` + `evidence_gaps=["trial_summary_oversized"]`。`refs/check` 同时核验 review 的 `document_ref`，文档 checksum 漂移即 `checksum_mismatch`。

**白名单动作** `POST /api/research/topics/{id}/actions`（MCP `finboard_topic_action`，工具级 `research.write`）：

| action | 委托 | 所需 scope | 链接 `entity_type` |
|---|---|---|---|
| `run.submit` / `run.replay` | #515（`start_topic_run` / `retry_topic_run`） | `research.run` | `research_run` |
| `factor_experiment.create` | 因子实验登记（只冻结，不执行） | `research.write` | `factor_experiment` |
| `code_run.execute` | `kind=research_code_run` 入队（只能引用已登记 artifact） | `research.run` | `code_run` |
| `validation_experiment.create` / `add_trial` | #57 验证实验 | `research.write` | `validation_experiment` |
| `validation_experiment.unseal` | 既有一次性揭盲执行入队 | `research.run` | `validation_experiment` |

共同不变量：

- 课题必须 `active` 且 `goal_version` 等于当前目标版本；漂移返回 409 `topic_not_active` / `goal_version_mismatch`。
- 每个动作在**同一事务**写产物/任务 + 不可变链接 + 回执 + `topic.action.<action>` 研究审计；失败整体回滚，拒绝/失败独立会话补记。
- `operation_id` 是响应丢失的幂等锚。**链接唯一键 `(topic, entity_type, entity_id)` 不足以锚定同一产物的第二次动作**（例如同一实验的第二次 `add_trial`），因此新增只追加回执表 `research_topic_action_receipts`（迁移 `b7d1e94c3a26`）：
  - 列：`topic_id, operation_id, action, entity_type, entity_id, job_id, result_code, result_summary JSON, created_by, created_at`；`UNIQUE(topic_id, operation_id)`，索引 `(entity_type, entity_id)` / `entity_id` / `topic_id`；无外键无级联。
  - 动作入口先查回执，命中即返回原结果（不重跑、不新增 trial、不消耗预算、不新建沙箱任务）；同 `operation_id` 用于不同 action/entity_type → 409 `operation_id_bound`；并发同键 → 409 `operation_id_in_flight`（重试即命中回执）。
  - `run.submit` / `run.replay` 委托 #515，由其链接 `operation_id` 与 `topic.run.*` 审计负责幂等，**不写**动作回执、也**不重复**写 `topic.action.run.*` 审计；active/goal 门同样由 #515 校验（拒绝只留一条 `topic.run.start`/`topic.run.retry`）。
- 响应区分 `artifact_created`（本次真的新建了产物/任务/trial）与 `link_created`（本次新建了链接），并附 `replayed`；REST 仅当两者皆 false（纯回执重放）时返回 200，否则 201。
- `code_run.execute` 的请求模型 `extra="forbid"` 且没有任何代码字段；**所有 action**（含 `run.submit`/`add_trial`）的 payload 出现 `code` / `source_code` / `python` / `module` / `module_path` / `expression` / `expr` / `script` 键一律 422 `code_channel_forbidden`。代码提交只在 MCP `finboard_research_code_submit`，Web 只引用已登记 artifact。
- 重复揭盲返回 409 `already_unsealed`，且不改变 `final_test_unsealed`；揭盲仍是一次性门，重做必须新建实验（`supersedes_id`）。
- **动作永不自动执行**：保存计划/复核条目不会产生 run/job；只有显式提交动作才入队。

**读取**：`GET /topics/{id}/artifacts?limit≤50`（MCP `finboard_topic_read(view=artifacts)`）返回链接 + 读取时派生的 `state`、`status`、`checksum`、`evidence_gaps`；因子实验额外给 `result_bytes` / `result_checksum`（≤64 KiB 才给摘要）/ 小体积 `metrics`；OOS 链接额外给 `oos_outcome`（首要）、`trial_budget{used,limit}`、`final_test_unsealed`。失败、拒绝、中断与成功同列展示，不折叠；`code_run` 链接在 worker 生成 `RCR-` 前以入队 `job_id` 为 `entity_id`（`job_id` 列同值），读取时按 job 派生运行记录与缺口 `run_not_created`。

**Web**：课题详情新增“因子实验 / 沙箱运行 / OOS 实验”卡片：OOS 卡先显示 `oos_outcome`，再显示流程状态、试验预算与“已揭盲，不可重做”；未揭盲且课题 active 时提供“显式执行一次性揭盲”按钮，重复揭盲显示 409 文案。因子实验卡显示数据发布 checksum、结果摘要 checksum/字节数与指标（体量受限）、失败原因。轮次表单区分“记录新轮次（执行后）/ 只保存计划（不执行）/ 记录复核结论”，复核必须填**相对 `docs/research`** 的 `.md` 路径与 checksum。卡片是只读展示 + 显式动作，不提供因子实验/沙箱创建表单（这些走 FactorLab 或 MCP）；Web 源码不调用 `finboard_research_code_submit`（有 grep 守卫测试）。

## 工具同步、验证与回滚

MCP工具与上述服务共用实现：topic_read、topic_write、memory_page、source_check、strategy_explain、decision_explain。`finboard_topic_read` 增加 `view=context|branches|refs` 与 `entry_id`，`finboard_topic_write` 增加 `operation=draft_create|activate|transition`；旧参数与行为不变。具体参数与完整例子见 Skill references/tools.md。MCP写工具受 readonly_only 拒绝，输入脱敏、审计，不注册任何实盘能力。

迁移 `62f8b1e7a905` 创建独立 research_topics/research_topic_entries，无研究产物/实盘外键或级联；回滚前导出这两表，再downgrade。`a7c41e9b5d32`（#514）为 research_topics 增加 status/updated_at/idempotency_key、部分唯一索引与 (status, updated_at DESC) 索引，并从 payload.status 回填。downgrade 会**先导出**：把列值折回 payload.status 后再删除新列/索引/CHECK 约束——`idempotency_key`、`updated_at` 与按状态索引的查询能力会随之丢失，回滚后不能靠 downgrade 恢复，须先 `pg_dump`/导出。回滚不会删除任何 run、dataset、factor、memory 或模拟账本。执行差异与压力验证仍由 #502/#503 处理。

本轮测试覆盖日历/OOS旧契约、方向翻转、冻结参数/版本缺证据、同源来源核验、目标与成交不一致、分页/载荷护栏、跨轮目标更新/纠正/幂等、路径包含、权限/创建者边界及迁移往返。phase1_doc.md §3.4 的3–14仅作为研究域产物关联与决策/账本解释映射，不替代实盘恢复/券商验收。

载荷边界：课题列表中的问题/摘要/下一步各截取1200字、开放问题最多5项各300字，`excerpted=true`，精确课题详情保留全文；记忆正文1200字、refs超过16KiB具名refs_truncated且不可验证；说明书manifest加载前1MiB上限、因子params16KiB，超限明确缺证据。实验来源核查在加载试验前检查总数不超过500、总载荷不超过1MiB，超限返回结论未知及具名缺口。目标版本定义不可改写，轮次须引用课题已归档目标版本。
