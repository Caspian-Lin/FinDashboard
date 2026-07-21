"""``AsyncEngine`` 工厂。

* ``postgresql+psycopg`` 方言 + psycopg v3,async 模式开箱即用;
* ``pool_pre_ping=True``:每次取连接前 ping,避免长时间空闲后拿到死连接;
* ``pool_size`` / ``max_overflow`` 通过环境变量控制,默认适配 P0 单进程。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import create_async_engine as _sa_create_async_engine

DEFAULT_DB_URL = "postgresql+psycopg://finboard:finboard@127.0.0.1:5432/finboard"


def create_async_engine(
    url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 10,
    echo: bool = False,
) -> AsyncEngine:
    """便捷包装:固定 ``pool_pre_ping=True``,其余参数透传给 SQLAlchemy。"""
    return _sa_create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        echo=echo,
    )
