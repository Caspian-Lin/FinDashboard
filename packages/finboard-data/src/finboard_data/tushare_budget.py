"""Tushare 请求频率与每日额度的进程内共享预算。

每次远端调用前先持久化预占一次额度。这样即使请求失败或进程随后退出,
计数也只会偏保守,不会因重启而重新获得一份虚假的每日额度。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")

_REPLACE_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.4)


def _replace_with_retry(source: Path, destination: Path) -> None:
    """os.replace 带短退避重试。

    Windows 上另一进程(API 预算快照端点)恰好持有目标文件读句柄时,
    ``os.replace`` 报 WinError 5 拒绝访问;该读句柄是瞬态的,短退避后
    重试即可收敛。重试用尽仍失败则按原样抛出(fail-visible)。
    """
    for delay in _REPLACE_RETRY_DELAYS:
        try:
            source.replace(destination)
            return
        except PermissionError:
            time.sleep(delay)
    source.replace(destination)


class TushareRequestLimitError(RuntimeError):
    """Tushare 本地请求预算已耗尽。"""


@dataclass(frozen=True, slots=True)
class TushareBudgetSnapshot:
    """当前进程配置的 Tushare 请求预算快照。"""

    date: str
    requests_per_minute: int
    daily_limit: int
    used: int

    @property
    def remaining(self) -> int:
        """今日尚可预占的请求数。"""
        return max(0, self.daily_limit - self.used)


class TushareBudget(Protocol):
    """供 Provider 注入测试预算的最小协议。"""

    async def acquire(self) -> None:
        """预占一次远端请求。"""
        ...


class TushareRequestBudget:
    """按固定最小间隔限速,并把每日用量持久化到本地。"""

    def __init__(
        self,
        *,
        requests_per_minute: int = 200,
        daily_request_limit: int = 100_000,
        usage_file: str | Path = "data_cache/tushare_usage.json",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if requests_per_minute < 1:
            raise ValueError("requests_per_minute 必须 >= 1")
        if daily_request_limit < 1:
            raise ValueError("daily_request_limit 必须 >= 1")
        self._requests_per_minute = requests_per_minute
        self._minimum_interval = 60.0 / requests_per_minute
        self._daily_request_limit = daily_request_limit
        self._usage_file = Path(usage_file)
        self._now = now or (lambda: datetime.now(_SHANGHAI))
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._recent_requests: deque[float] = deque(maxlen=500)

    async def acquire(self) -> None:
        """等待频率窗口并在调用远端前持久化预占一次额度。"""
        async with self._lock:
            loop = asyncio.get_running_loop()
            elapsed = loop.time() - self._last_request_at
            if elapsed < self._minimum_interval:
                await asyncio.sleep(self._minimum_interval - elapsed)

            # 从时隙预留时刻计算下一次间隔,把预算文件 I/O 包含在 0.3 秒
            # (200 RPM)窗口内。远端调用仍发生在持久化成功之后;I/O 超过
            # 最小间隔时锁本身会串行化请求,不会导致突破配置上限。
            reserved_at = loop.time()
            self._last_request_at = reserved_at

            today = self._now().astimezone(_SHANGHAI).date().isoformat()
            used = await asyncio.to_thread(self._read_usage, today)
            if used >= self._daily_request_limit:
                raise TushareRequestLimitError(
                    f"Tushare 每日请求预算已耗尽: {used}/{self._daily_request_limit}"
                )
            await asyncio.to_thread(self._write_usage, today, used + 1)
            now = loop.time()
            self._recent_requests.append(now)
            cutoff = now - 60.0
            while self._recent_requests and self._recent_requests[0] < cutoff:
                self._recent_requests.popleft()

    @property
    def current_rpm(self) -> int:
        """最近 60 秒窗口内的实际请求数。"""
        return len(self._recent_requests)

    async def snapshot(self) -> TushareBudgetSnapshot:
        """读取当前日期的本地预算用量,不占用请求额度。"""
        today = self._now().astimezone(_SHANGHAI).date().isoformat()
        used = await asyncio.to_thread(self._read_usage, today)
        return TushareBudgetSnapshot(
            date=today,
            requests_per_minute=self._requests_per_minute,
            daily_limit=self._daily_request_limit,
            used=used,
        )

    def _read_usage(self, today: str) -> int:
        try:
            payload = json.loads(self._usage_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return 0
        if payload.get("date") != today:
            return 0
        try:
            return max(0, int(payload.get("requests", 0)))
        except (TypeError, ValueError):
            return 0

    def _write_usage(self, today: str, requests: int) -> None:
        self._usage_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._usage_file.with_suffix(self._usage_file.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"date": today, "requests": requests}, ensure_ascii=False),
            encoding="utf-8",
        )
        _replace_with_retry(temporary, self._usage_file)


_BUDGETS: dict[tuple[int, int, str], TushareRequestBudget] = {}


def shared_tushare_budget(
    *,
    requests_per_minute: int = 200,
    daily_request_limit: int = 100_000,
    usage_file: str | Path = "data_cache/tushare_usage.json",
) -> TushareRequestBudget:
    """同一进程内让行情与研究 Provider 共用一把限流锁。"""
    resolved = str(Path(usage_file).resolve())
    key = (requests_per_minute, daily_request_limit, resolved)
    budget = _BUDGETS.get(key)
    if budget is None:
        budget = TushareRequestBudget(
            requests_per_minute=requests_per_minute,
            daily_request_limit=daily_request_limit,
            usage_file=resolved,
        )
        _BUDGETS[key] = budget
    return budget
