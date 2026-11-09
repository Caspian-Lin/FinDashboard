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

课题是跨会话持续更新的研究锚点与逻辑证据目录；当前目录由追加轮次的source_refs组成，`#514` 起可用 `/branches` 看分支聚合、`/refs/check` 逐条核验断链、`/context` 一次取回有界续接上下文（仍无 run→topic 反向归属，留待 #515 链接表）。已有运行、记忆和实验用 `entry_type=evidence` 引用关联，保留原产物和历史状态，一个产物可被多个课题引用。完整操作见 [课题组织与已有证据归档](topic-organization.md)。

课题稳定ID `RT-`，含 title/question、goal（version/criteria/source）、status（draft/active/paused/closed）、conclusion（工作结论）、summary/open_questions/next_step。`status` 自 #514 起是真实列（`draft|active|paused|closed`，带 CHECK 约束），payload 内同值双写，读取以列为权威。更新需 expected_revision，旧版本冲突返回409；每次更新追加目标快照，历史15%/20%与当次15%/10%门可分别记录，系统不自行决定新门。草稿没有 goal，编辑草稿不会改变生命周期状态。

轮次稳定ID `RE-`，只追加，记录 goal_version/objective/action/rationale/outcome/conclusion/confidence/next_step、source_refs、branch、supersedes_id。同课题幂等键相同且内容相同返回原记录，内容不同拒绝；纠正链仅能引用同课题已有记录。可记录 completed/failed/interrupted/rejected/paused，不抹掉失败与断续过程。引用精确ID，策略版本需补 version；checksum可选，缺失会限制核验强度。用户创建者由REST固定为user:api，agent由MCP固定为agent:mcp，payload不能伪造创建者。

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

`GET /api/research/topics/{id}/context?rounds=10&refs=10` 返回跨客户端续接载荷：`topic`（含 revision/status/updated_at）、`goal`、`lifecycle`（since + 最近一次转移）、`open_questions`、`next_step`、`recent_rounds`（≤20 条；结论/目标/下一步任一超长都会截到 ≤300 字并置 `excerpted=true`）、`active_links`（#515 前恒为空）、`dangling_refs`（对最近轮次引用逐条 `source_fact`，≤10 条/次）与 `dangling_refs_has_more`/`refs_checked`、`evidence_levels`、`next_actions`（服务端派生建议，不代表执行）、`generated_at`、`revision`、`bounds`。总载荷 ≤64 KiB；任何截取都置 `excerpted=true` 并在 `truncated_sections` 具名，绝不静默截断。`rounds` 上限 20，`refs` 上限 10。列表模式的 `excerpted` 只在确实截断（>1200 字或开放问题 >5 项/单项 >300 字）时为 true。

精确查询与聚合：

- `GET /topics/{id}/entries/{entry_id}`：单条全文 + checksum + refs；跨课题 entry_id 返回 404。
- `GET /topics/{id}/branches`：按 `payload.branch` 聚合 `{branch, count, last_at, last_outcome}`，limit ≤50、offset 分页。
- `GET /topics/{id}/refs/check?rounds=10`：对最近 N 轮（≤20）引用去重后逐条调用既有 `source_fact`，返回 `{ref, status, facts, entry_ids}`；每次最多 20 条，`has_more` + `offset` 续页。
- `GET /topics?status=&sort=updated|created`：默认按 `updated_at` 倒序（新列），`sort=created` 按插入 id 倒序。

Web 侧：新建课题分两步（保存草稿 → 补齐目标激活），草稿可单独编辑（无 goal），生命周期按钮带原因与二次确认，详情展示续接上下文卡片与断链提示；表单打开时固定 `base_revision`，后台刷新不会改写提交版本；409 显示结构化冲突（当前 revision、变化字段）并提供“重新加载最新版本”和（非草稿）“仅保留我的说明（追加轮次）”，不静默覆盖。

MCP 冲突返回 `conflict`，并把结构化 detail 以 `| details={json}` 形式附在 message 中（ToolError 无 details 字段），保留 `allowed`/`current`/`current_revision`/`changed_fields`。

## 工具同步、验证与回滚

MCP工具与上述服务共用实现：topic_read、topic_write、memory_page、source_check、strategy_explain、decision_explain。`finboard_topic_read` 增加 `view=context|branches|refs` 与 `entry_id`，`finboard_topic_write` 增加 `operation=draft_create|activate|transition`；旧参数与行为不变。具体参数与完整例子见 Skill references/tools.md。MCP写工具受 readonly_only 拒绝，输入脱敏、审计，不注册任何实盘能力。

迁移 `62f8b1e7a905` 创建独立 research_topics/research_topic_entries，无研究产物/实盘外键或级联；回滚前导出这两表，再downgrade。`a7c41e9b5d32`（#514）为 research_topics 增加 status/updated_at/idempotency_key、部分唯一索引与 (status, updated_at DESC) 索引，并从 payload.status 回填。downgrade 会**先导出**：把列值折回 payload.status 后再删除新列/索引/CHECK 约束——`idempotency_key`、`updated_at` 与按状态索引的查询能力会随之丢失，回滚后不能靠 downgrade 恢复，须先 `pg_dump`/导出。回滚不会删除任何 run、dataset、factor、memory 或模拟账本。执行差异与压力验证仍由 #502/#503 处理。

本轮测试覆盖日历/OOS旧契约、方向翻转、冻结参数/版本缺证据、同源来源核验、目标与成交不一致、分页/载荷护栏、跨轮目标更新/纠正/幂等、路径包含、权限/创建者边界及迁移往返。phase1_doc.md §3.4 的3–14仅作为研究域产物关联与决策/账本解释映射，不替代实盘恢复/券商验收。

载荷边界：课题列表中的问题/摘要/下一步各截取1200字、开放问题最多5项各300字，`excerpted=true`，精确课题详情保留全文；记忆正文1200字、refs超过16KiB具名refs_truncated且不可验证；说明书manifest加载前1MiB上限、因子params16KiB，超限明确缺证据。实验来源核查在加载试验前检查总数不超过500、总载荷不超过1MiB，超限返回结论未知及具名缺口。目标版本定义不可改写，轮次须引用课题已归档目标版本。
