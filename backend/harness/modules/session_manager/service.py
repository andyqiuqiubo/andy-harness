"""SessionService —— 会话管理服务。

提供会话 CRUD、重命名、归档、消息追加。
作为 ServicePlugin 注册到 ServiceRegistry。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
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
    def count_messages(self, session_id: str) -> int: ...
    def delete_message(self, message_id: str) -> bool: ...
    def delete_turn(self, session_id: str, user_message_id: str) -> int: ...
    def export_session(self, session_id: str) -> dict[str, Any] | None: ...
    def import_session(
        self, payload: dict[str, Any], title: str | None = None
    ) -> Session: ...
    def fork_session(
        self, session_id: str, at_message_id: str | None = None
    ) -> Session | None: ...


class SessionServiceImpl:
    """会话服务实现。"""

    def __init__(self, db: Database, services: Any = None) -> None:
        self._session_repo = SessionRepository(db)
        self._message_repo = MessageRepository(db)
        # 可选：服务注册表，用于读取附属数据（如任务清单）。
        # 未注入时导出/导入仍可工作，只是不含 todo。
        self._services = services

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
        """删除会话（同时删除所有消息与附件）。"""
        # 清理附件落盘文件
        try:
            import shutil

            from harness.modules.attachment.limits import attachments_dir

            sess_dir = Path(attachments_dir()) / session_id
            if sess_dir.exists():
                shutil.rmtree(sess_dir, ignore_errors=True)
        except Exception:
            pass
        # 清理附件元信息
        try:
            from harness.infra.database import Database
            from harness.infra.repository import AttachmentRepository

            db = self._services.get(Database) if self._services else None
            if db is not None:
                AttachmentRepository(db).delete_by_session(session_id)
        except Exception:
            pass
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
        attachments: list[dict[str, Any]] | None = None,
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
            attachments=attachments,
        )
        return self._message_repo.create(message)

    def list_messages(self, session_id: str) -> list[Message]:
        """列出会话的所有消息。"""
        return self._message_repo.list_by_session(session_id)

    def get_message(self, message_id: str) -> Message | None:
        """获取单条消息。"""
        return self._message_repo.get(message_id)

    def count_messages(self, session_id: str) -> int:
        """统计会话的消息条数。"""
        return self._message_repo.count_by_session(session_id)

    def delete_message(self, message_id: str) -> bool:
        """物理删除单条消息。"""
        return self._message_repo.delete(message_id)

    def delete_turn(self, session_id: str, user_message_id: str) -> int:
        """物理删除以某条 user 消息开始的**整轮问答**。

        一轮 = 该 user 消息 + 其后紧随的所有 assistant/tool 消息，
        直到下一条 user 消息（不含）为止。这样删除后上下文仍然自洽
        （不会留下孤立的 tool 消息或"带 tool_calls 却无 tool 响应"的 assistant 消息）。

        Returns:
            删除的消息条数；-1 表示消息不存在；-2 表示该消息不是 user 消息。
        """
        messages = self.list_messages(session_id)
        idx = next(
            (i for i, m in enumerate(messages) if m.id == user_message_id), None
        )
        if idx is None:
            return -1
        if messages[idx].role != "user":
            return -2
        end = idx + 1
        while end < len(messages) and messages[end].role != "user":
            end += 1
        ids = [m.id for m in messages[idx:end]]
        return self._message_repo.delete_many(ids)

    # ── 导出 / 导入 / 分支 ────────────────────────────

    def export_session(self, session_id: str) -> dict[str, Any] | None:
        """导出会话（元数据 + 全部消息 + 任务清单）为可移植结构。"""
        session = self._session_repo.get(session_id)
        if session is None:
            return None
        todos = self._load_todos(session_id)
        return {
            "version": 1,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "session": {
                "title": session.title,
                "config": session.config,
                "created_at": session.created_at,
                "archived": session.archived,
            },
            # Message.to_dict() 不含 tool_call_id（它存在 tool_calls_json 里），
            # 导出必须补上，否则工具消息导入后失去关联，上下文会错乱。
            "messages": [
                {
                    **m.to_dict(),
                    **({"tool_call_id": m.tool_call_id} if m.tool_call_id else {}),
                }
                for m in self.list_messages(session_id)
            ],
            "todos": todos,
        }

    def import_session(
        self, payload: dict[str, Any], title: str | None = None
    ) -> Session:
        """从导出结构导入为一个新会话（不覆盖任何现有数据）。"""
        raw_session = payload.get("session") or {}
        new_title = title or raw_session.get("title") or "导入的会话"
        session = self.create_session(new_title)

        imported = 0
        for raw in payload.get("messages") or []:
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role", "user"))
            if role not in ("user", "assistant", "system", "tool"):
                role = "user"
            self.append_message(
                session_id=session.id,
                role=role,
                content=str(raw.get("content", "") or ""),
                tool_calls=raw.get("tool_calls"),
                tool_call_id=raw.get("tool_call_id"),
                tokens=int(raw.get("tokens", 0) or 0),
                latency_ms=raw.get("latency_ms"),
            )
            imported += 1

        self._save_todos(session.id, payload.get("todos") or [])
        logger.info("会话导入完成: %s（%d 条消息）", session.id, imported)
        return session

    def fork_session(
        self, session_id: str, at_message_id: str | None = None
    ) -> Session | None:
        """从某条消息处分叉出新会话（含该消息及其之前的历史）。"""
        session = self._session_repo.get(session_id)
        if session is None:
            return None

        messages = self.list_messages(session_id)
        if at_message_id:
            cut = next(
                (i for i, m in enumerate(messages) if m.id == at_message_id), None
            )
            if cut is None:
                return None
            # 截断点若落在"带 tool_calls 的 assistant 消息"上，必须把它之后的
            # tool 消息一并带上：否则新会话会出现「有 tool_calls 却没有对应
            # tool 响应」的非法上下文，下一轮请求会被模型 API 拒绝。
            end = cut + 1
            while end < len(messages) and messages[end].role == "tool":
                end += 1
            messages = messages[:end]

        forked = self.create_session(f"{session.title}（分支）")
        for m in messages:
            self.append_message(
                session_id=forked.id,
                role=m.role,
                content=m.content or "",
                tool_calls=m.tool_calls,
                tool_call_id=m.tool_call_id,
                tokens=m.tokens,
                latency_ms=m.latency_ms,
            )

        self._save_todos(forked.id, self._load_todos(session_id))
        logger.info(
            "会话分叉完成: %s -> %s（%d 条消息）", session_id, forked.id, len(messages)
        )
        return forked

    # ── 任务清单读写（导出/导入的附属数据） ────────────

    def _load_todos(self, session_id: str) -> list[dict[str, Any]]:
        """读取会话任务清单（Todo 服务不可用时返回空列表）。"""
        try:
            service = self._todo_service()
        except Exception:
            return []
        return [t.to_dict() for t in service.list_todos(session_id)]

    def _save_todos(self, session_id: str, items: list[dict[str, Any]]) -> None:
        """写入会话任务清单（Todo 服务不可用时静默跳过）。

        丢弃来源 id：导入/分叉产生的是**新**记录，复用原 id 会撞主键。
        """
        if not items:
            return
        fresh: list[dict[str, Any]] = []
        for raw in items:
            if isinstance(raw, dict):
                fresh.append({k: v for k, v in raw.items() if k != "id"})
        if not fresh:
            return
        try:
            self._todo_service().replace_todos(session_id, fresh)
        except Exception as e:
            logger.warning("导入任务清单失败（已跳过）: %s", e)

    def _todo_service(self) -> Any:
        """从服务注册表解析 TodoService。"""
        from harness.modules.todo_manager.service import TodoService

        if self._services is None:
            raise RuntimeError("未注入服务注册表")
        return self._services.get(TodoService)
