"""finboard-data: 历史行情数据源抽象与实现。

akshare / yfinance / pyarrow 均为 lazy import,Linux CI 环境无需安装。
"""

from finboard_data.akshare_provider import AkShareProvider
from finboard_data.base import HistoricalDataProvider
from finboard_data.symbols import (
    SymbolEntry,
    SymbolPoolConfig,
    load_symbol_pool,
    save_symbol_pool,
)
from finboard_data.yfinance_provider import YFinanceProvider

__all__ = [
    "AkShareProvider",
    "HistoricalDataProvider",
    "SymbolEntry",
    "SymbolPoolConfig",
    "YFinanceProvider",
    "load_symbol_pool",
    "save_symbol_pool",
]
