"""Repository 抽象层 —— 会话与消息的数据访问。

通过 Repository 抽象层保留切换 Postgres 或其他存储的能力。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from harness.infra.database import Database

logger = logging.getLogger("harness.repository")


class Session:
    """会话数据模型。"""

    def __init__(
        self,
        id: str | None = None,
        title: str = "New Session",
        config: dict[str, Any] | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
        archived: bool = False,
    ) -> None:
        self.id: str = id or str(uuid.uuid4())
        self.title: str = title
        self.config: dict[str, Any] = config or {}
        self.created_at: str = created_at or datetime.now().isoformat()
        self.updated_at: str = updated_at or datetime.now().isoformat()
        self.archived: bool = archived

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "id": self.id,
            "title": self.title,
            "config": self.config,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived": self.archived,
        }


class Message:
    """消息数据模型。"""

    def __init__(
        self,
        id: str | None = None,
        session_id: str = "",
        role: str = "user",
        content: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        tokens: int = 0,
        latency_ms: int | None = None,
        created_at: str | None = None,
    ) -> None:
        self.id: str = id or str(uuid.uuid4())
        self.session_id: str = session_id
        self.role: str = role
        self.content: str = content
        self.tool_calls: list[dict[str, Any]] = tool_calls or []
        self.tool_call_id: str | None = tool_call_id
        self.tokens: int = tokens
        self.latency_ms: int | None = latency_ms
        self.created_at: str = created_at or datetime.now().isoformat()

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "latency_ms": self.latency_ms,
            "created_at": self.created_at,
        }


class ContextSnapshot:
    """上下文快照数据模型。"""

    def __init__(
        self,
        id: str | None = None,
        session_id: str = "",
        message_id: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        token_count: int = 0,
        budget: int | None = None,
        created_at: str | None = None,
    ) -> None:
        self.id: str = id or str(uuid.uuid4())
        self.session_id: str = session_id
        self.message_id: str | None = message_id
        self.messages: list[dict[str, Any]] = messages or []
        self.token_count: int = token_count
        self.budget: int | None = budget
        self.created_at: str = created_at or datetime.now().isoformat()


class SessionRepository:
    """会话 Repository。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, session: Session) -> Session:
        """创建会话。"""
        self._db.execute(
            "INSERT INTO sessions (id, title, config_json, created_at, updated_at, archived) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                session.id,
                session.title,
                json.dumps(session.config),
                session.created_at,
                session.updated_at,
                1 if session.archived else 0,
            ),
        )
        logger.info("会话已创建: %s", session.id)
        return session

    def get(self, session_id: str) -> Session | None:
        """获取会话。"""
        row = self._db.query_one(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        if not row:
            return None
        return self._row_to_session(row)

    def list_sessions(self, include_archived: bool = False) -> list[Session]:
        """列出会话。"""
        if include_archived:
            rows = self._db.query("SELECT * FROM sessions ORDER BY updated_at DESC")
        else:
            rows = self._db.query(
                "SELECT * FROM sessions WHERE archived = 0 ORDER BY updated_at DESC"
            )
        return [self._row_to_session(r) for r in rows]

    def update(self, session: Session) -> Session:
        """更新会话。"""
        session.updated_at = datetime.now().isoformat()
        self._db.execute(
            "UPDATE sessions SET title = ?, config_json = ?, updated_at = ?, archived = ? "
            "WHERE id = ?",
            (
                session.title,
                json.dumps(session.config),
                session.updated_at,
                1 if session.archived else 0,
                session.id,
            ),
        )
        return session

    def rename(self, session_id: str, title: str) -> Session | None:
        """重命名会话。"""
        session = self.get(session_id)
        if not session:
            return None
        session.title = title
        return self.update(session)

    def archive(self, session_id: str) -> Session | None:
        """归档会话。"""
        session = self.get(session_id)
        if not session:
            return None
        session.archived = True
        return self.update(session)

    def delete(self, session_id: str) -> bool:
        """删除会话。"""
        self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return True

    @staticmethod
    def _row_to_session(row: Any) -> Session:
        """数据库行转 Session 对象。"""
        return Session(
            id=row["id"],
            title=row["title"],
            config=json.loads(row["config_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived=bool(row["archived"]),
        )


class MessageRepository:
    """消息 Repository。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, message: Message) -> Message:
        """追加消息。"""
        # 将 tool_call_id 存入 tool_calls_json 中
        if message.tool_call_id:
            extra = {"tool_call_id": message.tool_call_id}
            if message.tool_calls:
                extra["tool_calls"] = message.tool_calls
            tool_calls_json = json.dumps(extra)
        elif message.tool_calls:
            tool_calls_json = json.dumps(message.tool_calls)
        else:
            tool_calls_json = None

        self._db.execute(
            "INSERT INTO messages (id, session_id, role, content, tool_calls_json, "
            "tokens, latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message.id,
                message.session_id,
                message.role,
                message.content,
                tool_calls_json,
                message.tokens,
                message.latency_ms,
                message.created_at,
            ),
        )
        logger.info("消息已追加: session=%s, role=%s", message.session_id, message.role)
        return message

    def list_by_session(self, session_id: str) -> list[Message]:
        """列出会话的所有消息（按时间排序）。"""
        rows = self._db.query(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        )
        return [self._row_to_message(r) for r in rows]

    def get(self, message_id: str) -> Message | None:
        """获取单条消息。"""
        row = self._db.query_one(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        )
        if not row:
            return None
        return self._row_to_message(row)

    def delete_by_session(self, session_id: str) -> int:
        """删除会话的所有消息。"""
        cursor = self._db.execute(
            "DELETE FROM messages WHERE session_id = ?", (session_id,)
        )
        return cursor.rowcount

    @staticmethod
    def _row_to_message(row: Any) -> Message:
        """数据库行转 Message 对象。"""
        tool_calls_json = row["tool_calls_json"]
        tool_calls: list[dict[str, Any]] = []
        tool_call_id: str | None = None
        if tool_calls_json:
            parsed = json.loads(tool_calls_json)
            if isinstance(parsed, dict) and "tool_call_id" in parsed:
                tool_call_id = parsed["tool_call_id"]
                tool_calls = parsed.get("tool_calls", [])
            elif isinstance(parsed, list):
                tool_calls = parsed
        return Message(
            id=row["id"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            tokens=row["tokens"],
            latency_ms=row["latency_ms"],
            created_at=row["created_at"],
        )


class ContextSnapshotRepository:
    """上下文快照 Repository。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, snapshot: ContextSnapshot) -> ContextSnapshot:
        """保存快照。"""
        self._db.execute(
            "INSERT INTO context_snapshots (id, session_id, message_id, messages_json, "
            "token_count, budget, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot.id,
                snapshot.session_id,
                snapshot.message_id,
                json.dumps(snapshot.messages),
                snapshot.token_count,
                snapshot.budget,
                snapshot.created_at,
            ),
        )
        return snapshot

    def list_by_session(self, session_id: str) -> list[ContextSnapshot]:
        """列出会话的上下文快照。"""
        rows = self._db.query(
            "SELECT * FROM context_snapshots WHERE session_id = ? ORDER BY created_at DESC",
            (session_id,),
        )
        return [self._row_to_snapshot(r) for r in rows]

    def get_latest(self, session_id: str) -> ContextSnapshot | None:
        """获取最新的快照。"""
        row = self._db.query_one(
            "SELECT * FROM context_snapshots WHERE session_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        )
        if not row:
            return None
        return self._row_to_snapshot(row)

    @staticmethod
    def _row_to_snapshot(row: Any) -> ContextSnapshot:
        """数据库行转 ContextSnapshot 对象。"""
        return ContextSnapshot(
            id=row["id"],
            session_id=row["session_id"],
            message_id=row["message_id"],
            messages=json.loads(row["messages_json"]),
            token_count=row["token_count"],
            budget=row["budget"],
            created_at=row["created_at"],
        )
