"""executor 共享运行时助手(issue #144)。

代码版本探测、路径默认值等在多个数据域 executor(feature_snapshot /
dataset_publish)里复用的小工具。集中在本模块避免重复实现。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_DEFAULT_CACHE_DIR = "data_cache"
_DEFAULT_RELEASE_ROOT = "data_releases"


def code_version() -> str:
    """返回服务端代码版本(不接受 payload 伪造)。

    优先读 ``FINBOARD_CODE_VERSION`` 环境变量;否则用 ``git rev-parse`` 取短 commit,
    工作区脏时附 ``-dirty``。复刻 ``routes/instruments._current_code_version``。
    """
    configured = os.getenv("FINBOARD_CODE_VERSION")
    if configured:
        return configured
    result = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return "unknown"
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        check=False,
        capture_output=True,
        text=True,
    )
    return f"{value}-dirty" if dirty.returncode == 0 and dirty.stdout.strip() else value


def cache_dir() -> Path:
    """本地 Parquet 缓存目录(可被 ``FINBOARD_DATA_CACHE_DIR`` 覆盖)。"""
    return Path(os.getenv("FINBOARD_DATA_CACHE_DIR", _DEFAULT_CACHE_DIR))


def release_root() -> Path:
    """冻结发布根目录(可被 ``FINBOARD_DATA_RELEASE_ROOT`` 覆盖)。"""
    return Path(os.getenv("FINBOARD_DATA_RELEASE_ROOT", _DEFAULT_RELEASE_ROOT))


__all__ = ["cache_dir", "code_version", "release_root"]
