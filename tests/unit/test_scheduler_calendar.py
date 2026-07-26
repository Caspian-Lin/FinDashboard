"""TradingCalendar 单元测试。"""

from __future__ import annotations

from datetime import date

import pytest

from finboard_scheduler.calendar import DEFAULT_HOLIDAYS, TradingCalendar

pytestmark = pytest.mark.unit


class TestTradingCalendar:
    def test_weekday_is_trading_day(self) -> None:
        cal = TradingCalendar()
        assert cal.is_trading_day(date(2024, 3, 4))  # Monday

    def test_saturday_is_not_trading_day(self) -> None:
        cal = TradingCalendar()
        assert not cal.is_trading_day(date(2024, 3, 9))  # Saturday

    def test_sunday_is_not_trading_day(self) -> None:
        cal = TradingCalendar()
        assert not cal.is_trading_day(date(2024, 3, 10))  # Sunday

    def test_holiday_is_not_trading_day(self) -> None:
        cal = TradingCalendar()
        assert not cal.is_trading_day(date(2024, 1, 1))  # 元旦

    def test_chinese_new_year_2024(self) -> None:
        cal = TradingCalendar()
        for d in [
            date(2024, 2, 9),
            date(2024, 2, 12),
            date(2024, 2, 13),
            date(2024, 2, 14),
            date(2024, 2, 15),
            date(2024, 2, 16),
        ]:
            assert not cal.is_trading_day(d), f"{d} should be holiday"

    def test_national_day_2024(self) -> None:
        cal = TradingCalendar()
        assert not cal.is_trading_day(date(2024, 10, 1))
        assert not cal.is_trading_day(date(2024, 10, 7))

    def test_next_trading_day_from_friday(self) -> None:
        cal = TradingCalendar()
        friday = date(2024, 3, 8)  # Friday
        assert cal.next_trading_day(friday) == date(2024, 3, 11)  # Monday

    def test_next_trading_day_skips_holiday(self) -> None:
        cal = TradingCalendar()
        result = cal.next_trading_day(date(2024, 9, 30))
        assert cal.is_trading_day(result)
        assert result == date(2024, 10, 8)  # after national day holiday

    def test_next_trading_day_from_weekday(self) -> None:
        cal = TradingCalendar()
        monday = date(2024, 3, 4)
        assert cal.next_trading_day(monday) == date(2024, 3, 5)  # Tuesday

    def test_custom_holidays_merge(self) -> None:
        custom = {date(2024, 3, 5)}  # custom holiday on a Tuesday
        cal = TradingCalendar(holidays=custom)
        assert not cal.is_trading_day(date(2024, 3, 5))
        assert cal.is_trading_day(date(2024, 3, 6))

    def test_default_holidays_not_empty(self) -> None:
        assert len(DEFAULT_HOLIDAYS) > 30

    def test_is_weekend(self) -> None:
        cal = TradingCalendar()
        assert cal.is_weekend(date(2024, 3, 9))  # Saturday
        assert not cal.is_weekend(date(2024, 3, 4))  # Monday
