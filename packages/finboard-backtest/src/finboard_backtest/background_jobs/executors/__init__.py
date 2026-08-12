"""内置执行器(issue #117 / #142 / #143)。

* ``echo`` —— 把 payload 原样回显到 result_ref,验证 API → DB 队列 → worker
  领取 → 执行 → 状态收口的端到端链路(#142 基础设施自检)。
* ``research_run`` —— 把 ResearchRunCoordinator 接入统一队列,让 queued 研究
  运行终于有人消费(#143);策略适配器由 CLI 注入。
"""

from finboard_backtest.background_jobs.executors.echo import EchoExecutor
from finboard_backtest.background_jobs.executors.research_run import (
    ResearchRunExecutor,
)

__all__ = ["EchoExecutor", "ResearchRunExecutor"]
