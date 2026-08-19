"""模拟时钟 —— 回测中替换 ``datetime.now()``。

让策略 / 风控在回测中感知"当前回放到了哪一天"。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo


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


def market_close(business_date: date) -> datetime:
    """该交易日的收盘时点(Asia/Shanghai 17:00)。

    回测域给决策 / 成交打时标的统一约定:日期部分即真实交易日,
    避免落到运行期 ``_utcnow()``(任务运行日)—— engine 的 selection
    ``decision_at`` 与 broker 的 ``Fill.filled_at`` 共用此函数。
    """
    return datetime.combine(
        business_date,
        time(hour=17, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
