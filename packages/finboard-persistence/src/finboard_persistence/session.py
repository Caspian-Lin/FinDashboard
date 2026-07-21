"""``AsyncSession`` 工厂。

风格说明:不强制 ``async with`` 风格的 ``sessionmaker``;
对外暴露一个可调用对象 ``session_factory(engine) -> AsyncSessionMaker``,
调用方按需 ``async with maker() as session: ...``。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

type AsyncSessionMaker = async_sessionmaker[AsyncSession]


def session_factory(
    engine: AsyncEngine,
    *,
    expire_on_commit: bool = False,
) -> AsyncSessionMaker:
    """创建一个 session maker。

    ``expire_on_commit=False`` 是关键:commit 后对象属性不失效,
    避免在 async 上下文中再次 lazy-load 触发 ``MissingGreenlet``。
    """
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=expire_on_commit,
    )
