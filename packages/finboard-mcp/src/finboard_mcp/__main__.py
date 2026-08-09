"""FinBoard MCP server CLI 入口。

默认以 stdio 传输启动(供 OpenCode 等本地 host 以子进程方式接入)。
设置 ``FINBOARD_MCP_TRANSPORT=streamable-http`` 可切换为 HTTP 传输 —— 此时必须同时设置
``FINBOARD_MCP_AUTH_TOKEN``(Bearer 鉴权,防 ``0.0.0.0`` 绑定裸奔;见 issue #xxx Docker 隔离)。
"""

from __future__ import annotations

from typing import Any

import uvicorn

from finboard_app.config import load_settings
from finboard_mcp.auth import wrap_with_bearer_auth
from finboard_mcp.server import build_mcp_server


def main() -> None:
    settings = load_settings()
    mcp = build_mcp_server()
    # stdio 传输:本机子进程接入,无需鉴权,保持原行为。
    if settings.mcp_transport == "stdio":
        mcp.run(transport="stdio")
        return
    # HTTP 传输(streamable-http / sse):跨网络访问必须鉴权。
    _run_http(mcp, settings)


def _run_http(mcp: object, settings: Any) -> None:
    """以 HTTP 传输启动,在外层包裹 Bearer 鉴权中间件后交给 uvicorn。

    ``settings`` 用 ``Any`` 而非具体类型,避免 ``finboard-mcp`` 反向依赖
    ``finboard-app`` 的 Settings 类(后者已依赖前者,会形成循环)。
    """
    transport = settings.mcp_transport
    token = settings.mcp_auth_token
    host = settings.mcp_host
    port = settings.mcp_port
    if not token:
        # 0.0.0.0 绑定 + 无鉴权 = 把研究工具裸奔到局域网,坚决拒绝。
        raise SystemExit(
            "FINBOARD_MCP_AUTH_TOKEN 必须设置:HTTP 传输(streamable-http/sse)"
            " 不能在无鉴权下启动。stdio 模式忽略此项。"
        )
    if transport == "streamable-http":
        starlette_app = mcp.streamable_http_app(host=host)  # type: ignore[attr-defined]
    elif transport == "sse":
        starlette_app = mcp.sse_app(host=host)  # type: ignore[attr-defined]
    else:  # pragma: no cover — _run_http 仅在非 stdio 时调用
        raise SystemExit(f"未支持的 mcp_transport: {transport}")
    wrapped = wrap_with_bearer_auth(starlette_app, token=token)
    uvicorn.run(wrapped, host=host, port=port)


if __name__ == "__main__":
    main()
