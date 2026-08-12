"""按 ``kind`` 注册后台任务执行器(issue #117 / #142)。"""

from __future__ import annotations

from finboard_backtest.background_jobs.contracts import JobExecutor


class UnknownJobKindError(KeyError):
    """worker 领取到一个没有注册执行器的 kind。"""


class JobExecutorRegistry:
    """执行器注册表。worker 启动时构造并 ``register`` 各 kind 的 executor。"""

    def __init__(self) -> None:
        self._executors: dict[str, JobExecutor] = {}

    def register(self, kind: str, executor: JobExecutor) -> None:
        if not kind:
            raise ValueError("kind 不能为空")
        self._executors[kind] = executor

    def get(self, kind: str) -> JobExecutor:
        try:
            return self._executors[kind]
        except KeyError as exc:  # pragma: no cover - 由调用方处理
            raise UnknownJobKindError(kind) from exc

    def has(self, kind: str) -> bool:
        return kind in self._executors

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(self._executors)


__all__ = ["JobExecutorRegistry", "UnknownJobKindError"]
