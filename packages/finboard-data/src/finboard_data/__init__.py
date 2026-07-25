"""finboard-data: 历史行情数据源抽象与实现。

akshare / pyarrow 均为 lazy import,Linux CI 环境无需安装。
"""

from finboard_data.akshare_provider import AkShareProvider
from finboard_data.base import HistoricalDataProvider

__all__ = ["AkShareProvider", "HistoricalDataProvider"]
