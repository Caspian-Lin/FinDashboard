"""A股交易日历。

节假日在 :data:`DEFAULT_HOLIDAYS` 中静态维护,覆盖 2024-2026 年法定假日。
实际生产中可通过 API(如 ``tushare`` / 交易所公告)定期更新。

``TradingCalendar`` 仅判断"是否交易日",不区分集合竞价 / 连续竞价 / 收盘阶段。
"""

from __future__ import annotations

from datetime import date, timedelta

# fmt: off
DEFAULT_HOLIDAYS: frozenset[date] = frozenset({
    # ---------- 2024 ----------
    date(2024, 1, 1),                                    # 元旦
    date(2024, 2, 9), date(2024, 2, 12), date(2024, 2, 13),
    date(2024, 2, 14), date(2024, 2, 15), date(2024, 2, 16),  # 春节
    date(2024, 4, 4), date(2024, 4, 5), date(2024, 4, 6),     # 清明
    date(2024, 5, 1), date(2024, 5, 2), date(2024, 5, 3),     # 劳动节
    date(2024, 6, 10),                                         # 端午
    date(2024, 9, 16), date(2024, 9, 17),                      # 中秋
    date(2024, 10, 1), date(2024, 10, 2), date(2024, 10, 3),
    date(2024, 10, 4), date(2024, 10, 7),                      # 国庆

    # ---------- 2025 ----------
    date(2025, 1, 1),                                          # 元旦
    date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30),
    date(2025, 1, 31), date(2025, 2, 3),                       # 春节
    date(2025, 4, 4), date(2025, 4, 5), date(2025, 4, 6),      # 清明
    date(2025, 5, 1), date(2025, 5, 2), date(2025, 5, 5),      # 劳动节
    date(2025, 5, 31), date(2025, 6, 2),                       # 端午
    date(2025, 10, 1), date(2025, 10, 2), date(2025, 10, 3),
    date(2025, 10, 6), date(2025, 10, 7), date(2025, 10, 8),   # 国庆+中秋

    # ---------- 2026 ----------
    date(2026, 1, 1),                                          # 元旦
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 23),
    date(2026, 2, 24),                                         # 春节
    date(2026, 4, 6), date(2026, 4, 7),                        # 清明
    date(2026, 5, 1),                                          # 劳动节
    date(2026, 6, 19),                                         # 端午
    date(2026, 9, 25),                                         # 中秋
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5),
    date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8),   # 国庆
})
# fmt: on


class TradingCalendar:
    """A股交易日历。

    Parameters
    ----------
    holidays:
        额外节假日集合(与 :data:`DEFAULT_HOLIDAYS` 取并集)。
        适合测试 mock 或动态加载最新假期。
    """

    def __init__(self, holidays: set[date] | None = None) -> None:
        self._holidays: frozenset[date] = (
            DEFAULT_HOLIDAYS | holidays if holidays else DEFAULT_HOLIDAYS
        )

    def is_trading_day(self, d: date) -> bool:
        """判断 *d* 是否为交易日(周一~周五 且 非节假日)。"""
        if d.weekday() >= 5:
            return False
        return d not in self._holidays

    def next_trading_day(self, d: date) -> date:
        """返回 *d* 之后的下一个交易日(不含 *d* 本身)。"""
        candidate = d + timedelta(days=1)
        while not self.is_trading_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def is_weekend(self, d: date) -> bool:
        return d.weekday() >= 5
