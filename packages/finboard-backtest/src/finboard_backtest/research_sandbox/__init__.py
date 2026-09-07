"""研究代码沙箱执行(issue #216;#359 增区间协议 v2)。

L3 沙箱路线第二环:让已提交的因子代码「能跑且跑不坏」。组成:

* :mod:`data_mount` —— 从冻结发布按 decision_at 生成只读挂载数据
  (**PIT 由物理隔离保证**:挂载内容即决策时点之前的数据,容器内不存在
  未来数据文件),附挂载清单 manifest;``build_window_data_mount`` 为
  区间协议(v2)的窗口挂载(清单 v3,上界 = window_end 日终);
* :mod:`runner` —— :class:`ResearchSandboxRunner`,一次性 Docker 容器
  (``--network none`` / ``--read-only`` / ``--cap-drop ALL`` / 非 root /
  ``--pids-limit`` / CPU 与内存限额 / 墙钟超时 kill);
  ``run_factor_series_container`` 为区间因子执行入口(#360 编排依赖);
* :mod:`executor` —— ``kind=research_code_run`` 后台任务执行器,run 记录
  code commit x 数据 release x 输出 checksum 三向引用;
* :mod:`audit` —— 前缀不变性审计引擎(truncation / perturbation),
  检出区间因子代码的逐日 PIT 契约违规。

协议 v1 为纯截面函数(``factor.compute(ctx) -> FactorResult``);协议 v2
(issue #359)为区间函数(``factor.compute_series(ctx) -> FactorSeries``,
窗口挂载 + 访问器契约 + 审计检出)。容器侧由
``finboard_research_kit.harness`` 装配上下文并校验输出。

边界:纯离线研究域,容器无网络无凭证,不触实盘任何组件;agent 产出仍须走
研究 → 回测 → OOS → 模拟 → 影子 → 小资金完整晋级链。
"""

from finboard_backtest.research_sandbox.data_mount import (
    DataMount,
    MountDataset,
    SandboxMountError,
    WindowDataMount,
    build_data_mount,
    build_window_data_mount,
)
from finboard_backtest.research_sandbox.errors import SandboxError
from finboard_backtest.research_sandbox.executor import (
    ResearchCodeRunExecutor,
    ResearchCodeRunPayload,
)
from finboard_backtest.research_sandbox.runner import (
    DockerDriver,
    FactorSeriesOutput,
    FactorSeriesRunSpec,
    SandboxRunResult,
    SandboxRunSpec,
    SubprocessDockerDriver,
    run_factor_series_container,
)

__all__ = [
    "DataMount",
    "DockerDriver",
    "FactorSeriesOutput",
    "FactorSeriesRunSpec",
    "MountDataset",
    "ResearchCodeRunExecutor",
    "ResearchCodeRunPayload",
    "SandboxError",
    "SandboxMountError",
    "SandboxRunResult",
    "SandboxRunSpec",
    "SubprocessDockerDriver",
    "WindowDataMount",
    "build_data_mount",
    "build_window_data_mount",
    "run_factor_series_container",
]
