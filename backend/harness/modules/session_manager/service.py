"""SessionService —— 会话管理服务。

提供会话 CRUD、重命名、归档、消息追加。
作为 ServicePlugin 注册到 ServiceRegistry。
"""

from __future__ import annotations

import logging
import uuid
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


def _sql_like(query: str) -> str:
    """转义用户输入用于 SQL LIKE（防 % / _ 被当通配符），返回 %...%。"""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _make_snippet(content: str, query: str, radius: int = 48) -> str:
    """取命中位置前后片段，折叠多余空白；未命中则取开头。"""
    text = " ".join(content.split())
    idx = text.lower().find(query.lower())
    if idx < 0:
        return text[: radius * 2] + ("…" if len(text) > radius * 2 else "")
    start = max(0, idx - radius)
    end = min(len(text), idx + len(query) + radius)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


class SessionService(Protocol):
    """会话服务接口（供其他插件通过 ServiceRegistry 调用）。"""

    def create_session(self, title: str = "New Session", user_id: str = "") -> Session: ...
    def get_session(self, session_id: str, user_id: str = "") -> Session | None: ...
    def list_sessions(self, include_archived: bool = False, user_id: str = "") -> list[Session]: ...
    def rename_session(self, session_id: str, title: str, user_id: str = "") -> Session | None: ...
    def archive_session(self, session_id: str, user_id: str = "") -> Session | None: ...
    def delete_session(self, session_id: str, user_id: str = "") -> bool: ...
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
        reasoning: str = "",
        parent_id: str | None = None,
    ) -> Message: ...
    def list_messages(self, session_id: str) -> list[Message]: ...
    def get_message(self, message_id: str) -> Message | None: ...
    def count_messages(self, session_id: str) -> int: ...
    def delete_message(self, message_id: str) -> bool: ...
    def delete_turn(self, session_id: str, user_message_id: str) -> int: ...
    def export_session(self, session_id: str, user_id: str = "") -> dict[str, Any] | None: ...
    def import_session(
        self,
        payload: dict[str, Any],
        title: str | None = None,
        user_id: str = "",
    ) -> Session: ...
    def fork_session(
        self,
        session_id: str,
        at_message_id: str | None = None,
        user_id: str = "",
    ) -> Session | None: ...
    def search_sessions(
        self,
        query: str,
        include_archived: bool = False,
        limit: int = 20,
        user_id: str = "",
    ) -> list[dict[str, Any]]: ...


class SessionServiceImpl:
    """会话服务实现。"""

    def __init__(self, db: Database, services: Any = None) -> None:
        self._session_repo = SessionRepository(db)
        self._message_repo = MessageRepository(db)
        # 可选：服务注册表，用于读取附属数据（如任务清单）。
        # 未注入时导出/导入仍可工作，只是不含 todo。
        self._services = services

    def create_session(self, title: str = "New Session", user_id: str = "") -> Session:
        """创建会话。"""
        session = Session(title=title)
        return self._session_repo.create(session, user_id=user_id)

    def get_session(self, session_id: str, user_id: str = "") -> Session | None:
        """获取会话。"""
        return self._session_repo.get(session_id, user_id=user_id)

    def list_sessions(self, include_archived: bool = False, user_id: str = "") -> list[Session]:
        """列出会话。"""
        return self._session_repo.list_sessions(include_archived, user_id=user_id)

    def rename_session(self, session_id: str, title: str, user_id: str = "") -> Session | None:
        """重命名会话。"""
        if user_id and self._session_repo.get(session_id, user_id=user_id) is None:
            return None
        return self._session_repo.rename(session_id, title)

    def archive_session(self, session_id: str, user_id: str = "") -> Session | None:
        """归档会话。"""
        if user_id and self._session_repo.get(session_id, user_id=user_id) is None:
            return None
        return self._session_repo.archive(session_id)

    def delete_session(self, session_id: str, user_id: str = "") -> bool:
        """删除会话（同时删除所有消息与附件）。"""
        if user_id and self._session_repo.get(session_id, user_id=user_id) is None:
            return False
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
        # 连带清理该会话的运行轨迹（tracing 插件；未启用时静默跳过）
        try:
            from harness.modules.tracing.service import SpanService

            if self._services is not None:
                span_svc = self._services.get(SpanService)
                span_svc.delete_by_session(session_id)
        except Exception:
            pass
        self._cleanup_orphans(session_id)
        return self._session_repo.delete(session_id)

    def _cleanup_orphans(self, session_id: str) -> None:
        """清理会话删除后残留的孤儿数据（无外键级联的四张表）。

        `artifacts` / `agent_runs` / `channel_links` / `memories` 均未声明
        ``ON DELETE CASCADE``，若不显式清理，删除会话后：
        - 工件大文件永久占用磁盘，且 UI 无法再找回或删除；
        - 运行快照（snapshot_json）持续膨胀；
        - 渠道映射指向已删除会话，后续渠道消息写入不存在的会话而静默丢失；
        - 会话级长期记忆成为不可见垃圾数据。

        全部步骤独立 try/except：任一环节失败不影响会话本身被删除。
        """
        db = self._services.get(Database) if self._services else None
        # 1) 工件：先删落盘文件再删元数据
        try:
            from harness.modules.artifact_store.service import ArtifactStore

            if self._services is not None and self._services.has(ArtifactStore):
                self._services.get(ArtifactStore).delete_by_session(session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("清理会话工件失败: %s", e)
        # 兜底：工件服务未注册时也要删掉元数据行
        if db is not None:
            try:
                db.execute("DELETE FROM artifacts WHERE session_id = ?", (session_id,))
            except Exception as e:  # noqa: BLE001
                logger.warning("清理 artifacts 元数据失败: %s", e)

        if db is None:
            return
        for table in ("agent_runs", "channel_links"):
            try:
                db.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
            except Exception as e:  # noqa: BLE001 —— 表不存在（插件未启用）时静默跳过
                logger.debug("清理 %s 失败（可能未建表）: %s", table, e)
        try:
            db.execute(
                "DELETE FROM memories WHERE scope = 'session' AND session_id = ?",
                (session_id,),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("清理会话级记忆失败: %s", e)

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
        reasoning: str = "",
        parent_id: str | None = None,
    ) -> Message:
        """追加消息到会话。

        parent_id：assistant 答案所回答的 user 消息 id。同一 user 消息下的多条
        assistant（首次回答 + 后续「重新回答」版本）据此归并为同一提问的版本组。
        """
        message = Message(
            session_id=session_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            tokens=tokens,
            latency_ms=latency_ms,
            attachments=attachments,
            reasoning=reasoning,
            parent_id=parent_id,
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
        """物理删除单条消息（连同其附件）。"""
        msg = self._message_repo.get(message_id)
        if msg is not None:
            # P1-2：先清理该消息挂载的附件，避免孤儿文件
            self._purge_attachments([msg])
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
        idx = next((i for i, m in enumerate(messages) if m.id == user_message_id), None)
        if idx is None:
            return -1
        if messages[idx].role != "user":
            return -2
        end = idx + 1
        while end < len(messages) and messages[end].role != "user":
            end += 1
        ids = [m.id for m in messages[idx:end]]
        # P1-2：删除消息行之前先回收其附件，避免孤儿文件与孤儿元数据
        self._purge_attachments(messages[idx:end])
        return self._message_repo.delete_many(ids)

    # ── 附件生命周期（P1-2 清理 / P1-3 复制） ─────────

    def _attachment_repo(self) -> Any:
        """解析 AttachmentRepository；不可用（未注入注册表 / 服务缺失）时返回 None。"""
        try:
            from harness.infra.database import Database
            from harness.infra.repository import AttachmentRepository

            db = self._services.get(Database) if self._services else None
            return AttachmentRepository(db) if db is not None else None
        except Exception as e:
            logger.warning("附件仓库不可用: %s", e)
            return None

    def _purge_attachments(self, messages: list[Message]) -> None:
        """删除若干消息所挂载的附件（磁盘文件 + 元数据）。

        P1-2：`delete_turn` / `delete_message` 过去只删 messages 行，
        附件文件与 attachments 表行成为双重孤儿（磁盘无界增长，且文件在
        会话被删之前始终可通过 URL 下载）。这里统一补上清理。
        """
        att_ids: list[str] = []
        seen: set[str] = set()
        for m in messages:
            for att in m.attachments or []:
                if isinstance(att, dict) and att.get("id"):
                    aid = str(att["id"])
                    if aid not in seen:
                        seen.add(aid)
                        att_ids.append(aid)
        if not att_ids:
            return
        repo = self._attachment_repo()
        if repo is None:
            return
        try:
            for row in repo.list_by_ids(att_ids):
                try:
                    file_path = Path(row.get("storage_path") or "")
                    if file_path.is_file():
                        file_path.unlink()
                except Exception as e:
                    logger.warning("删除附件文件失败（已跳过）: %s", e)
            repo.delete_by_ids(att_ids)
        except Exception as e:
            logger.warning("清理附件元数据失败（已跳过）: %s", e)

    def _clone_attachments_by_ids(self, dst_session_id: str, metas: Any | None) -> list[dict[str, Any]]:
        """把附件按 id **物理复制**到目标会话，返回新的公开元信息列表。

        P1-3：分叉/导入不能只是复用同一个 attachment id——那样新会话与
        原会话共享同一份磁盘文件，任何一方删除都会破坏另一方。这里 copy
        文件并登记新行。文件缺失时跳过该项（不阻断复制流程）。
        """
        clean = [m for m in (metas or []) if isinstance(m, dict) and m.get("id")]
        if not clean:
            return []
        repo = self._attachment_repo()
        if repo is None:
            return []
        try:
            from harness.modules.attachment.service import store_file

            ids = [str(m["id"]) for m in clean]
            rows = {str(r["id"]): r for r in repo.list_by_ids(ids)}
            cloned: list[dict[str, Any]] = []
            for meta in clean:
                row = rows.get(str(meta["id"]))
                if not row:
                    continue
                try:
                    raw = Path(row.get("storage_path") or "").read_bytes()
                except Exception:
                    continue
                new_id = uuid.uuid4().hex
                new_path = store_file(
                    dst_session_id,
                    raw,
                    row["filename"],
                    row["kind"],
                    row["mime"],
                    new_id,
                )
                cloned.append(
                    repo.create(
                        new_id,
                        dst_session_id,
                        row["kind"],
                        row["filename"],
                        row["mime"],
                        len(raw),
                        new_path,
                    )
                )
            return cloned
        except Exception as e:
            logger.warning("复制附件失败（已跳过）: %s", e)
            return []

    # ── 导出 / 导入 / 分支 ────────────────────────────

    def export_session(self, session_id: str, user_id: str = "") -> dict[str, Any] | None:
        """导出会话（元数据 + 全部消息 + 任务清单）为可移植结构。"""
        session = self._session_repo.get(session_id, user_id=user_id)
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
        self,
        payload: dict[str, Any],
        title: str | None = None,
        user_id: str = "",
    ) -> Session:
        """从导出结构导入为一个新会话（不覆盖任何现有数据）。"""
        raw_session = payload.get("session") or {}
        new_title = title or raw_session.get("title") or "导入的会话"
        session = self.create_session(new_title, user_id=user_id)

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
                # P1-3：导出包里的 attachments 属于**原会话**，必须物理复制到新会话，
                # 否则「导出 → 导入」静默丢附件，且共享 id 会让两边互相影响。
                attachments=self._clone_attachments_by_ids(session.id, raw.get("attachments")),
            )
            imported += 1

        self._save_todos(session.id, payload.get("todos") or [])
        logger.info("会话导入完成: %s（%d 条消息）", session.id, imported)
        return session

    def fork_session(
        self,
        session_id: str,
        at_message_id: str | None = None,
        user_id: str = "",
    ) -> Session | None:
        """从某条消息处分叉出新会话（含该消息及其之前的历史）。"""
        session = self._session_repo.get(session_id, user_id=user_id)
        if session is None:
            return None

        messages = self.list_messages(session_id)
        if at_message_id:
            cut = next((i for i, m in enumerate(messages) if m.id == at_message_id), None)
            if cut is None:
                return None
            # 截断点若落在"带 tool_calls 的 assistant 消息"上，必须把它之后的
            # tool 消息一并带上：否则新会话会出现「有 tool_calls 却没有对应
            # tool 响应」的非法上下文，下一轮请求会被模型 API 拒绝。
            end = cut + 1
            while end < len(messages) and messages[end].role == "tool":
                end += 1
            messages = messages[:end]

        forked = self.create_session(f"{session.title}（分支）", user_id=user_id)
        for m in messages:
            self.append_message(
                session_id=forked.id,
                role=m.role,
                content=m.content or "",
                tool_calls=m.tool_calls,
                tool_call_id=m.tool_call_id,
                tokens=m.tokens,
                latency_ms=m.latency_ms,
                # P1-3：分叉出的会话需要**自己的**附件副本，不能复用原 id
                # （复用会让新会话与原会话共享磁盘文件，删除任一方都会破坏另一方）
                attachments=self._clone_attachments_by_ids(forked.id, m.attachments),
            )

        self._save_todos(forked.id, self._load_todos(session_id))
        logger.info("会话分叉完成: %s -> %s（%d 条消息）", session_id, forked.id, len(messages))
        return forked

    # ── 搜索（E9：会话标题 / 消息内容） ─────────────────

    def search_sessions(
        self,
        query: str,
        include_archived: bool = False,
        limit: int = 20,
        user_id: str = "",
    ) -> list[dict[str, Any]]:
        """按会话标题或消息内容搜索会话。

        匹配标题（LIKE）或任一消息内容（LIKE）的会话，返回会话信息与
        最多 3 条命中消息的片段（高亮位置前后各取若干字符）。空查询返回空列表。
        `user_id` 非空时仅搜索归属该用户的会话。
        """
        q = (query or "").strip()
        if not q:
            return []
        like = _sql_like(q)

        where_extra: list[str] = []
        params_extra: list[Any] = []
        archived_clause = "" if include_archived else "AND s.archived = 0"
        if user_id:
            where_extra.append("s.user_id = ?")
            params_extra.append(user_id)
        extra_clause = (" AND " + " AND ".join(where_extra)) if where_extra else ""
        rows = self._session_repo._db.query(
            f"""
            SELECT s.id,
              MAX(CASE WHEN s.title LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END) AS title_hit,
              COUNT(m.id) AS msg_hits
            FROM sessions s
            LEFT JOIN messages m
              ON m.session_id = s.id AND m.content LIKE ? ESCAPE '\\'
            WHERE (s.title LIKE ? ESCAPE '\\' OR m.id IS NOT NULL)
              {archived_clause}
              {extra_clause}
            GROUP BY s.id
            ORDER BY s.updated_at DESC
            LIMIT ?
            """,
            (like, like, like, *params_extra, max(1, min(limit, 100))),
        )

        results: list[dict[str, Any]] = []
        for row in rows:
            sid = str(row["id"])
            session = self._session_repo.get(sid, user_id=user_id)
            if session is None:
                continue
            match_rows = self._message_repo._db.query(
                """
                SELECT id, role, content, created_at FROM messages
                WHERE session_id = ? AND content LIKE ? ESCAPE '\\'
                ORDER BY created_at ASC, rowid ASC
                LIMIT 3
                """,
                (sid, like),
            )
            matches = [
                {
                    "message_id": str(mr["id"]),
                    "role": str(mr["role"]),
                    "snippet": _make_snippet(str(mr["content"] or ""), q),
                    "created_at": mr["created_at"],
                }
                for mr in match_rows
            ]
            results.append(
                {
                    "session": {
                        **session.to_dict(),
                        "message_count": self._message_repo.count_by_session(sid),
                    },
                    "title_hit": bool(row["title_hit"]),
                    "msg_hits": int(row["msg_hits"]),
                    "matches": matches,
                }
            )
        return results

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
