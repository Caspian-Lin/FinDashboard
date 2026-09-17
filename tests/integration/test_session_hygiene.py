"""会话卫生回归测试(issue #166)。

背景:2026-08-15 诊断发现 dev server 泄漏 ``idle in transaction`` 会话
(4 小时僵尸事务,最后查询为 ``research_dataset_releases`` 的 SELECT,
曾把交易内核的 ``UPDATE accounts`` 阻塞 3 小时)。根因是数据域路由
(``/api/instruments`` 元数据 / 研究数据发布、``/api/audit-logs``)挂在
kernel 共享的长生命周期 session 上,只读 GET 从不 commit/rollback,
事务在共享连接上永久悬挂。

本测试:通过完整 app(含 lifespan)发起成功与异常两类请求后,断言
``pg_stat_activity`` 没有残留的 ``idle in transaction`` 会话。
修复前(共享 session)该断言失败,修复后(每请求独立 session)通过。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

from tests.integration.conftest import ApiTestApp


async def _idle_in_transaction_count(engine: Any, query_fingerprint: str) -> int:
    """统计残留 idle-in-transaction 会话数(query 指纹过滤)。"""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT count(*)
                FROM pg_stat_activity
                WHERE state = 'idle in transaction'
                  AND query ILIKE :fingerprint
                """
            ),
            {"fingerprint": f"%{query_fingerprint}%"},
        )
        return int(result.scalar_one())


@pytest.mark.integration
async def test_dataset_releases_get_leaves_no_open_transaction(
    api: ApiTestApp,
    request: pytest.FixtureRequest,
) -> None:
    """成功路径:研究数据页 GET 之后无残留事务(曾泄漏的端点)。"""
    _api_engine = request.getfixturevalue("_api_engine")
    resp = await api.client.get("/api/instruments/datasets/releases")
    assert resp.status_code == 200
    # 等事件循环让连接归还池后再断言(请求响应返回时 session 已关闭,
    # 但 pool 的 reset 是同步完成的,这里再让出一次确保稳定)。

    await asyncio.sleep(0.1)
    count = await _idle_in_transaction_count(
        _api_engine, "research_dataset_releases"
    )
    assert count == 0, (
        f"GET /api/instruments/datasets/releases 后残留 {count} 个 "
        "idle-in-transaction 会话(数据域路由不得在共享 session 上悬挂事务)"
    )


@pytest.mark.integration
async def test_audit_logs_get_leaves_no_open_transaction(
    api: ApiTestApp,
    request: pytest.FixtureRequest,
) -> None:
    """成功路径:审计日志 GET 无残留事务。"""
    _api_engine = request.getfixturevalue("_api_engine")
    resp = await api.client.get("/api/audit-logs")
    assert resp.status_code == 200

    await asyncio.sleep(0.1)
    count = await _idle_in_transaction_count(_api_engine, "audit_logs")
    assert count == 0


@pytest.mark.integration
async def test_exception_path_leaves_no_open_transaction(
    api: ApiTestApp,
    request: pytest.FixtureRequest,
) -> None:
    """异常路径:请求在查询后抛 404,事务必须被 rollback 而非悬挂。"""
    _api_engine = request.getfixturevalue("_api_engine")
    resp = await api.client.get(
        "/api/instruments/datasets/releases/not-exist-release"
    )
    assert resp.status_code == 404

    await asyncio.sleep(0.1)
    count = await _idle_in_transaction_count(
        _api_engine, "research_dataset_releases"
    )
    assert count == 0
