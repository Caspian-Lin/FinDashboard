"""FinBoard MCP Server。

把 FinDashboard 的研究能力以受控 MCP 工具的形式暴露给外置 Agent 运行时
(OpenCode)。FinDashboard 仍是业务工具 / 权限 / 任务 / 数据 / 审计 / 产物的
唯一事实来源,MCP 工具复用现有 service / repository / ``ResearchAssistant``,
不直接连接数据库做裸 SQL,不暴露实盘能力。

安全红线(详见 ``AGENTS.md``):

* 只读工具自动允许;写操作走「草案 + 人工审批」,AI 无法自主执行;
* 实盘能力(下单 / 撤单 / 改持仓 / Kill Switch / 连接 broker / 凭证探测)
  永久不暴露为 MCP 工具;
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
