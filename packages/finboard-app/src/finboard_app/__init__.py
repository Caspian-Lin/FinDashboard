"""finboard-app:进程入口 / CLI / 配置 / 组装根。"""

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
