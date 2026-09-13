"""issue #368 方案 A:决策窗口起点历史不足的报错具名最早可行决策起点。

fail-closed 语义零变化——拒绝照旧,只是 ValueError 文案追加「最早可行
决策起点」(发布交易日历上第 ``DEFAULT_MOMENTUM_LOOKBACK + 1`` 个交易日),
best effort:日历读不出 / 日历本身过短时降级为原文,不掩盖原始异常。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from finboard_backtest.research_run import signal_engine
from finboard_backtest.research_run.signal_engine import _compute_period_features
from finboard_data.releases import CloseHistoryColumns, ReleaseDatasetKind
from finboard_shared.types import AssetClass, Market

_DECISION_AT = datetime(2024, 1, 2, 7, 0, tzinfo=UTC)
#: 30 个交易日日历(2024-01-02 起);days[20](0 基)= 2024-01-30
_CALENDAR = [date(2024, 1, 2 + index) for index in range(30)]
_EXPECTED_EARLIEST = "2024-01-22"


@dataclass
class _StubInstrument:
    code: str
    name: str = "sample"
    market: Market = Market.A_SHARE
    asset_class: AssetClass = AssetClass.EQUITY


@dataclass
class _StubRelease:
    release_id: str
    instruments: tuple[_StubInstrument, ...]
    period: object = "d1"
    adjustment: str = "qfq"
    start_date: date = date(2024, 1, 2)
    end_date: date = date(2024, 3, 31)
    source: str = "stub"
    version: str = "v1"
    release_checksum: str = "c" * 64
    dataset_kind: object = ReleaseDatasetKind.BARS


@dataclass
class _StubUniverse:
    explicit_symbols: tuple[str, ...] | None = None


@dataclass
class _StubStrategySpec:
    universe: _StubUniverse = field(default_factory=_StubUniverse)


@dataclass
class _StubManifest:
    strategy_spec: _StubStrategySpec = field(default_factory=_StubStrategySpec)
    code_version: str = "v1"


@dataclass
class _StubProvider:
    """历史不足的发布:每标的只有决策日当天一根 close。"""

    release: _StubRelease
    closes_by_symbol: dict[str, dict[date, Decimal]]

    async def fetch_close_history(
        self,
        symbol: object,
        period: object,
        start: date,
        end: date,
        *,
        decision_at: datetime,
        adjust: str = "qfq",
    ) -> CloseHistoryColumns:
        del period, start, adjust
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        visible = [
            (day, close)
            for day, close in sorted((by_date or {}).items())
            if day <= end
            and datetime.combine(day, datetime.min.time(), tzinfo=UTC) <= decision_at
        ]
        return CloseHistoryColumns(
            dates=tuple(day for day, _ in visible),
            available_at=tuple(
                datetime.combine(day, datetime.min.time(), tzinfo=UTC)
                for day, _ in visible
            ),
            closes=np.array([float(close) for _, close in visible], dtype=np.float64),
            last_timestamp=(
                datetime.combine(visible[-1][0], datetime.min.time(), tzinfo=UTC)
                if visible
                else None
            ),
        )


def _provider() -> _StubProvider:
    return _StubProvider(
        release=_StubRelease(
            release_id="DR-warmup",
            instruments=(_StubInstrument("600000.SH"),),
        ),
        closes_by_symbol={"600000.SH": {date(2024, 1, 2): Decimal("10.0")}},
    )


def _manifest() -> _StubManifest:
    return _StubManifest()


@pytest.mark.parametrize(
    ("calendar", "expect_hint"),
    [
        (_CALENDAR, True),
        (_CALENDAR[:10], False),
    ],
)
async def test_warmup_error_names_earliest_feasible_start(
    monkeypatch: pytest.MonkeyPatch, calendar: list[date], expect_hint: bool
) -> None:
    """历史不足拒绝照旧;日历足够长时文案具名最早可行起点,过短时降级原文。"""

    async def _stub_trading_days(provider: Any) -> list[date]:
        del provider
        return list(calendar)

    monkeypatch.setattr(signal_engine, "_release_trading_days", _stub_trading_days)
    with pytest.raises(ValueError, match="无法从发布重算价格特征") as exc_info:
        await _compute_period_features(
            _provider(),  # type: ignore[arg-type]
            _manifest(),  # type: ignore[arg-type]
            _DECISION_AT,
            "DR-warmup",
        )
    message = str(exc_info.value)
    assert "通常发布起点历史不足" in message
    if expect_hint:
        assert f"最早可行决策起点 {_EXPECTED_EARLIEST}" in message
        assert "21 个交易日收盘" in message
    else:
        assert "最早可行决策起点" not in message


async def test_warmup_error_degrades_without_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日历读取失败:提示缺席,原始错误照常抛出(不掩盖、不二次炸)。"""

    async def _broken_trading_days(provider: Any) -> list[date]:
        del provider
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(signal_engine, "_release_trading_days", _broken_trading_days)
    with pytest.raises(ValueError, match="无法从发布重算价格特征") as exc_info:
        await _compute_period_features(
            _provider(),  # type: ignore[arg-type]
            _manifest(),  # type: ignore[arg-type]
            _DECISION_AT,
            "DR-warmup",
        )
    assert "最早可行决策起点" not in str(exc_info.value)
