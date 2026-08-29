"""研究代码沙箱执行(issue #216)。

L3 沙箱路线第二环:让已提交的因子代码「能跑且跑不坏」。组成:

* :mod:`data_mount` —— 从冻结发布按 decision_at 生成只读挂载数据
  (**PIT 由物理隔离保证**:挂载内容即决策时点之前的数据,容器内不存在
  未来数据文件),附挂载清单 manifest;
* :mod:`runner` —— :class:`ResearchSandboxRunner`,一次性 Docker 容器
  (``--network none`` / ``--read-only`` / ``--cap-drop ALL`` / 非 root /
  ``--pids-limit`` / CPU 与内存限额 / 墙钟超时 kill);
* :mod:`executor` —— ``kind=research_code_run`` 后台任务执行器,run 记录
  code commit x 数据 release x 输出 checksum 三向引用。

协议 v1 为纯截面函数(``factor.compute(ctx) -> FactorResult``),容器侧由
``finboard_research_kit.harness`` 装配上下文并校验输出。

边界:纯离线研究域,容器无网络无凭证,不触实盘任何组件;agent 产出仍须走
研究 → 回测 → OOS → 模拟 → 影子 → 小资金完整晋级链。
"""

from finboard_backtest.research_sandbox.data_mount import (
    DataMount,
    MountDataset,
    SandboxMountError,
    build_data_mount,
)
from finboard_backtest.research_sandbox.errors import SandboxError
from finboard_backtest.research_sandbox.executor import (
    ResearchCodeRunExecutor,
    ResearchCodeRunPayload,
)
from finboard_backtest.research_sandbox.runner import (
    DockerDriver,
    SandboxRunResult,
    SandboxRunSpec,
    SubprocessDockerDriver,
)

__all__ = [
    "DataMount",
    "DockerDriver",
    "MountDataset",
    "ResearchCodeRunExecutor",
    "ResearchCodeRunPayload",
    "SandboxError",
    "SandboxMountError",
    "SandboxRunResult",
    "SandboxRunSpec",
    "SubprocessDockerDriver",
    "build_data_mount",
]
