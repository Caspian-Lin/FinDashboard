"""Tushare 请求频率与每日额度的进程内共享预算。

每次远端调用前先持久化预占一次额度。这样即使请求失败或进程随后退出,
计数也只会偏保守,不会因重启而重新获得一份虚假的每日额度。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class TushareRequestLimitError(RuntimeError):
    """Tushare 本地请求预算已耗尽。"""


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
        self._minimum_interval = 60.0 / requests_per_minute
        self._daily_request_limit = daily_request_limit
        self._usage_file = Path(usage_file)
        self._now = now or (lambda: datetime.now(_SHANGHAI))
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def acquire(self) -> None:
        """等待频率窗口并在调用远端前持久化预占一次额度。"""
        async with self._lock:
            loop = asyncio.get_running_loop()
            elapsed = loop.time() - self._last_request_at
            if elapsed < self._minimum_interval:
                await asyncio.sleep(self._minimum_interval - elapsed)

            today = self._now().astimezone(_SHANGHAI).date().isoformat()
            used = await asyncio.to_thread(self._read_usage, today)
            if used >= self._daily_request_limit:
                raise TushareRequestLimitError(
                    f"Tushare 每日请求预算已耗尽: {used}/{self._daily_request_limit}"
                )
            await asyncio.to_thread(self._write_usage, today, used + 1)
            self._last_request_at = loop.time()

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
        temporary.replace(self._usage_file)


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
