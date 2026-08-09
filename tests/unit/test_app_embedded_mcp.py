"""API lifespan 内嵌 finboard-mcp HTTP server 单元测试。

验证:
* ``opencode_embed_mcp=True`` + 有 ``mcp_auth_token`` → 后台 uvicorn server 被启动
  (``app.state.opencode_mcp_server`` 被设置,端口/host/token 正确)。
* 无 ``mcp_auth_token`` → 跳过(HTTP 传输强制鉴权,空 token 拒绝启动)。
不真正启动 uvicorn(构造完 server 对象即 mock 掉 ``serve()``)。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from finboard_api.app import _start_embedded_mcp_server


def _make_settings(**overrides: object) -> MagicMock:
    """构造带必要字段的 settings mock。"""
    defaults = {
        "mcp_host": "0.0.0.0",
        "mcp_port": 8765,
        "mcp_auth_token": "secret-token",
    }
    defaults.update(overrides)
    settings = MagicMock()
    for key, value in defaults.items():
        setattr(settings, key, value)
    return settings


@pytest.mark.asyncio
async def test_start_embedded_mcp_server_starts_uvicorn() -> None:
    """有 auth_token 时,构造 uvicorn.Server 并后台启动 serve()。"""
    app = FastAPI()
    settings = _make_settings()

    fake_mcp = MagicMock()
    fake_starlette = MagicMock(name="starlette_app")
    fake_mcp.streamable_http_app.return_value = fake_starlette

    fake_server = MagicMock()
    fake_server.serve = AsyncMock()

    with patch(
        "finboard_mcp.server.build_mcp_server", return_value=fake_mcp
    ), patch(
        "finboard_mcp.auth.wrap_with_bearer_auth",
        return_value=MagicMock(name="wrapped"),
    ), patch("uvicorn.Config") as config_cls, patch(
        "uvicorn.Server", return_value=fake_server
    ) as server_cls:
        await _start_embedded_mcp_server(app, settings)

    # Config 用 wrapped app + host/port/loop="none" 构造。
    config_cls.assert_called_once()
    call_kwargs = config_cls.call_args
    assert call_kwargs.kwargs["host"] == "0.0.0.0"
    assert call_kwargs.kwargs["port"] == 8765
    assert call_kwargs.kwargs["loop"] == "none"
    assert call_kwargs.kwargs["log_level"] == "warning"
    server_cls.assert_called_once_with(config_cls.return_value)
    # server 挂到 app.state,serve() 作为后台任务派发。
    assert app.state.opencode_mcp_server is fake_server
    assert isinstance(app.state.opencode_mcp_task, asyncio.Task)
    fake_mcp.streamable_http_app.assert_called_once_with(host="0.0.0.0")
    # 清理后台任务(serve 是 mock,会立即完成)。
    await app.state.opencode_mcp_task


@pytest.mark.asyncio
async def test_start_embedded_mcp_server_skipped_without_token() -> None:
    """无 mcp_auth_token 时跳过启动(不抛异常,不设 app.state)。"""
    app = FastAPI()
    settings = _make_settings(mcp_auth_token="")

    with patch(
        "finboard_mcp.server.build_mcp_server"
    ) as build_mock, patch("uvicorn.Server") as server_mock:
        await _start_embedded_mcp_server(app, settings)

    build_mock.assert_not_called()
    server_mock.assert_not_called()
    assert not hasattr(app.state, "opencode_mcp_server") or (
        getattr(app.state, "opencode_mcp_server", None) is None
    )
