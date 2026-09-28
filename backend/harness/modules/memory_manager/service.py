"""长期记忆服务 —— SQLite 持久化的 key/value 记忆，支持检索与注入。

设计要点：
- **零新增依赖**：标准库 + 项目既有 SQLite；`embedding` 列预留给未来的向量检索。
- **同 key 覆盖**：`save` 对相同 (scope, session_id, key) 的记录做 upsert，
  语义幂等，模型反复保存同一事实不会产生重复条目。
- **可检索**：`search` 对 key/value/tags 做大小写不敏感的 LIKE 匹配。
- **可注入**：`render_hint` 生成一小段系统提示，只取最近的若干条 global 记忆。
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from harness.infra.database import Database

SCOPE_GLOBAL = "global"
SCOPE_SESSION = "session"
_VALID_SCOPES = {SCOPE_GLOBAL, SCOPE_SESSION}

# 注入系统提示时的单条 value 截断长度与默认条数
_HINT_VALUE_LIMIT = 200
_HINT_DEFAULT_COUNT = 8


def _now_iso() -> str:
    """UTC 时间戳（带显式偏移），前端据此转本地时间展示。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_tags(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    return []


def _escape_like(q: str) -> str:
    """转义 LIKE 通配符，避免用户输入 `%`/`_` 被当作通配符匹配全部。"""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass
class MemoryRecord:
    """一条长期记忆。"""

    id: str
    key: str
    value: str
    scope: str
    session_id: str
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: Any) -> MemoryRecord:
        return cls(
            id=row["id"],
            key=row["key"],
            value=row["value"],
            scope=row["scope"],
            session_id=row["session_id"],
            tags=_parse_tags(row["tags"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "value": self.value,
            "scope": self.scope,
            "session_id": self.session_id,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class MemoryService(ABC):
    """长期记忆服务接口。"""

    @abstractmethod
    def save(
        self,
        key: str,
        value: str,
        scope: str = SCOPE_GLOBAL,
        session_id: str = "",
        tags: Any = None,
    ) -> MemoryRecord:
        """保存记忆（相同 key 覆盖）。"""

    @abstractmethod
    def get(self, memory_id: str) -> MemoryRecord | None:
        """按 id 获取。"""

    @abstractmethod
    def search(
        self, query: str, scope: str | None = None, limit: int = 10
    ) -> list[MemoryRecord]:
        """按关键词检索。"""

    @abstractmethod
    def list_memories(
        self,
        scope: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        """列出记忆。"""

    @abstractmethod
    def update(self, memory_id: str, **fields: Any) -> MemoryRecord | None:
        """更新记忆字段（key/value/tags/scope）。"""

    @abstractmethod
    def delete(self, memory_id: str) -> bool:
        """删除记忆。"""

    @abstractmethod
    def render_hint(self, limit: int = _HINT_DEFAULT_COUNT) -> str:
        """生成注入系统提示的长期记忆片段（global）。"""


class MemoryServiceImpl(MemoryService):
    """基于 SQLite 的长期记忆实现。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def save(
        self,
        key: str,
        value: str,
        scope: str = SCOPE_GLOBAL,
        session_id: str = "",
        tags: Any = None,
    ) -> MemoryRecord:
        key = str(key).strip()
        value = str(value)
        scope = scope if scope in _VALID_SCOPES else SCOPE_GLOBAL
        tag_list = _parse_tags(tags)
        tags_str = ",".join(tag_list)
        session_id = session_id if scope == SCOPE_SESSION else ""

        existing = self._db.query_one(
            "SELECT * FROM memories WHERE scope = ? AND session_id = ? AND key = ?",
            (scope, session_id, key),
        )
        now = _now_iso()
        if existing is not None:
            self._db.execute(
                "UPDATE memories SET value = ?, tags = ?, updated_at = ? WHERE id = ?",
                (value, tags_str, now, existing["id"]),
            )
            record = self.get(existing["id"])
            assert record is not None
            return record

        mid = f"mem_{uuid.uuid4().hex[:12]}"
        self._db.execute(
            "INSERT INTO memories "
            "(id, key, value, scope, session_id, tags, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, key, value, scope, session_id, tags_str, now, now),
        )
        record = self.get(mid)
        assert record is not None
        return record

    def get(self, memory_id: str) -> MemoryRecord | None:
        row = self._db.query_one("SELECT * FROM memories WHERE id = ?", (memory_id,))
        return MemoryRecord.from_row(row) if row is not None else None

    def search(
        self, query: str, scope: str | None = None, limit: int = 10
    ) -> list[MemoryRecord]:
        q = (query or "").strip()
        params: list[Any] = []
        where: list[str] = []
        if q:
            like = f"%{_escape_like(q)}%"
            where.append(
                "(key LIKE ? ESCAPE '\\' OR value LIKE ? ESCAPE '\\' "
                "OR tags LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like, like])
        if scope:
            where.append("scope = ?")
            params.append(scope)
        sql = "SELECT * FROM memories"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(max(1, limit))
        return [MemoryRecord.from_row(r) for r in self._db.query(sql, tuple(params))]

    def list_memories(
        self,
        scope: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        where: list[str] = []
        params: list[Any] = []
        if scope:
            where.append("scope = ?")
            params.append(scope)
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        sql = "SELECT * FROM memories"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(max(1, limit))
        return [MemoryRecord.from_row(r) for r in self._db.query(sql, tuple(params))]

    def update(self, memory_id: str, **fields: Any) -> MemoryRecord | None:
        record = self.get(memory_id)
        if record is None:
            return None
        sets: list[str] = []
        params: list[Any] = []
        if "key" in fields and fields["key"]:
            sets.append("key = ?")
            params.append(str(fields["key"]).strip())
        if "value" in fields and fields["value"] is not None:
            sets.append("value = ?")
            params.append(str(fields["value"]))
        if "tags" in fields:
            sets.append("tags = ?")
            params.append(",".join(_parse_tags(fields["tags"])))
        if "scope" in fields and fields["scope"] in _VALID_SCOPES:
            sets.append("scope = ?")
            params.append(fields["scope"])
        if not sets:
            return record
        sets.append("updated_at = ?")
        params.append(_now_iso())
        params.append(memory_id)
        self._db.execute(
            f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        return self.get(memory_id)

    def delete(self, memory_id: str) -> bool:
        if self.get(memory_id) is None:
            return False
        self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return True

    def render_hint(self, limit: int = _HINT_DEFAULT_COUNT) -> str:
        memories = self.list_memories(scope=SCOPE_GLOBAL, limit=limit)
        if not memories:
            return ""
        lines = [
            "[长期记忆] 以下是跨会话保存的长期事实（用户偏好 / 项目约定），相关时请参考："
        ]
        for m in memories:
            value = m.value
            if len(value) > _HINT_VALUE_LIMIT:
                value = value[:_HINT_VALUE_LIMIT] + "…"
            lines.append(f"- {m.key}: {value}")
        lines.append("（可用 memory_search 检索更多，或用 memory_save 追加新记忆。）")
        return "\n".join(lines)
