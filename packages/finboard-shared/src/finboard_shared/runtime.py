"""进程级运行时安全边界。"""

from __future__ import annotations

import os

from threadpoolctl import threadpool_limits  # type: ignore[import-untyped]

# Keep the limiter alive for the whole process.  ``threadpool_limits`` applies
# immediately to libraries already loaded and the environment value covers
# libraries imported later.
_BLAS_LIMITER: threadpool_limits | None = None


def enforce_single_thread_blas() -> None:
    """Fail-closed to one BLAS thread, including pre-imported NumPy."""

    global _BLAS_LIMITER
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    if _BLAS_LIMITER is None:
        _BLAS_LIMITER = threadpool_limits(limits=1, user_api="blas")


__all__ = ["enforce_single_thread_blas"]
