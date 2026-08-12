"""内置执行器(issue #117 / #142)。

本期只提供 ``echo`` —— 把 payload 原样回显到 result_ref,用于验证
API → DB 队列 → worker 领取 → 执行 → 状态收口的端到端链路。
业务执行器(research-run / feature-snapshot / bulk-download 等)由 #143 / #144 注册。
"""

from finboard_backtest.background_jobs.executors.echo import EchoExecutor

__all__ = ["EchoExecutor"]
