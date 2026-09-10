"""行情 provider 构造助手(issue #144)。

数据域 executor(bulk_download / backtest_run / quality_repair)需要
按 ``source`` + ``settings`` 构造行情 provider。本模块抽出与
``finboard_api.routes.data._get_provider`` 等价的工厂,供 executor 与 CLI 复用,
避免 ``finboard-backtest`` 反向依赖 ``finboard-api``。

settings 通过注入的 :func:`SettingsFactory` 延迟读取(默认从 ``finboard_app`` 加载),
敏感字段(tushare_token 等)不进入日志 / 审计。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from finboard_app.config import Settings
    from finboard_data import AkShareProvider, TushareBarProvider, YFinanceProvider

    BarProvider = AkShareProvider | TushareBarProvider | YFinanceProvider
    #: () -> Settings | None —— 延迟读取应用配置(worker 进程独立于 API 进程)。
    SettingsFactory = Callable[[], "Settings | None"]


#: 支持的行情源白名单(与 ``data.py`` 保持一致)。
SUPPORTED_BAR_PROVIDERS: frozenset[str] = frozenset({"akshare", "tushare", "yfinance"})


def resolve_provider_name(
    source: str | None,
    settings_factory: SettingsFactory,
) -> str:
    """解析并校验行情源,未知值直接抛 ``ValueError``(避免静默落到 yfinance)。"""
    import os

    settings = settings_factory()
    configured = settings.data_provider if settings is not None else None
    name = (
        source
        or configured
        or os.getenv("FINBOARD_DATA_PROVIDER")
        or "akshare"
    ).strip().lower()
    if name not in SUPPORTED_BAR_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_BAR_PROVIDERS))
        raise ValueError(f"不支持的行情源: {name}; 可用: {supported}")
    return name


def build_bar_provider(
    name: str,
    settings_factory: SettingsFactory,
    *,
    use_cache: bool = True,
    max_concurrency: int | None = None,
    request_interval: float | None = None,
) -> BarProvider:
    """按 ``name`` + settings 构造行情 provider。"""
    from finboard_data import AkShareProvider, TushareBarProvider, YFinanceProvider

    settings = settings_factory()
    if name == "akshare":
        return AkShareProvider(
            use_cache=use_cache,
            max_concurrency=max_concurrency or 2,
            request_interval=request_interval if request_interval is not None else 0.5,
        )
    if name == "tushare":
        return TushareBarProvider(
            token=settings.tushare_token if settings is not None else None,
            use_cache=use_cache,
            requests_per_minute=(
                settings.tushare_requests_per_minute if settings is not None else 200
            ),
            daily_request_limit=(
                settings.tushare_daily_request_limit if settings is not None else 100_000
            ),
            usage_file=(
                settings.tushare_usage_file
                if settings is not None
                else "data_cache/tushare_usage.json"
            ),
            max_concurrency=max_concurrency or 16,
        )
    return YFinanceProvider(
        use_cache=use_cache,
        max_concurrency=max_concurrency or 3,
        request_interval=request_interval if request_interval is not None else 0.3,
    )


def default_settings_factory() -> Settings | None:
    """默认 settings 工厂:延迟从 ``finboard_app`` 加载配置。"""
    from finboard_app.config import load_settings

    try:
        return load_settings()
    except Exception:
        return None


__all__ = [
    "SUPPORTED_BAR_PROVIDERS",
    "build_bar_provider",
    "default_settings_factory",
    "resolve_provider_name",
]
