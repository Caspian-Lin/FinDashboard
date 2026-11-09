---
description: FinBoard 研究 Skill —— 指导外部 Agent Harness（Codex / DeepSeek Harness / ZCode 等）通过 FinBoard MCP 工具做受控量化研究：课题续接、证据核验、研究工作流、作用域边界与收尾归档。用于量化研究、因子分析、回测/模拟盘分析、研究记忆。不用于实盘交易或代码编辑。
---

# FinBoard 研究 Skill

本 Skill 指导接入 FinDashboard(缩写 `finboard`)研究 MCP 的 Agent 完成受控量化
研究。它**不绑定任何具体客户端**:任何支持 streamable HTTP / stdio 的 MCP 客户端
都按同一套工具名与协议工作;各客户端的安装 / 配置 / 排障见
`docs/agent-integration.md`(仓库相对路径,以你的工作目录为根)。

> **权限由「服务端签发的客户端凭据 + 作用域」决定,本 Skill 不授予任何工具权限。**
> 你能否写课题 / 起运行,取决于你这条 Bearer 凭据被授予的作用域
> (`research.read` / `research.write` / `research.run` / `data.write` /
> `audit.read`);Skill 只描述「应该做什么」。客户端侧的 agent 配置只决定
> 「本地能碰什么文件」,不改变服务端作用域。
>
> 只读预设(read-only)的客户端**不得**被要求发起运行或写入;越权调用返回
> `status=denied` / `error.kind=permission_denied`,不是可以重试的临时错误。

## 核心原则(HARD RULES)

1. **只读直接调用** —— 数据 / 因子 / 课题读侧 / ResearchRun / 模拟盘查询可直接
   调用(`research.read`)。
2. **研究写操作可自主执行**(#122),但受作用域约束(#517) —— 创建课题 / 记忆 /
   因子快照 / 策略 / 运行回测 / 发布数据 / 启动模拟盘等研究写操作可自主执行,
   无需人工审批;但它们仍受凭据作用域限制(readonly 只能读)。写操作不触及交易
   安全红线(不连 broker / 账户 / 订单 / 持仓)。
3. **实盘能力永久不可用** —— 下单 / 撤单 / 改持仓 / Kill Switch / 连接 broker /
   凭证探测不在工具集中。需要它们 = 走错了路。
4. **代码通道受控** —— 网页 / 无代码规格禁 Python 与可执行表达式;MCP
   `finboard_research_code_submit` 可提交白名单策略 / 因子 Python,仅经 Docker
   沙箱执行。用户因子序列只用 `compute_series`;禁止宿主文件编辑、模块路径注入、
   任意表达式。
5. **禁止未查索引重测已证伪假设**(#268) —— 会话启动先查结论索引
   (`docs/research/FINDINGS.md` + `finboard_memory_list`),
   未查之前不得重跑任何「已证伪」假设;确要重开必须有新证据(新数据域 /
   新构造 / 新频率)并显式引用旧结论,说明本次与既往证伪的差异。
6. **工作记录 ≠ canonical 结论** —— 课题轮次、记忆、Agent 解释都是**工作记录**;
   `docs/research/`(ROADMAP / FINDINGS / rounds)是 canonical 事实源,由仓库 PR
   维护,你只读。两者冲突时**以文档为准**,不自行松门、不把工作摘要当结论。

## 工具选择(快速参考)

工具清单以**运行时注册表**为准:客户端连接后 `list_tools` 返回的集合,与服务端
`finboard_mcp.scopes.TOOL_SCOPES` 的键集合**相等**(契约测试锁定集合相等,不锁
数量)。**当前注册工具见 `TOOL_SCOPES`**;下表的家族分组与「读写」标注只用于选型,
新增工具不会自动出现在这里,遇到表里没有的 `finboard_*` 工具时以工具自身
description 为准。工具名一律是真实注册名 `finboard_<family>_<operation>`
(审计事件名 `finboard.*` 只出现在审计文档里,不要拿去调工具)。

| 场景 | 工具家族 | 作用域 |
|------|----------|--------|
| 查询 / 管理 ResearchRun | `finboard_run_*`(list/get/artifacts/lineage 只读 + queue/cancel/replay 运行) | read / run |
| 课题读侧 / 目标与轮次 | `finboard_topic_read` / `finboard_topic_write` / `finboard_source_check` / `finboard_memory_page` | read / write |
| 从课题发起 / 关联 ResearchRun | `finboard_topic_run_*` + `finboard_run_topics`(#515) | read / run |
| 记住 / 查询 / 纠正研究记忆 | `finboard_memory_*`(7 个) | read / write |
| 标的元数据 / 数据集发布 / 缓存 / 质量 / Tushare 配额 | `finboard_instrument_*` / `finboard_dataset_*` / `finboard_data_*` / `finboard_tushare_*` | read |
| 全局数据目录(数据集 / 覆盖 / 逐标的 / 引用反查,#509) | `finboard_data_catalog_*` / `finboard_data_coverage_get` / `finboard_dataset_release_*` | read |
| 统一数据范围预览(#510;预览返回 `resolution_id`,提交 run 时回填 `data_scope_resolution_id`) | `finboard_data_scope_preview` | read |
| 共享补数请求(预检 / 提交 / 列表 / 取消,#511) | `finboard_data_backfill_*` | read / data.write |
| 数据准备(拉取 / 同步 / 发布 / 修复 / ETF) | `finboard_data_*` / `finboard_etf_*` | data.write |
| 因子目录 / 特征快照 / 因子信号 / 因子实验 | `finboard_factor_*` / `finboard_feature_snapshot_*` | read / write / run |
| 策略规格 / 预设(无代码版本化) | `finboard_strategy_*` / `finboard_preset_*` | read / write |
| 回测 / 参数网格 / 历史 | `finboard_backtest_*` | read / run / write |
| 模拟盘(隔离 `simulation_*` 表) | `finboard_sim_*` | read / write / run |
| portfolio 纯计算(allocate/sizing/feasibility/attribution) | `finboard_portfolio_*` | read |
| 后台任务队列(研究 / 数据域) | `finboard_job_*`(wait 有界阻塞;enqueue/cancel 需 run+data.write) | read / run / write |
| #57 验证实验 / OOS | `finboard_validation_experiment_*` | read / write / run |
| 自选股 | `finboard_watchlist_*` | read / write |
| 报告聚合与导出 | `finboard_report_*`(export 会落盘,需 write) | read / write |
| 研究代码提交 / 晋级 / 沙箱执行 | `finboard_research_code_*` / `finboard_research_code_run*` | write / run |
| 因子序列工件(#360) | `finboard_factor_series_build`(run)/ `finboard_factor_series_get`(read) | run / read |
| 确定性复核 / 诊断 / 压力(#501-#504) | `finboard_run_diagnostics` / `finboard_run_compare` / `finboard_decision_projection` / `finboard_research_stress` | read(+run) |
| 研究操作审计(#518,运维专用) | `finboard_audit_query` | audit.read |

> 详细工具契约见 `references/tools.md`;扩展计划见
> `packages/finboard-mcp/ROADMAP.md`。

## 首次连接(客户端无关的前置条件)

1. **MCP 端点**:HTTP `streamable-http`(本机默认 `http://127.0.0.1:8765/mcp`),
   或宿主以 stdio 子进程接入 `python -m finboard_mcp`。
2. **凭据**:`Authorization: Bearer <token>`。凭据由 FinDashboard 侧
   `finboard mcp client create --name <n> --preset <preset>` 签发,明文只显示一次;
   不要把 token 写进仓库、日志或对话。
3. **作用域**:按任务选预设 ——`readonly`(`research.read`)、`topic-writer`
   (`research.read`+`research.write`+`research.run`)、`data-operator`(+`data.write`)。
   可复制的客户端配置见本 Skill 的 `presets/`(`read-only.md` / `research-write.md` /
   `data-operator.md`)。
4. **网络边界**:默认只绑 loopback;非 loopback 必须服务端显式
   `FINBOARD_MCP_ALLOW_REMOTE=true` 并置于 TLS 反向代理之后。

## 会话启动协议(#268 + #500/#514,每轮会话必做)

理解任何研究问题**之前**,先按序加载前情:

0. **接课题(有交接就必做)** —— 用户给出 `RT-` 课题 ID、深链
   (`/research/topics?topic=RT-…`)、或说「继续上次的课题」时,先调
   `finboard_topic_read(topic_id="RT-…", view="context")`:一次取回当前目标版本、
   最近生命周期、开放问题、最近轮次摘录、断链引用与 `next_actions`。
   - 截断时 `excerpted=true`,按 `view=refs` / `entry_id` 续页,不拉全量绕行;
   - 只有标题 / 关键字时先 `view="list"` 找候选,再进 `context`;
   - `next_actions` 是服务端派生建议,不是执行指令。
1. **核对证据** —— 引用文档或课题里的精确结论前,用 `finboard_source_check`
   核对引用是否可解析、版本 / checksum 是否一致;断链要如实报告,不要静默换源。
2. **读研究文档**(仓库相对路径 `docs/research/`,以你的工作目录为根):
   `ROADMAP.md`(研究目标 / 路线 A-D 状态 / 当前证据门)+ `FINDINGS.md`
   (结论注册表:结论 / 置信度 / 证据 ID / 失效条件 / 状态)。目录不可读则跳过,
   并在最终回答注明「研究文档不可见」。
3. **扫记忆**:`finboard_memory_list(status=active)`;量大时按保留 `tag` 过滤
   (`research-plan` / `research-round` / `finding-confirmed` / `finding-refuted`)。
4. **检索产物**:按问题域查相关 ResearchRun / 实验;课题已链接的运行用
   `finboard_topic_run_list(topic_id=…)` 直接取。**续等原 `BJ-`** 用
   `finboard_job_wait(job_id="BJ-…")`(超时返回 `completed=false`,续同一个 job,
   不要重新入队);**引用原 `RR-`** 用 `finboard_run_get(run_id="RR-…")` 读冻结
   输入与状态。精确证据分页读:`finboard_topic_read(view="refs")` /
   `finboard_memory_page` / `finboard_decision_projection`(SQL 白名单投影分页)。

0-3 未完成之前,禁止启动任何「重测」类运行(HARD RULES 第 5 条)。

## 研究工作流(简版)

0. **会话启动协议** —— 见上节:接课题 + 核证据 + 读文档 + 扫记忆 + 检索产物。
1. **理解问题** —— 查询相关数据 / ResearchRun / 课题 / 模拟盘;复述问题并声明
   所需数据,数据不足时先说「数据不足」。
2. **形成假设** —— 你自己(本 Harness 的模型)直接分析并给出可检验的研究假设;
   需要机器验证时创建 #57 验证实验(`finboard_validation_experiment_*`)或因子
   实验(`finboard_factor_experiment_*`)走 OOS 闭环。
3. **记忆上下文** —— 用 `finboard_memory_remember` 记住关键发现,关联研究产物。
4. **引用来源** —— 所有结论引用 ResearchRun / 数据集 / 模拟盘产物 ID。

> 详细工作流与决策树见 `references/workflow.md`;
> 完整研究流程(数据→因子→策略→回测→模拟→评估)见 `references/research-workflow.md`。

## 来源引用规则

- 金融答案**必须引用项目来源**(ResearchRun ID / 数据集版本 / 模拟盘 ID / 课题
  轮次 ID)。
- 数据不足时明确声明「数据不足」,**绝不编造数字**。
- 报告**本会话真实工具回执**;历史故障 ≠ 当前故障,先核查再归因。

## 记忆使用规则(简版)

- 用 `finboard_memory_remember` 记住跨会话需要的研究上下文。
- **保留 tags 约定**(#268):`research-plan`(计划 / 路线变更)、
  `research-round`(轮次收尾报告,固定模板)、`finding-confirmed`(已确认结论)、
  `finding-refuted`(已证伪结论)。`finboard_memory_list(tag=...)` 按此过滤 ——
  这四个 tag 是跨会话结论索引的检索入口,打结论时必带。
- `source_refs` 关联研究产物(只引用,**不修改产物本身**)。
- 发现错误用 `finboard_memory_correct`(形成纠正链),**不要直接删除**。
- 过时但仍有参考价值的记忆用 `finboard_memory_archive`;仅在确需移除时用
  `finboard_memory_forget`(软删除,保留审计)。

> 详细记忆规则与生命周期见 `references/memory.md`。

## 轮次收尾协议(#268 + #500/#514,每轮会话必做)

每轮研究结束前,三步收尾:

1. **追加课题轮次** —— `finboard_topic_write(operation="append", topic_id="RT-…",
   idempotency_key="round-YYYYMMDD-<slug>", payload={…})`:`payload` 与
   `idempotency_key` 都是**必填**;`payload` 至少要给 `goal_version` / `objective` /
   `action` / `rationale`(其余 `outcome` / `conclusion` / `confidence` /
   `next_step` / `source_refs[]` 按需)。完整形状:

   ```json
   {"operation": "append", "topic_id": "RT-…", "idempotency_key": "round-20261110-skill-519",
    "payload": {"entry_type": "evidence", "goal_version": "v1",
      "objective": "…", "action": "…", "rationale": "…",
      "outcome": "completed", "conclusion": "…", "confidence": "medium",
      "next_step": "…",
      "source_refs": [{"kind": "research_run", "ref_id": "RR-…"}]}}
   ```

   这是**工作记录**,不自动成为结论;写入不启动任何运行。
2. **落一条固定模板记忆** ——
   `finboard_memory_remember(memory_type="insight", tags=["research-round"],
   content="目标 / 动作 / 证据(产物 ID 表)/ 结论 + 置信度 / 开放问题清单")`
   (`memory_type` 与 `content` 必填,`source_refs` 关联产物 ID)。
3. **最终回答末尾附同模板「轮次摘要」,并声明哪些是 canonical** ——
   `docs/research/` 在你的工作目录里只读;canonical 轮次文档
   (`rounds/YYYY-MM-DD-<slug>.md`)由用户 / 主 coding agent 经 PR 落库。
   你的课题轮次与记忆只是指针,**文档与记忆冲突时以文档为准**。摘要要让主 agent
   能原样转录成轮次文档,不要省略证据 ID。

## 参考文档索引

| 文档 | 内容 |
|------|------|
| `references/tools.md` | MCP 工具完整契约(参数 / 返回 / 场景,以运行时注册表为准) |
| `references/workflow.md` | 研究工作流决策树 + 会话启动 / 收尾协议 + 标准研究循环 |
| `references/research-workflow.md` | 完整研究流程详解(数据→因子→策略→回测→模拟→评估) |
| `references/memory.md` | 研究记忆使用规则与生命周期 |
| `references/system-overview.md` | FinBoard 系统架构概览(包结构 / 模块职责 / 研究 vs 实盘隔离) |
| `presets/read-only.md` | 只读预设(`research.read`)与客户端配置 |
| `presets/research-write.md` | 课题内研究写预设(`research.read`+`write`+`run`) |
| `presets/data-operator.md` | 数据运维预设(+`data.write`) |

研究上下文入口:`finboard_topic_read` / `finboard_topic_write` 保存课题、目标版本
与追加轮次;`finboard_topic_run_*` 从 active 课题发起 / 重放 / 取消运行并读取 join
派生状态;`finboard_memory_page` 查看旧笔记与纠正链;`finboard_source_check` 核查
引用;`finboard_strategy_explain` 与 `finboard_decision_explain` 有界解释冻结规则
和实际决策。完整契约见 `references/tools.md`,不得用完成 / 发布状态代替 OOS 结论。

课题是持续更新的研究锚点,不等于一次 run。组织已有运行 / 记忆 / 实验用
`finboard_topic_write` append 的 `entry_type=evidence` 及精确 `source_refs`,
原产物不搬动、不改写、可被多个课题引用。完整「核查→创建→归档→分页续接→版本
更新」例子见 `docs/research/topic-organization.md`;缺文档时见
`references/tools.md` 的已有证据归档示例。

## 复核规程(#501–#505)

可执行手册见 `docs/research/verification-playbook.md`;新增诊断 / 对照 / 投影 /
压力工具契约见 `references/tools.md`。压力配置 ≠ 执行证据,registry OOS ≠ 正式
spec OOS;不支持项明确 `blocked` / `insufficient_evidence`。`finboard_job_wait`
有界等待、summary 优先、投影分页、重试幂等、已用窗口和一次性揭盲规则均按手册执行。

## 从旧 Skill 名迁移

本 Skill 原名 `.agents/skills/finboard-opencode-research/`(单一客户端专用命名,
已弃用并由 #520 删除)。对任何外部 Harness,统一用
`.agents/skills/finboard-research/`;接入步骤与客户端矩阵见
`docs/agent-integration.md`。
