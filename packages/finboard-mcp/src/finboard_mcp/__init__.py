"""FinBoard MCP Server。

把 FinDashboard 的研究能力以受控 MCP 工具的形式暴露给外置 Agent 运行时
(OpenCode)。FinDashboard 仍是业务工具 / 权限 / 任务 / 数据 / 审计 / 产物的
唯一事实来源,MCP 工具复用现有 service / repository / ``ResearchAssistant``,
不直接连接数据库做裸 SQL,不暴露实盘能力。

安全红线(详见 ``AGENTS.md``):

* 研究写操作(创建因子/快照/策略/运行回测/发布数据/启动模拟盘)agent 可通过
  MCP **自主执行**(#122 放开审批门);不触及交易安全红线(不连 broker/账户/订单/持仓);
* 实盘能力(下单 / 撤单 / 改持仓 / Kill Switch / 连接 broker / 凭证探测)
  **永久不暴露**为 MCP 工具;
* 每次工具调用统一记录审计事件,不记录 API Key / 原始凭证 / 未脱敏思考内容。
"""

from __future__ import annotations

from finboard_mcp.envelope import (
    ErrorKind,
    ToolEnvelope,
    ToolError,
    denied,
    error,
    new_operation_id,
    ok,
    pending_approval,
)
from finboard_mcp.server import build_mcp_server

__all__ = [
    "ErrorKind",
    "ToolEnvelope",
    "ToolError",
    "build_mcp_server",
    "denied",
    "error",
    "new_operation_id",
    "ok",
    "pending_approval",
]
