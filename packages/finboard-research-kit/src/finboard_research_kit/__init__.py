"""finboard-research-kit —— 研究代码沙箱工具包(issue #216;#218 增策略协议;
#359 增区间因子协议 v2)。

因子执行协议 v1(纯截面函数,无状态)、策略逐日决策协议 v1
(``strategy.decide(ctx) -> targets``,引擎回显当前权重)与因子区间协议
v2(``factor.compute_series(ctx) -> FactorSeries``,窗口内逐决策日截面,
逐日 PIT 由 ``bars_view`` / ``dataset_view`` 访问器契约承担)的容器侧
实现:数据上下文、输出契约与 harness。版本与沙箱镜像 tag 绑定
(``finboard-research-sandbox:<version>``)。
"""

from finboard_research_kit.context import (
    BarsView,
    DatasetView,
    FactorContext,
    FactorSeriesContext,
    StrategyConstraints,
    StrategyContext,
)
from finboard_research_kit.result import (
    FactorResult,
    FactorSeries,
    OutputContractError,
    StrategyResult,
)

__version__ = "0.3.1"

__all__ = [
    "BarsView",
    "DatasetView",
    "FactorContext",
    "FactorResult",
    "FactorSeries",
    "FactorSeriesContext",
    "OutputContractError",
    "StrategyConstraints",
    "StrategyContext",
    "StrategyResult",
    "__version__",
]
