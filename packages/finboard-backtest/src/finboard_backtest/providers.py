"""回测行情 provider 构造(issue #257)。

REST(``run_backtest_and_persist``,同 ``backtest_run`` 执行器)、MCP 同步路径
两处构造点共用本模块,保证 ``data_fallback_provider`` 回退语义在三入口
(REST 入队 / MCP 同步 / MCP 异步入队)一致:主源对某标的返回空或抛错时,
按 settings 配置的备用源回退取数(典型:tushare 2000 积分拉不到 ETF,
回退 akshare 缓存 / 接口)。

settings 为 ``None``(加载失败)时按无回退构造 primary,行为与 #257 之前
一致;本模块不读取 os.environ——pydantic-settings 不会把 .env 写回环境变量。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from finboard_app.config import Settings
    from finboard_data.base import HistoricalDataProvider

#: 回测可用行情源白名单(与 ``background_jobs.executors._providers`` 一致)。
BACKTEST_BAR_PROVIDERS: frozenset[str] = frozenset({"akshare", "tushare", "yfinance"})


def build_backtest_bar_provider(
    provider_name: str,
    settings: Settings | None,
) -> HistoricalDataProvider:
    """按名称构造回测行情 provider;配置 ``data_fallback_provider`` 时包裹回退。"""
    primary = _build_primary(provider_name, settings)
    fallback_name = _resolve_fallback_name(settings, provider_name)
    if fallback_name is None:
        return primary
    from finboard_data import FallbackBarProvider

    return FallbackBarProvider(
        primary=primary,
        primary_name=provider_name,
        fallback=_build_primary(fallback_name, settings),
        fallback_name=fallback_name,
    )


def _resolve_fallback_name(
    settings: Settings | None,
    primary_name: str,
) -> str | None:
    configured = (settings.data_fallback_provider if settings is not None else "") or ""
    fallback = configured.strip().lower()
    if fallback not in BACKTEST_BAR_PROVIDERS or fallback == primary_name:
        return None
    return fallback


def _build_primary(
    provider_name: str,
    settings: Settings | None,
) -> HistoricalDataProvider:
    from finboard_data import AkShareProvider, TushareBarProvider, YFinanceProvider

    if provider_name == "tushare":
        return TushareBarProvider(
            token=settings.tushare_token if settings is not None else None,
            use_cache=True,
            requests_per_minute=(
                settings.tushare_requests_per_minute if settings is not None else 200
            ),
            daily_request_limit=(
                settings.tushare_daily_request_limit if settings is not None else 100_000
            ),
            usage_file=(
                settings.tushare_usage_file if settings is not None else "data_cache/tushare_usage.json"
            ),
        )
    if provider_name == "yfinance":
        return YFinanceProvider()
    if provider_name != "akshare":
        supported = ", ".join(sorted(BACKTEST_BAR_PROVIDERS))
        raise ValueError(f"不支持的行情源: {provider_name}; 可用: {supported}")
    return AkShareProvider()


__all__ = ["BACKTEST_BAR_PROVIDERS", "build_backtest_bar_provider"]
