"""finboard-app:进程入口 / CLI / 配置 / 组装根。"""

# BLAS 安全钩子必须先于其余导入;后续 imports 的 E402 属于有意顺序。
# ruff: noqa: E402

from __future__ import annotations

from finboard_shared.runtime import enforce_single_thread_blas

# NumPy 会在首次导入时初始化 OpenBLAS。包初始化先于 ``bootstrap`` /
# ``config``,后者的依赖链可能经 ``psycopg.types.numpy`` 提前加载 NumPy;
# 因此这个界必须放在包级导入之前,CLI 模块级设置已经太晚(#471)。研究
# 并行度由分块/进程池提供,BLAS 内部多线程会在 Windows 上造成超额订阅及
# 原生 LAPACK 并发挂死。生产路径不接受不安全的线程覆盖。
enforce_single_thread_blas()

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
