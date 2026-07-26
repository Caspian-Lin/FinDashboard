"""模拟时钟 —— 回测中替换 ``datetime.now()``。

让策略 / 风控在回测中感知"当前回放到了哪一天"。
"""

from __future__ import annotations

from datetime import UTC, datetime


class SimulatedClock:
    """可控时间源。

    BacktestEngine 在每根 Bar 回放前调用 ``advance_to(ts)``,
    所有组件通过 ``now()`` 获取当前回测时间。
    """

    def __init__(self) -> None:
        self._current: datetime = datetime(2000, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def advance_to(self, ts: datetime) -> None:
        if ts > self._current:
            self._current = ts
