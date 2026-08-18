# AGENTS.md

可实盘交易的量化系统。实盘交易涉及真实资金，改动须谨慎，不要在未确认风险的情况下改动下单 / 持仓 / 风控相关逻辑。

## 命名约定

- **全名**:`FinDashboard`(大写 F、D、S)—— 用于文档标题、对外说明、正式表述。
- **缩写**:`finboard`(全小写)—— 用于包目录名(`packages/finboard-*`)、Python 模块名(`finboard_*`)、CLI 命令(`finboard`)、内部叙述。
- **禁止变体**:`Findashboard`、`FINBOARD`(全大写,除非是环境变量前缀 `FINBOARD_`)、`Finboard`(仅首字母大写)等。
- **历史遗留标识符(保持不变,不视作违规)**:
  - `pyproject.toml` 的 `name = "findashboard"`(分发包名,改影响大);
  - PostgreSQL 默认库名 / 用户名 `findashboard`(用户已按此建库,见 `README.md` 数据库初始化);
  - 前端 `web/package.json` 的 `name = "findashboard-web"`、localStorage key `findashboard-theme`(已发布的标识符)。
  这些是技术标识符,不是叙述用词;新代码 / 新文档应遵循上方的全名 / 缩写约定。

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
- **当前方向：回测 + 策略开发** —— QMT 权限受阻,实盘验证暂缓（#22/#24/#26）。转向历史数据源（#27 akshare/tushare）、回测引擎（#28 行情回放 + 纸面撮合 + 绩效分析）、第一个信号驱动策略（#29 均线交叉）。
- **研究里程碑收尾（#66/#77-#81/#83/#91）**：研究数据发布、因子实验室、无代码策略规格、统一 `ResearchRun` 生命周期、正式组合风控流水线和持久化产品模拟盘均已建立；研究运行使用独立表与 `RR-` ID，模拟运行使用 `simulation_*` 表与 `SIM-*` ID，均不得写入实盘订单/成交/持仓。前端暂不提供 Python 策略代码。
- **多期再平衡回放（#183）**：research_run 支持 `parameters.rebalance_frequency`（monthly|quarterly）从冻结发布交易日历推导每期 decision date，每期重算 universe/features/signals 与组合，决策间每日 mark-to-market 产出全区间权益曲线与绩效指标（总收益/年化/夏普/最大回撤）；返回与 report 标注 `execution_mode`（single_shot|multi_period）。多期不要求预建因子快照（价格因子按发布每日重算），未声明或非法值一律按 single_shot 单快照路径处理，行为不变；纯离线研究域，不连 broker 不下单。
- **指数基准行情（#184）**：akshare 指数日线（`000300.SH` 等）按代码规则分流 `index_zh_a_hist`，指数 bar 可缓存、可发布（INDEX 放行发布通道，`mult_asset_mixed` 发布至少含 stock/etf/index 之一，指数不可撮合只做基准数据）。事件驱动回测：MCP `finboard_backtest_run` 支持 `benchmark_symbol`、REST `BacktestRunRequest.benchmark`，透传 `BacktestConfig`，引擎在基准不在 universe 时单独拉取基准 bars。research_run 管线按 `benchmark_config.symbol` 从冻结发布取行情计算真实 `benchmark_return`/`excess_return`（不再读手工 `overrides.return` 默认 0.0）。**基准缺失时两条路径均返回 `benchmark_return=null` + 具名 warning，禁止静默 0.0**；`backtest_runs.benchmark_config` 如实归档。纯离线研究域，不连 broker 不下单。
- **instrument 元数据回填（#185）**：akshare 发现链路只写 `instruments` 的 code/name/market/instrument_type/exchange/listing_board，list_date/industry 无写入者；tushare `stock_basic` 的 list_date+industry 落在 `research_instrument_profiles`。data_sync 后置从最近一次已发布 profiles 批次回填（只回填 null，不覆盖已有主数据）；发布构造器在 instruments 字段为 null 时从 profiles 兜底，release instruments 携带 list_date/industry（manifest 新增 `industry` 字段）；sync/发布任务报告缺失字段统计（`quality_report.instrument_metadata` / job phase），缺失可见而非静默。`sector` 尚无上游来源，保持 null。纯离线数据域，不连 broker 不下单。
- **universe 预检与候选池诊断（#186）**：`strategy_validate`（REST+MCP）返回 `universe_precheck`——对主数据发布的 instruments 做静态评估，检查 universe 过滤条件依赖字段（list_date / delist_date / average_amount / ST 标记 / required_data_fields / ranking.field）存在性，缺失返回具名 warning，并给出候选池空池预览（total/included/排除统计 `excluded_by_condition`/缺失字段 `missing_fields`）；`run_queue`（REST+MCP，含 backtest_run 的 strategy_spec 形态）入队同步做候选池非空校验，空池秒级 `invalid_argument`（附排除统计与缺失字段名），杜绝「list_date 全 null → listing_days=0 → 全排除」后等执行期跑 30+ 分钟才报泛化错误；signal_engine 执行期对「因缺元数据未生效」的过滤发出运行时降级 warning，空池错误附根因字段。价格/特征类字段（min_price 等）运行时恒有，静态预览不误报空池。纯离线研究域，不连 broker 不下单。
- **冻结发布多数据集联合发布（#187）**：`dataset_release_publish`（REST+MCP）的 `release_kind` 扩为 4 值——在 `a_share_tushare` / `multi_asset_mixed` 之上新增 `daily_metrics` / `financial_indicators`：从 `research_*` 表（research_data_sync 摄取，`source=tushare` 且 `adjustment=none`）按字段白名单冻结 parquet（每标的分 kind 目录），质量门为覆盖率 + 元数据完整（`all_null_fields` 仅 as 可见 warning，财务指标稀疏是常态），同名 dataset 字段变化递增 `schema_version`，`research_*` 记录的 `available_at`/`observed_at`/`source` 完整归档。联合消费：因子快照可引用 bars 主发布 + research 发布联合集，产出 pb / 市值 / 换手 / ROE / 毛利率等基本面因子（`build_cross_section_feature_snapshot_from_releases`，`market_cap` 已注册为 `FACTOR_LAB_CATALOG` 的 `signal_eligible=False` 规模特征）；`strategy_validate` / `run_queue` / frozen_loader 的 `dataset_release_ids` 可同时引用多份发布，universe 预检与候选池始终落在 **bars 主发布**（研究数据发布只提供因子观测）；`FrozenReleaseProvider.fetch_daily_metrics` / `fetch_financial_indicators` 按 `available_at <= decision_at` PIT 门控读取。纯离线研究域，不连 broker 不下单。
- **research_run 分阶段进度上报（#188）**：`ResearchRunCoordinator.execute` 新增可选 `progress` 钩子，在 `_DECISION_STAGES` 逐 13 个 stage 与 REPORT 段回调 `progress(done, total, "research_run:<stage>")`，**不改变 artifact/checkpoint 语义**；决策总数 N 执行期才得知，total 采用「随新决策被发现而递增」的自修正计数（单快照 total=13 精确），worker 侧 `update_progress` 对 total 只增不减与之兼容；进度回调经 `_report_progress` 尽力而为（异常吞掉但 `CancelledError` 照常透传），上报故障不改变运行状态机。运行中 `finboard_job_get` / `GET /api/jobs/{id}` 可见 `research_run:<stage>` phase 与逐段 progress_done/total，可区分「正常计算」与「卡死」；终态 phase 保持 `research_run:<status>` 兼容。纯离线研究域，不连 broker 不下单。
- **正式研究组合入口（#91）**：long-only `ResearchRun` 必须由 `PortfolioPipelineAdapter` 从冻结候选池/特征/信号生成目标、硬约束、风险退出、三档资金可行性、研究订单/成交和账本；风险贡献上限启用后缺少协方差、数学不可行或不收敛必须在生成研究订单前失败关闭。
- **产品模拟盘边界（#83）**：`finboard-simulation` 只接受已发布策略和 completed `ResearchRun` 的结构化目标仓位，订单、成交、持仓、资金、时钟与审计全部持久化在独立模拟表。禁止导入/配置 Broker 或 QMT/CTP、禁止直接创建订单或修改持仓、禁止自动晋级影子盘/实盘。
- **研究策略配置边界**：允许用版本化无代码规格组合后端白名单候选池 / 因子 / 信号 / 目标仓位 / 研究风控 / 成交假设 / 验证计划；禁止网页提交 Python、模块路径或可执行表达式，保存 / 发布配置不得自动启动任何运行。
- **AI 能力边界（#84 建立，#160 全面迁移 OpenCode）**：FinBoard 自身**零内置 LLM 调用**——#84 的 `ResearchAssistant`/`OpenAICompatibleLLMProvider`/`finboard.ai.*` MCP 工具/`/api/research/ai` REST/前端审批中心与 `factor_hypotheses`/`factor_hypothesis_experiments`/`ai_drafts`/`ai_audit_events` 四张表已随 #160 移除。AI 智能由 OpenCode 研究运行时的外层 LLM 承担，FinBoard 只提供研究/数据域 MCP 工具；#57 机器验证本体（`research_experiments`，REST `/api/research/experiments` + `finboard.validation_experiment.*` MCP 工具）与因子实验（`finboard.factor.experiment.*` 的 `validation_experiment_id` 引用）保留，OOS 机器验证闭环不受影响。LLM key 配置迁移到 OpenCode 侧（`opencode_env_overrides` 注入）。
- **实盘恢复条件**：QMT 权限解决 → #22 真机验证 → #23 标的元数据 → #24 连续运行 → 小资金实盘。
- **P6 及以后禁止提前开工**：TWAP / VWAP / 冰山单 / 智能拆单、跨账户路由、复杂回测、因子系统、LLM 接入。

### 交易安全红线（改动这些逻辑必须先与用户确认风险）
- `client_order_id` 必须由本地生成且全局唯一，用于防重复下单 / 关联券商订单 / 状态恢复 / 排查异常
- **下单请求超时后禁止无条件重试**（这是文档明确指出的最危险场景）：订单须先进入 `UNKNOWN` 状态，查券商委托记录确认原订单不存在后才能重发；查询类请求可重试。重启恢复（`RecoveryEngine`）在 `kernel.start()` 中自动执行：逐订单查券商 → 修复状态 → UNKNOWN + 券商无记录 → REJECTED（不自动重发）
- 订单必须走完整链路：`Strategy → Order Intent → Risk Manager → Order Manager → Broker Adapter → Broker API`。**策略不得直接调用券商接口**
- 持仓的最终真实来源是**券商查询**；本地持仓仅用于实时响应 / 策略计算 / 风控 / 异常检测。**禁止策略直接修改持仓数量**，持仓只能由成交 / 公司行为 / 交割 / 人工校准等事件驱动
- 系统重启后必须先完成本地 ↔ 券商核对，核对通过前**禁止发送新订单**（恢复流程见 `phase1_doc.md` §5.4）
- Kill Switch（暂停新单 / 仅允许减仓 / 撤全部活动订单 / 暂停某策略某账户 / 全局停止）必须由**交易内核**执行，不能只依赖网页按钮
- 实盘资金/仓位预占与并发失败关闭缺口已登记为 #92；在用户确认并恢复 QMT 真机验证前不得以离线 #91 风控替代或提前实施

### LLM / Agent 边界
LLM（包括本 agent 自身）**不允许**：直接连接实盘账户、直接发送订单、修改账户持仓、绕过风控、在实盘运行时动态生成代码并立即执行。LLM 的产出必须经过完整流程才可上实盘：`研究 → 回测 → 样本外 → 行情回放 → 模拟交易 → 影子交易 → 小资金实盘 → 扩大资金`。

**#160 起 FinBoard 不再内置任何 LLM 调用**：`ResearchAssistant`/`OpenAICompatibleLLMProvider`/`llm_*` 配置与 `finboard.ai.*` MCP 工具已整体移除，AI 问答、假设与策略草案由 OpenCode 运行时的外层 LLM 直接承担。FinBoard 侧的安全控制收敛为：MCP 工具入参统一 `sanitize_prompt` 脱敏 + 审计（`mcp_audit_events`），研究写操作由 agent 自主执行（#122），实盘能力永久不注册为工具。`factor_research` 包仅保留 `sanitizer.py`。

- **FinBoard MCP Server 边界（#108，OpenCode 外置研究 Agent；#122 放开写操作审批门）**：`finboard-mcp` 包把研究能力以受控 MCP 工具形式暴露给外置 Agent 运行时（OpenCode），FinDashboard 仍是唯一事实来源。MCP 工具复用现有 service/repository，不直接连接数据库做裸 SQL（#160 后不含任何 LLM 工具）。权限矩阵：**研究写操作（创建因子/快照/策略/运行回测/发布数据/启动模拟盘）agent 可通过 MCP 自主执行**（#122 放开，不触及交易安全红线：不连 broker/账户/订单/持仓）；**实盘能力（下单/撤单/改持仓/Kill Switch/连接 broker/凭证探测）仍永久不注册为工具**。每次工具调用统一记录审计事件（structlog 结构化日志 + 内存副本；`mcp_audit_persist=true` 时追加到独立 `mcp_audit_events` 表，REST `GET /api/mcp/audit` 可查询，#157 接线），不记录 API Key/原始凭证/未脱敏思考内容，入参经 `sanitize_prompt` 脱敏。工具返回统一信封 `ToolEnvelope`（operation_id/status/data/error/provenance/idempotency_key）。回滚方案：关闭 MCP 入口（不启动 server）即可，不影响现有 REST 入口与研究产物/审计。OpenCode 接入（#109）、Skill 与记忆（#110）、前端增强（#111）为后续 sub-issue。
- **OpenCode 研究运行时边界（#109，#121 重构后简化；#157 移除 basic auth）**：`finboard-opencode` 包把 OpenCode（`opencode serve` / Docker 化 `opencode web`）作为受控研究 Agent 运行时接入 FinDashboard。**OpenCode 自身管理会话 / 历史 / 恢复；FinBoard 不再维护独立 conversation 投影层**（#121 移除了 `ConversationService`、`agent_conversations`/`agent_events` 表与 `/api/agent/conversations` 路由；会话 DB 持久化在容器命名卷 `opencode-data` → `/root/.local/share/opencode`）。`finboard-researcher` agent 在 `.opencode/agent/` 定义，`permission` 为显式 allowlist（#157 建立，#182 扩展）：`"*": "deny"` 默认拒绝全部，放行 `read`/`glob`/`grep`（Skill 渐进式加载与项目文档只读）、`skill`、`bash`（**仅只读轮询/状态检查**：如 `wget`/`sleep` 探测 HTTP 端点与任务进度；容器沙箱无 FinBoard 凭证，且 `.opencode`/`.agents`/运行时配置挂载一律 `:ro`，agent 无法改写仓库配置）与 `finboard_*` / `exa_*` MCP 工具（`exa` 为官方托管 remote 端点 `https://mcp.exa.ai/mcp`，匿名限速可用，API key 可经 `x-api-key` header 或 URL `?exaApiKey=` 注入，需先经 `opencode_env_overrides` 传入容器）；`edit`/`write`/`webfetch` 仍拒绝。**`OpenCodeRuntimeClient` 的两种部署形态**：（A）web 容器模式（`opencode_web_enabled`，#118 Docker 隔离）—— runtime client 复用托管的 `opencode web` 容器（`base_url` = 容器端口如 4097，无 auth：#157 用户决策单用户模型移除 basic auth，宿主机侧 127.0.0.1 绑定是唯一网络边界），该容器同时服务 iframe UI 与 `/api/*`，**不再需要独立的 4096 `opencode serve`**。（B）外部 serve 模式（仅 `opencode_enabled`，向后兼容）—— 连接外部已启动的 `opencode serve`（默认 4096，无 auth）。两种形态都不连接实盘 broker/账户/订单/持仓/Kill Switch；`opencode_enabled`/`opencode_web_enabled` 默认关闭，回滚方案为关闭入口回到现有 REST 入口。
- **研究 Skill 与长期记忆边界（#110，#122 更新审批门语义）**：项目级 `.agents/skills/finboard-opencode-research/` 提供研究 Skill（`SKILL.md` + `references/` 渐进式加载，含工作流/工具选择/权限边界/来源引用规则）。**Skill 不授予任何工具权限**——权限由 OpenCode 配置（`.opencode/opencode.json` agent permission）与 FinBoard MCP 服务端策略共同控制，Skill 只描述「应该做什么」。研究长期记忆持久化在独立 `research_memories` 表（`source_refs` 关联数据集/策略/实验/ResearchRun/Simulation 产物，只引用不修改产物），不写入实盘 `orders`/`fills`/`positions`/`audit_logs`。记忆操作（`finboard.memory.*` 记住/忘记/纠正/确认/归档/列表/详情）只操作 `research_memories` 表、不创建 ResearchRun/回测/模拟盘、不触及实盘，因此自动允许；`created_by` 由工具标记（`agent:mcp`/`user:api`），不可伪造。回滚方案：移除 Skill 目录与记忆表（迁移 downgrade），不影响研究产物/审计历史。
- **OpenCode Web 研究工作台网关边界（#118，已升级为 Docker 隔离；#121 重构后 access 不绑定 conversation；#157 移除 basic auth）**：`finboard-opencode` 的 `OpenCodeProcessManager` 托管一个**容器级隔离**的 `opencode web` Docker 容器（独立命名卷 `opencode-data`/`opencode-config` 持久化会话 DB 与 auth + 版本锁定镜像 `ghcr.io/anomalyco/opencode` + 环境变量严格白名单，**绝不**继承 FinBoard 的 DB 密码/broker 凭证/API Key + 宿主机侧 `127.0.0.1` 绑定）。**#157 用户决策：单一用户模型下 OpenCode Web 不启用 basic auth，直接使用明文 `http://127.0.0.1:{port}` URL；127.0.0.1 绑定是唯一网络边界，后续多用户需恢复网关鉴权层。**FinBoard 网关是 OpenCode Web 的**控制面**：`/api/opencode/status`（脱敏状态，含 `container_id` 与内嵌 finboard-mcp 运行状态）、`/api/opencode/health`（代理探测）、`/api/opencode/access`（网关启用即签发明文 `web_url`，无凭证字段），**不透传** OpenCode 流量（端口仅宿主机 loopback 可达）。因 OpenCode v1.18.15 不支持 `--base-path` 子路径部署（上游 PR #28326 审核中），前端采用 **iframe 跨源嵌入**而非子路径反代；待上游支持后可升级为同源子路径，上层契约不变。`opencode_web_enabled` 默认关闭，回滚方案为关闭入口回到现有 ResearchAssistant/REST 入口。OpenCode Web 工作台**不连接**实盘 broker/账户/订单/持仓/Kill Switch。**容器内 opencode 通过 `host.docker.internal:8765` 跨网络访问宿主机 `finboard_mcp`**（`finboard_mcp` 为 streamable-http + Bearer 鉴权；MCP 地址由 `opencode_mcp_remote_url` 在容器启动时渲染进 `.opencode/runtime/opencode.json` 单文件 bind mount，#157 接线；内嵌 MCP 在容器模式默认绑 `0.0.0.0`，token 保护），`-e` 只注入 LLM key / MCP token / HOME 与 XDG 目录语义，FinBoard 自身凭证不进容器。**容器目录语义（#112 实测）**：注入 `HOME=/workspace`（文件选择器/homedir 从工作目录开始，否则「搜不到 workspace」）与 `XDG_*_HOME` 指向 volume 挂载点的父目录（opencode 会在 XDG 数据根下追加 `opencode/` 子目录，值必须是父目录才能让最终落点正好是 named volume 挂载点）；OpenCode Web 的项目列表/最近会话存在**浏览器 IndexedDB**（iframe 与新窗口各自独立，服务端无法预置），首次使用需「添加项目 → 输入 `/` → 点击 `~`」恢复，会话回放走 runtime client 的 `session_history(after)` 与 SSE `after_seq` 游标。
- **OpenCode Web 研究工作台前端整合边界（#111，已合并 AIResearch；#121 重构后 iframe 直连 + #122 写操作自主执行 + #157 明文 URL）**：前端 `ResearchWorkbench` 页面（`/research/workbench`）顶部 Tab 分为「工作台」（**iframe 直连 OpenCode Web**：网关启用即签发明文 `web_url`（#157 无凭证，iframe 与「新窗口打开」共用同一 URL），无 conversation 绑定；顶部 gateway 状态 banner 展示运行/健康/容器 ID/版本/**FinBoard MCP 内嵌运行状态**）与「审批中心」（AI 草案 / 因子假设 / 审计日志的审计与历史查看入口，走 REST `/api/research/ai/*`，不依赖 OpenCode Web 网关）。**OpenCode Web 是研究交互层，OpenCode 自身管理会话/历史/恢复**（#121 移除了 FinBoard 侧 conversation 投影层，前端不再有会话列表/关键事件摘要/中断/中止）；**研究写操作（创建 ResearchRun/回测/模拟盘/因子/策略）由 agent 通过 MCP 自主执行**（#122 放开审批门），审批中心 Tab 从「写操作硬门」变为「审计/历史查看入口」，`DraftStatus` 生命周期仍可用于 AI 草案追溯但不再 block 写操作。`opencode_web_enabled=false`（网关 503）时「工作台」Tab 显示降级提示，「审批中心」Tab 仍可用。前端不连接实盘 broker/账户/订单/持仓/Kill Switch；后端 `/api/research/ai/*` REST 端点保留作为审批数据层。
- **MCP 研究能力全覆盖同步规范（#123）**：FinBoard 新增/修改研究功能时，**必须同步**（1）在 `finboard-mcp` 注册/更新对应 MCP 工具；（2）更新 `packages/finboard-mcp/src/finboard_mcp/server.py` 的 `_INSTRUCTIONS` 工具清单；（3）更新 Skill `.agents/skills/finboard-opencode-research/references/tools.md` 工具契约与 `SKILL.md` 工具选择表；（4）更新 `packages/finboard-mcp/ROADMAP.md`（如涉及新阶段）。否则 OpenCode Agent 的认知会与研究现实脱节。工具扩展路线图（#124-#128）见 ROADMAP.md。MCP 是受控入口，关闭即回滚，不影响 ResearchAssistant/REST 入口。
- **任务队列 MCP 工具边界（#136，统一持久化后台任务队列 #117 的 Sub-issue 4）**：`finboard-mcp` 把 #142/#143/#144 建立的持久化 `background_jobs` 队列以 4 个受控 MCP 工具暴露给外置 Agent：`finboard_job_list` / `finboard_job_get`（只读）、`finboard_job_enqueue` / `finboard_job_cancel`（写，受 `mcp_readonly_only` 门控）。**任务队列只服务研究 / 数据 / 回测类任务**（`background_jobs` 表，`BJ-` ID，与实盘 orders/fills/positions 完全隔离，不建任何外键）。`finboard_job_enqueue` 的 kind 白名单全是研究 / 数据域（`echo` / `research_run` / `feature_snapshot` / `bulk_download` / `dataset_publish` / `backtest_run` / `data_sync` / `fetch_all` / `quality_repair`），不含实盘能力；实盘交易内核（盘前检查 / 收盘撤单 / 日终核对 / Broker 心跳 / Kill Switch）由专用 `finboard-scheduler` asyncio 调度执行，**不进入**统一队列、不暴露为 MCP 工具。`job_cancel` 属研究域协作式取消（executor checkpoint 时退出），非实盘撤单，因此 `finboard_job_*` 与 `finboard_run_*` 同理豁免实盘交易动词检查。同 issue 顺带把 `feature_snapshot_job_start/status` 从已下线的进程内 `FeatureSnapshotJobManager` 迁移到持久化队列（与 #144 的 REST 口径一致）。回滚方案：移除 4 个 `finboard_job_*` 工具即可，不影响 #142/#143/#144 的 worker / 任务表 / REST 接口。

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

## 记忆（Memory）规范

- **记忆文件统一存放在仓库 `docs/memory/` 下**，一篇记忆一个 Markdown 文件
  （`<kebab-case-slug>.md`），内容与格式不限但至少包含：主题、结论 / 事实、
  **Why（为什么）** 与 **How to apply（下次如何应用）**；相对日期一律转绝对日期。
- **`docs/memory/README.md` 是唯一索引**：每个记忆文件一行指针
  （`- [标题](文件.md) — 一句话钩子`），新增 / 修改 / 归档记忆时同步更新。
- **禁止把记忆写入任何 coding 框架自有的路径**（如各框架的 `~/.` 记忆目录、
  会话目录、私有缓存），保证记忆可在不同框架 / 工具间共享协作。
- 仓库中已能直接查到的事实（代码结构、git 历史、AGENTS.md 本身、issue/PR 描述）
  不重复记录；只记「不读代码 / 不翻历史就不知道」的经验与决策（踩坑根因、
  环境特殊性、用户偏好、跨 issue 的上下文）。
- 记忆属于项目资产，随 PR 进仓库（与代码 / 文档一样走评审与合并），不留在
  本地私有位置。

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

**pytest 临时目录（Windows ACL 根除，勿绕行）**：所有 pytest 调用统一使用仓库内
`.pytest-tmp/`（`pyproject.toml` addopts 已固定 `--basetemp .pytest-tmp` +
`-p no:cacheprovider`，该目录已入 `.gitignore`）。原因：`%LOCALAPPDATA%` 下默认
`pytest-of-<user>` 与 `.pytest_cache` 在本机偶发 WinError 5 拒绝访问（ACL 损坏），
导致 `tmp_path` 系 fixture 报错。因此**所有 pytest 必须在仓库根目录运行**（相对路径
以 CWD 为基准），不要手动加 `--basetemp` 指向系统临时目录，也不要为绕过报错而
关闭/改写该配置。若仓库根目录没有 `.pytest-tmp`，pytest 会自动创建；CI 与本地行为一致。

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
  finboard-mcp/         — FinBoard MCP Server（向 OpenCode 等外置 Agent 暴露受控研究工具，issue #108）
  finboard-opencode/    — OpenCode 研究运行时集成（会话关联/SSE 事件/中断恢复，issue #109）
```
