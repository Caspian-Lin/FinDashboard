"""finboard-app:进程入口 / CLI / 配置 / 组装根。"""

from __future__ import annotations

import os

# NumPy 会在首次导入时初始化 OpenBLAS。包初始化先于 ``bootstrap`` /
# ``config``,后者的依赖链可能经 ``psycopg.types.numpy`` 提前加载 NumPy;
# 因此这个界必须放在包级导入之前,CLI 模块级设置已经太晚(#471)。研究
# 并行度由分块/进程池提供,BLAS 内部多线程会在 Windows 上造成超额订阅及
# 原生 LAPACK 并发挂死。保留显式环境覆盖,便于受控压测与运维调优。
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from finboard_app.bootstrap import KernelComponents, build_kernel_components
from finboard_app.config import Settings, load_settings
from finboard_app.logging import setup_logging

__all__ = [
    "KernelComponents",
    "Settings",
    "build_kernel_components",
    "load_settings",
    "setup_logging",
]
