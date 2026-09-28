"""memory_manager —— 长期记忆（跨会话记忆）服务。

对齐 Letta 的分层自编辑记忆 / OpenAI Agents SDK 的 built-in memory /
Deep Agents 的 store：把用户偏好、项目事实等**跨会话**信息持久化，
按需检索并注入，让 Agent 从「每次重新认识用户」进化为「记住你」。

- `global` 作用域：跨所有会话共享（默认）
- `session` 作用域：仅属于某个会话
"""

from .service import (
    SCOPE_GLOBAL,
    SCOPE_SESSION,
    MemoryRecord,
    MemoryService,
    MemoryServiceImpl,
)

__all__ = [
    "SCOPE_GLOBAL",
    "SCOPE_SESSION",
    "MemoryRecord",
    "MemoryService",
    "MemoryServiceImpl",
]
