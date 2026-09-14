"""``AsyncEngine`` 工厂。

* ``postgresql+psycopg`` 方言 + psycopg v3,async 模式开箱即用;
* ``pool_pre_ping=True``:每次取连接前 ping,避免长时间空闲后拿到死连接;
* ``pool_size`` / ``max_overflow`` 通过环境变量控制,默认适配 P0 单进程;
* **keepalive 默认全覆盖(#450/#471)**:postgres URL 自动注入
  :func:`postgres_connect_args` 的 libpq keepalive 参数 —— 调用方显式
  ``connect_args`` 同名键优先生效(浅合并)。#471 挂死取证发现:各命令
  各自建引擎时极易漏带 keepalive(``cli.py`` 多处裸引擎),黑洞连接上的
  ``await`` 永不返回;收敛到工厂层默认值后,任何经本工厂创建的 postgres
  引擎都不再依赖调用方自觉。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import create_async_engine as _sa_create_async_engine

DEFAULT_DB_URL = "postgresql+psycopg://findashboard:CHANGE_ME@127.0.0.1:5432/findashboard"


def postgres_connect_args(db_url: str) -> dict[str, int]:
    """psycopg(libpq)连接黑洞加固参数(#450;#471 起为引擎工厂默认值)。

    WSL2 NAT 转发下的长驻连接池实测会被静默黑洞化(对端无 RST,本地 send
    成功、recv 永不返回):worker 事件循环上的任务全体冻结在 ``await`` 上、
    心跳停摆、相位不动,僵尸检测与协作取消同循环同死。TCP keepalive 让死
    连接在 ~keepalives_idle + count x interval(默认 ~60s)内显式报错,由
    调用方(逐操作短会话等)按连接级失败处理;非 postgres 驱动(sqlite 等)
    返回空 dict 不影响测试。
    """
    if not db_url.startswith(("postgresql://", "postgresql+psycopg://", "postgres://")):
        return {}
    return {
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    }


def create_async_engine(
    url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 10,
    echo: bool = False,
    connect_args: Mapping[str, Any] | None = None,
) -> AsyncEngine:
    """便捷包装:固定 ``pool_pre_ping=True`` + postgres keepalive 默认注入。

    ``connect_args``(#450):psycopg/libpq 驱动级参数(TCP keepalive 等)经
    此透传 —— pre_ping 只能检出「取用时已死」的连接,且 ping 自身也可能在
    黑洞连接上永久挂起;keepalive 才能把静默黑洞变成显式异常。#471 起
    keepalive 不再要求调用方显式携带:postgres URL 的默认 connect_args 由
    :func:`postgres_connect_args` 提供,调用方 ``connect_args`` 同名键覆盖
    (例如 dev 预检想把 ``connect_timeout`` 收紧到 3s)。
    """
    merged: dict[str, Any] = postgres_connect_args(url)
    if connect_args:
        merged.update(dict(connect_args))
    return _sa_create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        echo=echo,
        connect_args=merged,
    )
