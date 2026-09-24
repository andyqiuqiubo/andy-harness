"""EventBus —— 异步发布/订阅事件总线，支持通配符主题。

命名规则: <域>.<...实体路径>.<动作>
通配符: `*` 匹配单段, `#` 匹配多段
例如: `model.*` 匹配 `model.request` 但不匹配 `model.request.delta`
      `model.#` 匹配 `model.request.delta`
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("harness.eventbus")

# 事件处理器类型（支持同步和异步）
EventHandler = Callable[[dict[str, Any]], Any]


def _match_topic(pattern: str, topic: str) -> bool:
    """AMQP 风格通配符匹配。

    `*` 匹配单段（不含 `.`），`#` 匹配多段（含 `.`）。
    """
    if pattern == topic:
        return True
    pattern_parts = pattern.split(".")
    topic_parts = topic.split(".")
    pi = 0
    ti = 0
    while pi < len(pattern_parts) and ti < len(topic_parts):
        p = pattern_parts[pi]
        if p == "#":
            # # 匹配零或多段
            if pi == len(pattern_parts) - 1:
                return True
            next_p = pattern_parts[pi + 1]
            for tj in range(ti, len(topic_parts)):
                if _match_topic(".".join(pattern_parts[pi + 1 :]), ".".join(topic_parts[tj:])):
                    return True
            return False
        elif p == "*":
            pi += 1
            ti += 1
        elif p == topic_parts[ti]:
            pi += 1
            ti += 1
        else:
            return False
    return pi == len(pattern_parts) and ti == len(topic_parts)


class EventBus:
    """异步事件总线。

    - 支持通配符主题订阅（fnmatch 语义）
    - publish 是 fire-and-forget，适用于旁路通知
    - 订阅返回 subscription_id，可用于注销
    - 插件停用时自动注销其所有订阅
    """

    def __init__(self) -> None:
        self._handlers: dict[str, dict[str, EventHandler]] = defaultdict(dict)
        self._subscriptions_by_owner: dict[str, set[str]] = defaultdict(set)
        self._sub_topics: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        topic: str,
        handler: EventHandler,
        owner: str = "",
    ) -> str:
        """订阅主题，返回 subscription_id。

        Args:
            topic: 主题（支持通配符 `*` 单段 / `#` 多段）
            handler: 事件处理器（同步或异步）
            owner: 订阅者标识（通常为插件 id），用于批量注销
        """
        sub_id = str(uuid.uuid4())
        async with self._lock:
            self._handlers[topic][sub_id] = handler
            self._sub_topics[sub_id] = topic
            if owner:
                self._subscriptions_by_owner[owner].add(sub_id)
        return sub_id

    async def unsubscribe(self, sub_id: str) -> None:
        """按 subscription_id 注销订阅。"""
        async with self._lock:
            topic = self._sub_topics.pop(sub_id, None)
            if topic and topic in self._handlers:
                self._handlers[topic].pop(sub_id, None)
                if not self._handlers[topic]:
                    del self._handlers[topic]

    async def unsubscribe_all(self, owner: str) -> None:
        """注销某 owner 的所有订阅（插件停用时调用）。"""
        async with self._lock:
            sub_ids = self._subscriptions_by_owner.pop(owner, set())
            for sub_id in sub_ids:
                topic = self._sub_topics.pop(sub_id, None)
                if topic and topic in self._handlers:
                    self._handlers[topic].pop(sub_id, None)
                    if not self._handlers[topic]:
                        del self._handlers[topic]

    async def publish(self, topic: str, data: dict[str, Any]) -> None:
        """异步发布事件（fire-and-forget）。

        匹配规则: AMQP 风格通配符（`*` 单段 / `#` 多段）。
        所有匹配的处理器被并发调用，单个处理器异常不影响其他。
        """
        matched: list[EventHandler] = []
        async with self._lock:
            for pattern, handlers in self._handlers.items():
                if _match_topic(pattern, topic):
                    matched.extend(handlers.values())

        if not matched:
            return

        # 支持同步和异步处理器
        async def _safe_call(h: EventHandler, data: dict[str, Any]) -> None:
            result = h(data)
            if inspect.isawaitable(result):
                await result

        results = await asyncio.gather(
            *[_safe_call(h, data) for h in matched], return_exceptions=True
        )
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning("事件处理器异常: %s — %s", matched[i], result)
