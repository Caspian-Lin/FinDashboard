"""FinBoard MCP server CLI 入口。

默认以 stdio 传输启动(供 OpenCode 等本地 host 以子进程方式接入)。
设置 ``FINBOARD_MCP_TRANSPORT=streamable-http`` 可切换为 HTTP 传输。
"""

from __future__ import annotations

from finboard_app.config import load_settings
from finboard_mcp.server import build_mcp_server


def main() -> None:
    settings = load_settings()
    mcp = build_mcp_server()
    if settings.mcp_transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=settings.mcp_host,
            port=settings.mcp_port,
        )
    elif settings.mcp_transport == "sse":
        mcp.run(transport="sse", host=settings.mcp_host, port=settings.mcp_port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
