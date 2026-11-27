"""API 关闭路径韧性(#518 收尾):kernel session 提交失败只回滚 + 告警。

CI 全量跑 `test_api_e2e` 时,teardown 的 ``await session.commit()`` 可能撞上
关闭期被取消的 flush(kernel session 已 rollback-required),抛
``PendingRollbackError`` 把正常关闭变成 teardown 失败。本测试直接验证抽出的
:func:`finboard_api.app._commit_kernel_session_on_shutdown`:
失败必须吞掉异常、执行 rollback、并带 ``exc_info=True`` 记警告。
"""

from __future__ import annotations

from typing import Any

from finboard_api.app import _commit_kernel_session_on_shutdown


class _PoisonedSession:
    """模拟 rollback-required 的 session:commit 必抛,rollback 记录调用。"""

    def __init__(self) -> None:
        self.rolled_back = False
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1
        raise RuntimeError("flush cancelled during shutdown")

    async def rollback(self) -> None:
        self.rolled_back = True


class _HealthySession:
    def __init__(self) -> None:
        self.rolled_back = False

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        self.rolled_back = True


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[dict[str, Any]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.warnings.append({"event": event, **kwargs})


async def test_shutdown_commit_failure_rolls_back_and_warns() -> None:
    session = _PoisonedSession()
    logger = _RecordingLogger()

    # 不得抛出:关闭期伪失败不能变成 teardown 异常。
    await _commit_kernel_session_on_shutdown(session, logger)  # type: ignore[arg-type]

    assert session.commits == 1
    assert session.rolled_back is True
    assert len(logger.warnings) == 1
    warning = logger.warnings[0]
    assert warning["event"] == "api.kernel_session_shutdown_rollback"
    # 可观测性取舍:根因仍在日志里(exc_info=True),只是不向外抛。
    assert warning["exc_info"] is True


async def test_shutdown_commit_success_does_not_rollback() -> None:
    session = _HealthySession()
    logger = _RecordingLogger()

    await _commit_kernel_session_on_shutdown(session, logger)  # type: ignore[arg-type]

    assert session.rolled_back is False
    assert logger.warnings == []


async def test_shutdown_commit_failure_tolerates_rollback_failure() -> None:
    class _BrokenRollback(_PoisonedSession):
        async def rollback(self) -> None:
            self.rolled_back = True
            raise RuntimeError("rollback also failed")

    session = _BrokenRollback()
    logger = _RecordingLogger()

    await _commit_kernel_session_on_shutdown(session, logger)  # type: ignore[arg-type]

    # rollback 自身失败也不得外抛;告警仍然写出。
    assert session.rolled_back is True
    assert logger.warnings[0]["event"] == "api.kernel_session_shutdown_rollback"
