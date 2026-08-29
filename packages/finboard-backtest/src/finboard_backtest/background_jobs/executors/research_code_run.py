"""``research_code_run`` 执行器入口(issue #216,#136 kind 白名单扩展)。

实现本体在 :mod:`finboard_backtest.research_sandbox.executor`(与 data_mount /
runner 同域);本模块只做再导出,保持 ``executors`` 包的注册口径。
"""

from finboard_backtest.research_sandbox.executor import (
    ResearchCodeRunExecutor,
    ResearchCodeRunPayload,
)

__all__ = [
    "ResearchCodeRunExecutor",
    "ResearchCodeRunPayload",
]
