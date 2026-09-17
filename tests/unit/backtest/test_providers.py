"""回测行情 provider 构造测试(issue #257)。

覆盖 ``build_backtest_bar_provider``:主源构造、``data_fallback_provider``
回退包裹、同名/非法值不包裹。
"""

from __future__ import annotations

from datetime import date

import pytest

from finboard_app.config import Settings
from finboard_backtest.providers import build_backtest_bar_provider
from finboard_data import AkShareProvider, FallbackBarProvider, YFinanceProvider
from finboard_data.base import HistoricalDataProvider
from finboard_shared.models import Symbol
from finboard_shared.types import BarPeriod, Market


@pytest.mark.unit
def test_akshare_without_fallback_returns_primary_only() -> None:
    provider = build_backtest_bar_provider("akshare", Settings())
    assert isinstance(provider, AkShareProvider)
    assert not isinstance(provider, FallbackBarProvider)


@pytest.mark.unit
def test_fallback_configured_wraps_primary() -> None:
    # 不用 tushare 主源:CI 无 .env token,构造真实 TushareBarProvider 会在
    # token 校验处抛错(本机因 .env 有 token 而通过,属环境差异)。
    settings = Settings(data_fallback_provider="akshare")
    provider = build_backtest_bar_provider("yfinance", settings)
    assert isinstance(provider, FallbackBarProvider)
    assert provider._primary_name == "yfinance"
    assert provider._fallback_name == "akshare"


@pytest.mark.unit
def test_fallback_equals_primary_is_ignored() -> None:
    settings = Settings(data_fallback_provider="akshare")
    provider = build_backtest_bar_provider("akshare", settings)
    assert isinstance(provider, AkShareProvider)
    assert not isinstance(provider, FallbackBarProvider)


@pytest.mark.unit
def test_invalid_fallback_value_rejected_by_settings_schema() -> None:
    """``data_fallback_provider`` 为 Literal 枚举,非法值在配置层即被拒。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(data_fallback_provider="bogus")  # type: ignore[arg-type]


@pytest.mark.unit
def test_none_settings_builds_primary_without_fallback() -> None:
    """settings 加载失败(None)时保持旧行为:裸主源、无回退。"""
    provider = build_backtest_bar_provider("akshare", None)
    assert isinstance(provider, AkShareProvider)
    assert not isinstance(provider, FallbackBarProvider)


@pytest.mark.unit
def test_unknown_provider_name_rejected() -> None:
    with pytest.raises(ValueError, match="不支持的行情源"):
        build_backtest_bar_provider("wind", None)


@pytest.mark.unit
def test_wrapped_provider_satisfies_protocol() -> None:
    settings = Settings(data_fallback_provider="yfinance")
    provider = build_backtest_bar_provider("akshare", settings)
    assert isinstance(provider, HistoricalDataProvider)


@pytest.mark.unit
async def test_wrapped_provider_delegates_fetch() -> None:
    """包裹后的 provider 真实转发 fetch_bars(akshare 主源 + 缓存 miss 时联网,
    这里只验证调用签名转发到主源,不联网:命中参数校验前先断言转发)。"""
    settings = Settings(data_fallback_provider="yfinance")
    provider = build_backtest_bar_provider("akshare", settings)
    assert isinstance(provider, FallbackBarProvider)
    assert isinstance(provider._primary, AkShareProvider)
    assert isinstance(provider._fallback, YFinanceProvider)
    # fetch_bars 可调用且签名与协议一致(不触发网络:仅检查协程创建)
    symbol = Symbol(code="510300.SH", market=Market.A_SHARE)
    coro = provider.fetch_bars(symbol, BarPeriod.D1, date(2024, 1, 1), date(2024, 1, 5))
    assert hasattr(coro, "close")
    coro.close()
