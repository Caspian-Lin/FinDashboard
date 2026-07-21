"""进程内事件总线(asyncio pub-sub)。

设计取舍:
* **不**引入 Kafka / Redis Streams;P0 单进程下 in-memory 足够;
* 同步 publish(顺序调用订阅者,一个失败不影响后续 —— 失败被记录但不上抛);
* 订阅者签名 ``async def handler(event) -> None``;
* 事件类型严格(按 dataclass class 匹配),避免字符串 topic 拼写错误。
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

E = TypeVar("E")

Handler = Callable[[Any], Awaitable[None]]


class EventBus:
    """轻量级 pub-sub。

    事件类型作为订阅 key,任何 dataclass / class 都可以作为事件。
    """

    def __init__(self) -> None:
        self._handlers: dict[type[Any], list[Handler]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def subscribe(self, event_type: type[E], handler: Callable[[E], Awaitable[None]]) -> None:
        """注册一个 async handler。

        允许同一 handler 多次注册(在测试中常见),publish 时会被多次调用。
        """
        self._handlers[event_type].append(handler)

    def unsubscribe(
        self, event_type: type[E], handler: Callable[[E], Awaitable[None]]
    ) -> None:
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    async def publish(self, event: Any) -> None:
        """同步顺序调用所有订阅者;单个 handler 抛异常不影响其他订阅者。"""
        handlers = list(self._handlers.get(type(event), []))
        # 父类匹配:用 isinstance 兼容事件继承(P0 不用,但留出口)
        for event_type, extra in self._handlers.items():
            if event_type is type(event):
                continue
            if isinstance(event, event_type):
                handlers.extend(extra)
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception(
                    "EventBus handler %s 处理事件 %s 时抛异常",
                    getattr(handler, "__qualname__", handler),
                    type(event).__name__,
                )

    async def publish_nowait(self, event: Any) -> None:
        """与 ``publish`` 同义;保留别名以便区分未来可能的 ``publish_background``。"""
        await self.publish(event)

    def clear(self) -> None:
        self._handlers.clear()
