"""finboard-research-kit —— 研究代码沙箱工具包(issue #216)。

因子执行协议 v1(纯截面函数,无状态)的容器侧实现:数据上下文、输出
契约与 harness。版本与沙箱镜像 tag 绑定(``finboard-research-sandbox:<version>``)。
"""

from finboard_research_kit.context import FactorContext
from finboard_research_kit.result import FactorResult, OutputContractError

__version__ = "0.1.0"

__all__ = [
    "FactorContext",
    "FactorResult",
    "OutputContractError",
    "__version__",
]
