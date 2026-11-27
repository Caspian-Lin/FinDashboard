"""MCP 按客户端凭据仓储(issue #517,``mcp_clients``)。

服务端签发可撤销 Bearer 凭据:创建 / 查询 / 吊销 / 记录最近使用。**明文
token 只存在于调用方(CLI 创建时打印一次)**,本仓储只接收 ``token_hash``
(sha256 十六进制)与 ``token_prefix``(人工辨认用,不能认证)。

``scopes`` 以 JSON 数组原样存取;合法取值与预设由 ``finboard_mcp.scopes``
定义(本层不反向依赖 MCP 包,避免持久化层耦合传输层)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import McpClientModel


class McpClientConflictError(RuntimeError):
    """``client_id`` / ``name`` 唯一约束冲突(重复创建同名客户端)。"""


class McpClientRepository:
    """``mcp_clients`` 读写仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        client_id: str,
        name: str,
        token_hash: str,
        token_prefix: str,
        scopes: list[str],
        created_by: str,
        expires_at: datetime | None = None,
    ) -> McpClientModel:
        """追加一个客户端凭据;``name`` / ``client_id`` 重复抛具名冲突。"""
        row = McpClientModel(
            client_id=client_id,
            name=name,
            token_hash=token_hash,
            token_prefix=token_prefix,
            scopes=list(scopes),
            created_by=created_by,
            expires_at=expires_at,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as exc:
            raise McpClientConflictError(
                f"MCP 客户端 name={name!r} / client_id={client_id!r} 已存在"
            ) from exc
        return row

    async def get_by_client_id(self, client_id: str) -> McpClientModel | None:
        result = await self._session.execute(
            select(McpClientModel).where(McpClientModel.client_id == client_id)
        )
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> McpClientModel | None:
        result = await self._session.execute(
            select(McpClientModel).where(McpClientModel.name == name)
        )
        return result.scalar_one_or_none()

    async def get_by_token_hash(self, token_hash: str) -> McpClientModel | None:
        """按 token 摘要查找(是否 revoked / expired 由调用方判定)。"""
        result = await self._session.execute(
            select(McpClientModel).where(McpClientModel.token_hash == token_hash)
        )
        return result.scalars().first()

    async def list_all(self) -> list[McpClientModel]:
        result = await self._session.execute(
            select(McpClientModel).order_by(McpClientModel.id.asc())
        )
        return list(result.scalars())

    async def revoke(
        self, client_id: str, *, revoked_at: datetime | None = None
    ) -> McpClientModel | None:
        """吊销客户端(幂等:已吊销的保留首次吊销时间)。"""
        row = await self.get_by_client_id(client_id)
        if row is None:
            return None
        if row.revoked_at is None:
            row.revoked_at = revoked_at or datetime.now(UTC)
            await self._session.flush()
        return row

    async def touch_last_used(
        self, client_id: str, *, when: datetime | None = None
    ) -> None:
        row = await self.get_by_client_id(client_id)
        if row is None or row.revoked_at is not None:
            return
        row.last_used_at = when or datetime.now(UTC)
        await self._session.flush()

    async def count_active(self, *, now: datetime | None = None) -> int:
        """未吊销且未过期的客户端数量(HTTP 启动凭据检查)。"""
        moment = now or datetime.now(UTC)
        result = await self._session.execute(
            select(func.count(McpClientModel.id)).where(
                McpClientModel.revoked_at.is_(None),
                (McpClientModel.expires_at.is_(None))
                | (McpClientModel.expires_at > moment),
            )
        )
        return int(result.scalar_one())


__all__ = ["McpClientConflictError", "McpClientRepository"]
