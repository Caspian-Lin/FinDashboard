"""沙箱执行失败分类(issue #216)。

可读错误码(写入 ``research_code_runs.error_code`` 与 background_jobs):

* ``static_validation_failed`` —— 执行前静态校验未过(#215 规则在执行端重放);
* ``runtime_error`` —— 容器内因子运行时异常(harness exit 4);
* ``timeout`` —— 超过墙钟超时被 kill;
* ``oom_killed`` —— 内存超限被 kill(OOMKilled / exit 137);
* ``output_contract_violation`` —— 输出不符合协议(exit 3 或输出缺失/不可解析);
* ``sandbox_unavailable`` —— Docker / 镜像不可用(可重试)。
"""

from __future__ import annotations

STATIC_VALIDATION_FAILED = "static_validation_failed"
RUNTIME_ERROR = "runtime_error"
TIMEOUT = "timeout"
OOM_KILLED = "oom_killed"
OUTPUT_CONTRACT_VIOLATION = "output_contract_violation"
SANDBOX_UNAVAILABLE = "sandbox_unavailable"

#: 可重试的失败码(其余视为确定性失败,不重试)
RETRYABLE_CODES = frozenset({SANDBOX_UNAVAILABLE})


class SandboxError(Exception):
    """沙箱执行失败(携带可读 error_code)。"""

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(f"[{code}] {summary}")
        self.code = code
        self.summary = summary

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


__all__ = [
    "OOM_KILLED",
    "OUTPUT_CONTRACT_VIOLATION",
    "RUNTIME_ERROR",
    "SANDBOX_UNAVAILABLE",
    "STATIC_VALIDATION_FAILED",
    "TIMEOUT",
    "SandboxError",
]
