"""FinBoard MCP 工具集。"""

from __future__ import annotations

from finboard_mcp.tools.ai import register as register_ai_tools
from finboard_mcp.tools.memories import register as register_memory_tools
from finboard_mcp.tools.runs import register as register_run_tools

__all__ = [
    "register_ai_tools",
    "register_memory_tools",
    "register_run_tools",
]
