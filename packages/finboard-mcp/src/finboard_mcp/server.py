"""FinBoard MCP Server 组装。

构建一个 :class:`MCPServer`,挂载研究域 lifespan(``AsyncEngine`` +
``ResearchAssistant`` + ``AuditRecorder``),并注册受控研究工具集。

当前工具集(随 sub-issue 增量扩展):

* ``finboard.ai.*`` —— AI 研究助手(问答 / 因子假设 / 策略草案 / 策略 diff);
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
  (无代码版本化生命周期)。

安全:实盘能力(下单 / 撤单 / 改持仓 / Kill Switch / 连接 broker / 凭证探测)
**永久不注册**为工具。研究写操作(创建 Run / 启动回测 / 模拟盘)由 agent 自主执行
(issue #122),不触及交易安全红线。
"""

from __future__ import annotations

from mcp.server import MCPServer

from finboard_mcp.context import app_lifespan
from finboard_mcp.tools import (
    register_ai_tools,
    register_data_tools,
    register_factor_tools,
    register_memory_tools,
    register_run_tools,
    register_strategy_tools,
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

== 当前可用工具(50 个,已实现) ==
- finboard.run.*(3) —— ResearchRun 只读:list / get / artifacts
- finboard.ai.*(4) —— AI 草案:ask / propose_hypothesis / propose_strategy_draft
  / propose_strategy_diff(底层 ResearchAssistant 强制 assert_research_only_request
  拒绝越权 + sanitize_prompt 抹掉凭证)
- finboard.memory.*(7) —— 研究记忆:remember / list / get / forget / correct
  / confirm / archive(跨会话长期上下文,操作 research_memories 独立表)
- 数据查询(9,✅ #124):instrument list/get/search、dataset_release list/get、
  dataset_manifest_list、data_cache_status、data_quality_check、tushare_quota
  (标的元数据 / 数据集发布 / 缓存状态 / 数据质量 / Tushare 配额,只读)
- 因子实验室(11,✅ #125):factor_catalog、feature_snapshot list/get/create/
  job_start/job_status、factor_signal list/get、factor_experiment list/get/create/
  sync_validation(因子目录 / 特征快照 / 因子信号 / 因子实验,含写操作)
- 策略规格(16,✅ #126):strategy registry/template/list/history/version_get/
  diff、preset list/get(只读);strategy validate(纯计算)/draft_create/
  supersede/publish/rollback、preset create/update/delete(写操作)。
  无代码版本化生命周期,反复 validate 预览 → draft → publish。

== 路线图(planned,对应 issue,尚未实现) ==
- 回测 + 模拟盘 + 研究运行工具 —— #127
- portfolio 计算工具(allocate/sizing/feasibility/attribution)—— #128
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
- AI 草案(DraftStatus: proposed→approved→consumed/rejected)是评审起点,不是结论。\
"""


def build_mcp_server() -> MCPServer:
    """组装 FinBoard MCP server(注册工具集 + 研究域 lifespan)。"""
    mcp = MCPServer(
        "finboard",
        instructions=_INSTRUCTIONS,
        lifespan=app_lifespan,
    )
    register_ai_tools(mcp)
    register_run_tools(mcp)
    register_memory_tools(mcp)
    register_data_tools(mcp)
    register_factor_tools(mcp)
    register_strategy_tools(mcp)
    return mcp


__all__ = ["build_mcp_server"]
