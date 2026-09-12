"""``AsyncEngine`` 工厂。

* ``postgresql+psycopg`` 方言 + psycopg v3,async 模式开箱即用;
* ``pool_pre_ping=True``:每次取连接前 ping,避免长时间空闲后拿到死连接;
* ``pool_size`` / ``max_overflow`` 通过环境变量控制,默认适配 P0 单进程。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import create_async_engine as _sa_create_async_engine

DEFAULT_DB_URL = "postgresql+psycopg://findashboard:CHANGE_ME@127.0.0.1:5432/findashboard"


def create_async_engine(
    url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 10,
    echo: bool = False,
    connect_args: Mapping[str, Any] | None = None,
) -> AsyncEngine:
    """便捷包装:固定 ``pool_pre_ping=True``,其余参数透传给 SQLAlchemy。

    ``connect_args``(#450):psycopg/libpq 驱动级参数(TCP keepalive 等)经
    此透传 —— pre_ping 只能检出「取用时已死」的连接,且 ping 自身也可能在
    黑洞连接上永久挂起;keepalive 才能把静默黑洞变成显式异常。
    """
    return _sa_create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        echo=echo,
        connect_args=dict(connect_args) if connect_args else {},
    )
