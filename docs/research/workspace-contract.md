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

课题稳定ID `RT-`，含 title/question、goal（version/criteria/source）、status（active/paused/closed）、conclusion（工作结论）、summary/open_questions/next_step。更新需 expected_revision，旧版本冲突返回409；每次更新追加目标快照，历史15%/20%与当次15%/10%门可分别记录，系统不自行决定新门。

轮次稳定ID `RE-`，只追加，记录 goal_version/objective/action/rationale/outcome/conclusion/confidence/next_step、source_refs、branch、supersedes_id。同课题幂等键相同且内容相同返回原记录，内容不同拒绝；纠正链仅能引用同课题已有记录。可记录 completed/failed/interrupted/rejected/paused，不抹掉失败与断续过程。引用精确ID，策略版本需补 version；checksum可选，缺失会限制核验强度。用户创建者由REST固定为user:api，agent由MCP固定为agent:mcp，payload不能伪造创建者。

引用只是指针，不级联删除或修改研究产物。`GET /api/research/topics/source` 与 `finboard_source_check` 核验存在性、精确版本、checksum，并展示自动事实。断链/版本缺失/校验不符显式呈现；实验事实复用 `derive_oos_outcome`。引用存在不是假设通过。文档路径只允许研究根目录内 `.md`；文档正文以经PR维护的仓库为准。工作摘要、agent轮次与记忆不能自动覆盖正式文档，冲突保留双方来源。

`GET /api/research/topics` 和 `/{topic_id}/entries` 默认20最多50、offset分页；详情 `/{topic_id}`。POST创建、PUT更新、POST `/{topic_id}/entries` 追加。`/memories` SQL截取每条1200字摘录，显示来源、确认、状态与 supersedes_id；选定记忆详情复用既有记忆API。旧故障描述不自动成为当前事实，空/unknown引用提示无法验证。

FinDashboard 保存研究结构，OpenCode 保存原对话；不恢复对话流量代理、不读取凭证、不自动推送文档PR。

## 工具同步、验证与回滚

MCP工具与上述服务共用实现：topic_read、topic_write、memory_page、source_check、strategy_explain、decision_explain。具体参数与完整例子见 Skill references/tools.md。MCP写工具受 readonly_only 拒绝，输入脱敏、审计，不注册任何实盘能力。

迁移 `62f8b1e7a905` 创建独立 research_topics/research_topic_entries，无研究产物/实盘外键或级联；回滚前导出这两表，再downgrade。回滚不会删除任何 run、dataset、factor、memory 或模拟账本。执行差异与压力验证仍由 #502/#503 处理。

本轮测试覆盖日历/OOS旧契约、方向翻转、冻结参数/版本缺证据、同源来源核验、目标与成交不一致、分页/载荷护栏、跨轮目标更新/纠正/幂等、路径包含、权限/创建者边界及迁移往返。phase1_doc.md §3.4 的3–14仅作为研究域产物关联与决策/账本解释映射，不替代实盘恢复/券商验收。

载荷边界：课题列表中的问题/摘要/下一步各截取1200字、开放问题最多5项各300字，`excerpted=true`，精确课题详情保留全文；记忆正文1200字、refs超过16KiB具名refs_truncated且不可验证；说明书manifest加载前1MiB上限、因子params16KiB，超限明确缺证据。实验来源核查在加载试验前检查总数不超过500、总载荷不超过1MiB，超限返回结论未知及具名缺口。目标版本定义不可改写，轮次须引用课题已归档目标版本。
