"""统一后台任务执行器包(issue #117 / #142)。

只提供执行器协议 + 注册表 + worker 进程;真正的业务执行器(research-run /
feature-snapshot / bulk-download 等)由 #143 / #144 各自注册。
本期只内置一个 echo executor 验证端到端跑通。
"""

from finboard_backtest.background_jobs.contracts import (
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_backtest.background_jobs.registry import JobExecutorRegistry

__all__ = [
    "JobExecutor",
    "JobExecutorRegistry",
    "JobRecord",
    "JobResult",
    "ProgressCallback",
]
