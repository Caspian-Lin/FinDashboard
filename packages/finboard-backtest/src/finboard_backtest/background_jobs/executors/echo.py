"""``echo`` 执行器 —— 基础设施端到端自检(issue #142)。

不连 broker / 数据库 / 行情源,只读 payload 并把它摘要成 result_ref,
供 ``POST /api/jobs kind=echo → worker 消费 → GET /api/jobs/{id} 显示 succeeded``
的链路验证。``sleep_seconds`` payload 字段可触发可观测的运行态与协作式取消。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from finboard_backtest.background_jobs.contracts import (
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
)


class EchoExecutor:
    """``kind=echo`` 执行器。"""

    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        # 把冻结 payload 摘要成稳定 result_ref —— 模拟产物引用写入。
        digest = hashlib.sha256(
            json.dumps(job.payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        result_ref = f"echo:{digest}"
        sleep_raw: Any = job.payload.get("sleep_seconds", 0)
        steps_raw: Any = job.payload.get("steps", 1)
        try:
            sleep_seconds = float(sleep_raw) if sleep_raw is not None else 0.0
            steps = max(1, int(steps_raw) if steps_raw is not None else 1)
        except (TypeError, ValueError):
            sleep_seconds = 0.0
            steps = 1
        await progress(0, steps, "echo:start")
        for i in range(1, steps + 1):
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds / steps)
            await progress(i, steps, f"echo:step-{i}")
        return JobResult(
            status="succeeded",
            result_ref=result_ref,
            progress_total=steps,
        )


# JobExecutor 是 runtime_checkable Protocol,直接用类即满足结构子类型。
_: type[JobExecutor] = EchoExecutor

__all__ = ["EchoExecutor"]
