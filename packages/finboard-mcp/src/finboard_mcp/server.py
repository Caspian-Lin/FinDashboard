"""FinBoard MCP Server 组装。

构建一个 :class:`MCPServer`,挂载研究域 lifespan(``AsyncEngine`` +
``ResearchAssistant`` + ``AuditRecorder``),并注册受控研究工具集。

当前工具集(随 sub-issue 增量扩展):

* ``finboard.ai.*`` —— AI 研究助手(问答 / 因子假设 / 策略草案 / 策略 diff);
* ``finboard.run.*`` —— ResearchRun 只读查询(列表 / 详情 / artifact);
* ``finboard.memory.*`` —— 研究长期记忆(记住 / 忘记 / 纠正 / 确认 / 归档 /
  列表 / 详情),让 Agent 跨会话积累研究上下文。

安全:实盘能力(下单 / 撤单 / 改持仓 / Kill Switch / 连接 broker / 凭证探测)
**永久不注册**为工具。写操作(创建 Run / 启动回测 / 模拟盘)走审批门。
"""

from __future__ import annotations

from mcp.server import MCPServer

from finboard_mcp.context import app_lifespan
from finboard_mcp.tools import (
    register_ai_tools,
    register_memory_tools,
    register_run_tools,
)

_INSTRUCTIONS = """\
FinBoard 研究工具集(只读为主)。

权限边界:
- 只读工具(数据 / 因子 / ResearchRun / 模拟盘查询 / AI 问答)可直接调用。
- 写操作(创建 ResearchRun / 回测 / 模拟盘 / 定时任务)需经人工审批,AI 无法自主执行。
- 实盘能力(下单 / 撤单 / 改持仓 / Kill Switch / 凭证)永久不可用。

工具返回统一信封(operation_id / status / data / error / provenance / idempotency_key)。
回答研究问题必须引用项目来源,数据不足时明确声明,不编造。\
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
    return mcp


__all__ = ["build_mcp_server"]
