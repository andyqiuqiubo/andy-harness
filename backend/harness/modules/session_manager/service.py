"""SessionService —— 会话管理服务。

提供会话 CRUD、重命名、归档、消息追加。
作为 ServicePlugin 注册到 ServiceRegistry。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from harness.infra.database import Database
from harness.infra.repository import (
    Message,
    MessageRepository,
    Session,
    SessionRepository,
)

logger = logging.getLogger("harness.session_manager")


class SessionService(Protocol):
    """会话服务接口（供其他插件通过 ServiceRegistry 调用）。"""

    def create_session(self, title: str = "New Session") -> Session: ...
    def get_session(self, session_id: str) -> Session | None: ...
    def list_sessions(self, include_archived: bool = False) -> list[Session]: ...
    def rename_session(self, session_id: str, title: str) -> Session | None: ...
    def archive_session(self, session_id: str) -> Session | None: ...
    def delete_session(self, session_id: str) -> bool: ...
    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        tokens: int = 0,
        latency_ms: int | None = None,
    ) -> Message: ...
    def list_messages(self, session_id: str) -> list[Message]: ...
    def get_message(self, message_id: str) -> Message | None: ...


class SessionServiceImpl:
    """会话服务实现。"""

    def __init__(self, db: Database) -> None:
        self._session_repo = SessionRepository(db)
        self._message_repo = MessageRepository(db)

    def create_session(self, title: str = "New Session") -> Session:
        """创建会话。"""
        session = Session(title=title)
        return self._session_repo.create(session)

    def get_session(self, session_id: str) -> Session | None:
        """获取会话。"""
        return self._session_repo.get(session_id)

    def list_sessions(self, include_archived: bool = False) -> list[Session]:
        """列出会话。"""
        return self._session_repo.list_sessions(include_archived)

    def rename_session(self, session_id: str, title: str) -> Session | None:
        """重命名会话。"""
        return self._session_repo.rename(session_id, title)

    def archive_session(self, session_id: str) -> Session | None:
        """归档会话。"""
        return self._session_repo.archive(session_id)

    def delete_session(self, session_id: str) -> bool:
        """删除会话（同时删除所有消息）。"""
        self._message_repo.delete_by_session(session_id)
        return self._session_repo.delete(session_id)

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        tokens: int = 0,
        latency_ms: int | None = None,
    ) -> Message:
        """追加消息到会话。"""
        message = Message(
            session_id=session_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            tokens=tokens,
            latency_ms=latency_ms,
        )
        return self._message_repo.create(message)

    def list_messages(self, session_id: str) -> list[Message]:
        """列出会话的所有消息。"""
        return self._message_repo.list_by_session(session_id)

    def get_message(self, message_id: str) -> Message | None:
        """获取单条消息。"""
        return self._message_repo.get(message_id)
