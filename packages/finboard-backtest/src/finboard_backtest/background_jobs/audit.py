"""Worker 侧研究审计钩子(issue #518)。

``finboard-backtest`` 不依赖 ``finboard-app``(反向依赖),因此 worker 执行器只
声明**结构协议**与中立事件;真正的审计写入由装配根(``finboard-app`` 的 CLI)
注入 :class:`~finboard_app.research_audit.ResearchAuditWorkerSink`。

协议语义:

* ``record`` 由执行器在**终态收口**调用(成功 / 失败 / 取消 / 重排);
* 实现方必须自行用独立会话写入并 commit,且**绝不抛出**(审计失败不能改变
  worker 的任务结果,更不能掩盖原始错误)。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class WorkerAuditEvent:
    """worker 侧一条研究审计事件(中立结构,不含 ORM / app 类型)。"""

    operation: str
    outcome: str
    worker_id: str
    correlation: Mapping[str, str] = field(default_factory=dict)
    entity_type: str | None = None
    entity_id: str | None = None
    result_code: str | None = None
    error_summary: str | None = None
    params_summary: Mapping[str, Any] = field(default_factory=dict)


class WorkerAuditSink(Protocol):
    """worker 执行器可注入的审计收口(实现必须吞掉自身失败)。"""

    async def record(self, event: WorkerAuditEvent) -> None: ...


__all__ = ["WorkerAuditEvent", "WorkerAuditSink"]
