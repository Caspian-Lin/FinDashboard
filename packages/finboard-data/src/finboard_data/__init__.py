"""finboard-data: 历史行情与时点化研究数据源抽象及实现。

akshare / yfinance / tushare / pyarrow 均为 lazy import,Linux CI 环境无需安装。
"""

from finboard_data.akshare_provider import AkShareProvider
from finboard_data.base import HistoricalDataProvider
from finboard_data.research import (
    DailySecurityMetrics,
    FinancialIndicator,
    IndustryMembership,
    InstrumentProfile,
    ResearchDataConfigurationError,
    ResearchDataContractError,
    ResearchDataDependencyError,
    ResearchDataError,
    ResearchDataProvider,
    ResearchDataUpstreamError,
)
from finboard_data.symbols import (
    SymbolEntry,
    SymbolPoolConfig,
    load_symbol_pool,
    save_symbol_pool,
)
from finboard_data.tushare_provider import TushareResearchDataProvider
from finboard_data.yfinance_provider import YFinanceProvider

__all__ = [
    "AkShareProvider",
    "DailySecurityMetrics",
    "FinancialIndicator",
    "HistoricalDataProvider",
    "IndustryMembership",
    "InstrumentProfile",
    "ResearchDataConfigurationError",
    "ResearchDataContractError",
    "ResearchDataDependencyError",
    "ResearchDataError",
    "ResearchDataProvider",
    "ResearchDataUpstreamError",
    "SymbolEntry",
    "SymbolPoolConfig",
    "TushareResearchDataProvider",
    "YFinanceProvider",
    "load_symbol_pool",
    "save_symbol_pool",
]
