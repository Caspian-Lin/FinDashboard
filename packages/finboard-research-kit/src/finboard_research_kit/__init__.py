"""finboard-research-kit —— 研究代码沙箱工具包(issue #216;#218 增策略协议)。

因子执行协议 v1(纯截面函数,无状态)与策略逐日决策协议 v1
(``strategy.decide(ctx) -> targets``,引擎回显当前权重)的容器侧实现:
数据上下文、输出契约与 harness。版本与沙箱镜像 tag 绑定
(``finboard-research-sandbox:<version>``)。
"""

from finboard_research_kit.context import (
    FactorContext,
    StrategyConstraints,
    StrategyContext,
)
from finboard_research_kit.result import (
    FactorResult,
    OutputContractError,
    StrategyResult,
)

__version__ = "0.2.0"

__all__ = [
    "FactorContext",
    "FactorResult",
    "OutputContractError",
    "StrategyConstraints",
    "StrategyContext",
    "StrategyResult",
    "__version__",
]
