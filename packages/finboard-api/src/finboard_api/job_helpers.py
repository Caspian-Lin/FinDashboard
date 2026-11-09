"""统一后台任务队列的共享 enqueue 助手(issue #143 / #144)。

各语义化端点(批量下载 / 特征快照 / 数据集发布 / 回测 / 数据同步 / 全量拉取 /
质量修复 / 研究运行)在自己的路由里调 :func:`enqueue_job` 登记一个 ``queued``
任务并立即返回 202 + ``JobOut``,**不在 HTTP 请求内执行任务**(由独立 worker
进程消费)。

issue #513:FastAPI 无关的核心已抽到 ``finboard_app.background_jobs``
(``enqueue_job_core``,返回 ``(row, created)``),MCP 侧不再 import 本模块。这里
只剩 HTTP 包装:``response.status_code = 200``(幂等命中)与 ``JobOut`` 序列化。

边界:只写 ``background_jobs`` 表,与实盘 orders / fills / positions / audit_logs
完全隔离。
"""

from __future__ import annotations

from typing import Any

from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.job_schemas import JobOut
from finboard_app.background_jobs import enqueue_job_core, payload_checksum

#: 兼容再导出:测试与外部调用方按 ``finboard_api.job_helpers.BackgroundJobRepository``
#: 定位仓储(monkeypatch 目标);实现已迁移到 ``finboard_app.background_jobs``。
from finboard_persistence import BackgroundJobRepository


async def enqueue_job(
    session: AsyncSession,
    response: Response | None,
    *,
    kind: str,
    queue: str,
    idempotency_key: str,
    payload: dict[str, Any],
    requested_by: str,
    priority: int = 0,
    max_attempts: int = 3,
) -> JobOut:
    """在同事务内幂等创建一个 ``queued`` 任务,返回 ``JobOut``。

    * 共用 ``idempotency_key``:同 key 已存在则返回旧记录,``response.status_code``
      置 200(新建默认 202),调用方据此区分"首次提交"与"幂等命中";
    * 不 commit(调用方在路由里统一 commit / rollback),保证与其他写入原子。
    """
    row, created = await enqueue_job_core(
        session,
        kind=kind,
        queue=queue,
        idempotency_key=idempotency_key,
        payload=payload,
        requested_by=requested_by,
        priority=priority,
        max_attempts=max_attempts,
    )
    if response is not None and not created:
        response.status_code = 200
    return JobOut.model_validate(row)


__all__ = ["BackgroundJobRepository", "enqueue_job", "payload_checksum"]
