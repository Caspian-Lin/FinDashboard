"""FinBoard MCP Server 组装。

构建一个 :class:`MCPServer`,挂载研究域 lifespan(``AsyncEngine`` +
``AuditRecorder``),并注册受控研究工具集。

当前工具集(随 sub-issue 增量扩展):

* ``finboard.run.*`` —— ResearchRun 只读查询(列表 / 详情 / artifact);
* ``finboard.memory.*`` —— 研究长期记忆(记住 / 忘记 / 纠正 / 确认 / 归档 /
  列表 / 详情),让 Agent 跨会话积累研究上下文;
* 数据查询(``finboard.instrument.*`` / ``finboard.dataset.*`` /
  ``finboard.data.*`` / ``finboard.tushare.*``,#124)—— 标的元数据 /
  数据集发布 / 缓存状态 / 数据质量 / Tushare 配额(只读);
* ``finboard.factor.*`` / ``finboard.feature_snapshot.*``(#125)——
  因子目录 / 特征快照 / 因子信号 / 因子实验(含异步快照任务);
* ``finboard.strategy.*`` / ``finboard.preset.*``(#126)——
  策略规格注册表 / 模板 / 校验 / 草稿 / 发布 / 回滚 / diff + 预设 CRUD
  (无代码版本化生命周期);
* ``finboard.backtest.*``(#127)—— 回测引擎(可用策略 schema / 同步运行 /
  历史列表 / 详情 / 删除);
* ``finboard.sim.*``(#127 + #139)—— 模拟盘(账户 / 会话生命周期 / 决策提交 /
  行情投递 / 晋级评估 / 归档 / 订单 / 成交 / 持仓 / 账本 / 审计 / 报告);
* ``finboard.run.*`` 写工具(queue / cancel / replay / lineage,#127)。
* ``finboard.portfolio.*``(#128)—— 组合计算(目标权重分配 /
  离散手数 sizing / 资金档位可行性 / 绩效归因,纯计算无 DB 写入);
* ``finboard.job.*``(#136)—— 统一后台任务队列监控与提交
  (list / get 只读 + enqueue / cancel 写,复用 ``background_jobs`` 表)。
* 数据写操作(#137)—— ``finboard.data_write.*`` / ``finboard.etf.*``:
  data_fetch(同步单标的)/ fetch_all / sync_universe / bulk_download_start /
  quality_repair / dataset_release_publish(任务化,返回 job_id,用
  ``finboard_job_get`` 轮询)、config_get/update、etf_sync/batch_confirm/update/
  review_queue。补全「数据→因子→策略」闭环的数据准备第一步。
* #57 验证实验(#138)—— ``finboard.validation_experiment.*``:create / list /
  get / reject / add_trial / delete,暴露 REST ``/api/research/experiments``
  的 6 个端点(OOS 样本外验证实验元数据 CRUD,不触发 ValidationRunner 执行),
  给因子实验的 ``validation_experiment_id`` 提供源头,补齐 OOS 验证闭环。
* 自选股(#140)—— ``finboard.watchlist.*``:list / get 只读 + create / update /
  delete / add_symbols / remove_symbol 写,暴露 REST ``/api/watchlists`` 的 7 个
  端点(用户标的组 —— 保存常用回测标的集合,为回测 / 研究准备标的池)。
* 报告聚合与导出(#141)—— ``finboard.report.*``:report_run / report_backtest
  只读聚合(ResearchRun result + artifacts;回测 metrics + equity_curve + fills),
  report_export 导出 CSV / Markdown 文件(纯标准库,返回绝对路径)。

安全:实盘能力(下单 / 撤单 / 改持仓 / Kill Switch / 连接 broker / 凭证探测)
**永久不注册**为工具。研究写操作(创建 Run / 启动回测 / 模拟盘)由 agent 自主执行
(issue #122),不触及交易安全红线。交易内核(盘前检查 / 收盘撤单 / 日终核对 /
Broker 心跳 / Kill Switch)由专用 Scheduler 执行,不进入统一队列。
"""

from __future__ import annotations

from mcp.server import MCPServer

from finboard_mcp.context import app_lifespan
from finboard_mcp.tools import (
    register_backtest_tools,
    register_data_tools,
    register_data_write_tools,
    register_factor_tools,
    register_jobs_tools,
    register_memory_tools,
    register_portfolio_tools,
    register_report_tools,
    register_run_tools,
    register_simulation_tools,
    register_strategy_tools,
    register_validation_experiment_tools,
    register_watchlist_tools,
)

_INSTRUCTIONS = """\
FinBoard 研究 MCP —— 量化研究工具集

你是接入 FinDashboard(简称 FinBoard)量化交易系统的 AI 研究 Agent。FinBoard 是
模块化单体架构(非微服务)的可实盘交易量化系统,技术栈 Python + PostgreSQL,
研究 / 回测 / 模拟 / 实盘严格隔离。你只接入【研究】域,不接入实盘。

== 研究流程全景 ==
数据获取(akshare/tushare)→ 因子分析(因子实验室)→ 策略规格(无代码版本化)→
回测(行情回放 + 纸面撮合)→ 模拟盘(持久化隔离)→ 评估(绩效分析)。
完整流程详解见 Skill `references/research-workflow.md`。

== 当前可用工具(113 个,已实现)==
- finboard.run.*(7) —— ResearchRun 只读:list / get / artifacts;
  写:queue / cancel / replay / lineage(✅ #127)
- finboard.memory.*(7) —— 研究记忆:remember / list / get / forget / correct
  / confirm / archive(跨会话长期上下文,操作 research_memories 独立表)
- 数据查询(9,✅ #124):instrument list/get/search、dataset_release list/get、
  dataset_manifest_list、data_cache_status、data_quality_check、tushare_quota
  (标的元数据 / 数据集发布 / 缓存状态 / 数据质量 / Tushare 配额,只读)
- 因子实验室(12,✅ #125):factor_catalog、feature_snapshot list/get/create/
  job_start/job_status、factor_signal list/get、factor_experiment list/get/create/
  sync_validation(因子目录 / 特征快照 / 因子信号 / 因子实验,含写操作)
- 策略规格(16,✅ #126):strategy registry/template/list/history/version_get/
  diff、preset list/get(只读);strategy validate(纯计算)/draft_create/
  supersede/publish/rollback、preset create/update/delete(写操作)。
  无代码版本化生命周期,反复 validate 预览 → draft → publish。
- 回测(5,✅ #127):backtest_strategy_list(可用策略+参数 schema)、
  backtest_run(同步运行,返回 metrics/equity/fills)、
  backtest_history_list/get、backtest_history_delete(写)。
- 模拟盘(21,✅ #127+#139):sim_account list/get/create(写)、
  sim_session list/get/create(写)/start/pause/stop/archive(写)/reset(写)、
  sim_decision_submit(写,结构化目标仓位 → 生成订单,不直接创建订单)、
  sim_market_event(写,投递 OHLCV K 线驱动撮合,仅 running 会话,source_event_id
  幂等)、sim_session_evaluate(写,晋级评估,仅 stopped 会话,automatic_live_promotion
  恒 false)、sim_order_cancel(写)、sim_orders/fills/positions/ledger/audit/report
  (只读)。
  完整生命周期:创建账户+会话 → start → 投 K 线撮合 → 提交决策 → stop →
  evaluate(eligible/failed)→ archive。
- portfolio(4,✅ #128):portfolio_allocate(目标权重分配,纯计算)、
  portfolio_sizing(离散手数 + 费用/保证金)、portfolio_feasibility(10万/20万/50万
  档位可行性)、portfolio_attribution(绩效归因分解,纯计算,无 DB 写入)。
- finboard.job.*(4,✅ #136)—— 统一后台任务队列监控与提交:
  job list/get(只读)、job enqueue/cancel(写)。复用 background_jobs 表,
  enqueue kind 白名单全是研究/数据/回测域(echo/research_run/feature_snapshot/
  bulk_download/dataset_publish/backtest_run/data_sync/fetch_all/quality_repair);
  实盘交易内核任务不进入队列。feature_snapshot/bulk_download 等异步任务的进度
  统一用 finboard_job_get(job_id) 轮询(result_ref 携带产物引用如 snapshot_id)。
- 数据写操作(12,✅ #137):data_fetch(同步单标的拉取)、fetch_all /
  sync_universe / bulk_download_start / quality_repair / dataset_release_publish
  (任务化,登记 queued 返回 job_id,进度用 finboard_job_get 轮询)、
  data_config_get/update(调度器配置)、etf_sync(默认 dry_run)/
  etf_batch_confirm / etf_update(人工覆盖)/ etf_review_queue(只读)。
  补全「数据→因子→策略」闭环的数据准备第一步:agent 能拉 K 线、发布数据集、
  修复质量缺陷、同步 ETF 元数据。不连 broker / 账户 / 订单 / 持仓。
- 验证实验(6,✅ #138):validation_experiment create/list/get/reject/add_trial/
  delete(#57 OOS 机器验证实验元数据 CRUD:冻结假设+计划+门 → 登记 trial →
  因子实验用 validation_experiment_id 引用 → factor_experiment_sync_validation
  同步终态,补齐 OOS 过拟合控制闭环;实验实际执行由离线 ValidationRunner 完成,
  揭盲端点不在 MCP 内)。与因子实验(登记簿)是两套独立但耦合的系统。
- 自选股(7,✅ #140):watchlist list/get(只读)、create/update/delete/
  add_symbols/remove_symbol(写,受 mcp_readonly_only 守卫)。标的组管理:
  创建标的集合(如回测候选池)→ 加 symbols(自动去重)→ 回测/研究复用,
  与 strategies/simulation 无关联(独立用户查找列表)。
- 报告聚合与导出(3,✅ #141):report_run(聚合 ResearchRun:result 指标 +
  全部 artifacts,含 report/equity/decisions 各阶段 payload)、report_backtest
  (聚合回测:metrics + equity_curve + fills + summary)、report_export
  (导出 CSV/Markdown 文件,写入 FINBOARD_EXPORT_DIR 或系统临时目录,
  返回绝对路径;纯标准库,零新依赖;只读不写 DB)。

分阶段扩展计划见 `packages/finboard-mcp/ROADMAP.md`。

== 权限边界 ==
- 研究写操作(创建因子 / 快照 / 策略 / 运行回测 / 发布数据 / 启动模拟盘):
  agent 可通过 MCP 自主执行(#122),不触及交易安全红线。
- 实盘能力(下单 / 撤单 / 改持仓 / Kill Switch / 连接 broker / 凭证探测):
  永久不可用,不注册为工具。需要它们 = 走错了路。
- 不生成代码:策略是无代码版本化规格,禁止生成 Python / 模块路径 / 可执行表达式。

== 输出规范 ==
- 工具返回统一信封 ToolEnvelope(operation_id / status / data / error /
  provenance / idempotency_key)。
- 金融答案必须引用项目来源(ResearchRun ID / 数据集版本 / 模拟盘 ID)。
- 数据不足时明确声明「数据不足」,绝不编造数字。
"""


def build_mcp_server() -> MCPServer:
    """组装 FinBoard MCP server(注册工具集 + 研究域 lifespan)。"""
    mcp = MCPServer(
        "finboard",
        instructions=_INSTRUCTIONS,
        lifespan=app_lifespan,
    )
    register_run_tools(mcp)
    register_memory_tools(mcp)
    register_data_tools(mcp)
    register_data_write_tools(mcp)
    register_factor_tools(mcp)
    register_strategy_tools(mcp)
    register_backtest_tools(mcp)
    register_simulation_tools(mcp)
    register_portfolio_tools(mcp)
    register_report_tools(mcp)
    register_jobs_tools(mcp)
    register_validation_experiment_tools(mcp)
    register_watchlist_tools(mcp)
    return mcp


__all__ = ["build_mcp_server"]
