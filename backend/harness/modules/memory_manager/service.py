"""长期记忆服务 —— SQLite 持久化的 key/value 记忆，支持向量检索与注入。

设计要点：
- **向量检索（E7）**：保存时为每条记忆生成嵌入写入 `embedding` 列，检索时按
  余弦相似度排序；默认用零依赖的 `HashingEmbedder`（可离线复现），
  也可注入 OpenAI 兼容嵌入器获得真正的语义（同义词 / 改写）检索。
- **优雅降级**：未配置嵌入器或向量无命中时，回退到 LIKE 关键词匹配。
- **同 key 覆盖**：`save` 对相同 (scope, session_id, key) 的记录做 upsert，
  语义幂等，模型反复保存同一事实不会产生重复条目。
- **可注入**：`render_hint` 生成一小段系统提示，只取最近的若干条 global 记忆。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from harness.infra.database import Database
from harness.modules.memory_manager.embedding import (
    Embedder,
    HashingEmbedder,
    cosine_similarity,
)

logger = logging.getLogger("harness.memory_manager")

SCOPE_GLOBAL = "global"
SCOPE_SESSION = "session"
_VALID_SCOPES = {SCOPE_GLOBAL, SCOPE_SESSION}

# 注入系统提示时的单条 value 截断长度与默认条数
_HINT_VALUE_LIMIT = 200
_HINT_DEFAULT_COUNT = 8

# 向量检索的默认最小相似度（低于此值视为不相关）。
_DEFAULT_MIN_SIMILARITY = 0.15

# ── SEC-18：向量检索热点保护 ──────────────────────────
# embedding 解码结果缓存条数上限（LRU）。选择依据：缓存的是「记忆条数」而非
# 请求数，单用户长期记忆一般几十到几百条，512 足以覆盖全量且内存可控
# （每条向量默认 256 维 float ≈ 数 KB）。
_EMBED_CACHE_MAX = 512
# 单次向量检索最多扫描的行数。向量检索无法在 SQL 侧剪枝，必须全表读取后逐条
# 比对；这里设一个安全上限，避免记忆表膨胀后单次检索把主线程拖死（超限仅告警，
# 仍按相关性排序返回，不静默丢弃「更新」的记忆——见 _vector_search）。
_MAX_VECTOR_SCAN_ROWS = 20_000

# ── tool_memory 总条数上限（SEC-34 归纳 / PLG 项） ──────────────────────────
# 记忆表无内置增长上限，长期运行的 agent 可能无限堆积（尤其 session 作用域的记忆）。
# 这里设一个全局总条数上限，超出后按 updated_at 最早的优先淘汰（环形淘汰）。
# 默认 10000 条，可通过 HARNESS_MEMORIES_MAX 调整；下限 100 防止误配导致功能失效。
_MAX_MEMORIES_TOTAL = 10_000


def _max_memories_total() -> int:
    try:
        v = int(os.environ.get("HARNESS_MEMORIES_MAX", str(_MAX_MEMORIES_TOTAL)))
        return max(100, v)
    except (TypeError, ValueError):
        return _MAX_MEMORIES_TOTAL


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
    user_id: str = ""

    @classmethod
    def from_row(cls, row: Any) -> MemoryRecord:
        keys = row.keys()
        return cls(
            id=row["id"],
            key=row["key"],
            value=row["value"],
            scope=row["scope"],
            session_id=row["session_id"],
            tags=_parse_tags(row["tags"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            user_id=(row["user_id"] if "user_id" in keys else "") or "",
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
            "user_id": self.user_id,
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
        user_id: str = "",
    ) -> MemoryRecord:
        """保存记忆（相同 key 覆盖）。"""

    @abstractmethod
    def get(self, memory_id: str, user_id: str = "") -> MemoryRecord | None:
        """按 id 获取（`user_id` 非空时校验归属）。"""

    @abstractmethod
    def search(
        self,
        query: str,
        scope: str | None = None,
        limit: int = 10,
        user_id: str = "",
    ) -> list[MemoryRecord]:
        """按关键词检索。"""

    @abstractmethod
    def list_memories(
        self,
        scope: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
        user_id: str = "",
    ) -> list[MemoryRecord]:
        """列出记忆。"""

    @abstractmethod
    def update(self, memory_id: str, user_id: str = "", **fields: Any) -> MemoryRecord | None:
        """更新记忆字段（key/value/tags/scope）。"""

    @abstractmethod
    def delete(self, memory_id: str, user_id: str = "") -> bool:
        """删除记忆。"""

    @abstractmethod
    def render_hint(self, user_id: str = "") -> str:
        """生成注入系统提示的长期记忆片段（global）。"""


class MemoryServiceImpl(MemoryService):
    """基于 SQLite 的长期记忆实现。"""

    def __init__(
        self,
        db: Database,
        embedder: Embedder | None = None,
        min_similarity: float = _DEFAULT_MIN_SIMILARITY,
    ) -> None:
        self._db = db
        # 默认用本地哈希嵌入器（零依赖、可离线）；显式传 None 可只保留 LIKE。
        self._embedder: Embedder | None = HashingEmbedder() if embedder is None else embedder
        self._min_similarity = min_similarity
        # embedding 解码缓存：row_id -> (embedding 原文, 向量)。以「原文」参与比较，
        # 记录被改写后原文变化即自动失效，无需依赖写入方全部记得清缓存。
        self._embed_cache: OrderedDict[str, tuple[Any, list[float]]] = OrderedDict()

    # ── embedding 解码缓存（SEC-18） ──────────────────
    def _embed_cache_put(self, row_id: str, raw: Any, vec: list[float]) -> None:
        self._embed_cache[row_id] = (raw, vec)
        self._embed_cache.move_to_end(row_id)
        while len(self._embed_cache) > _EMBED_CACHE_MAX:
            self._embed_cache.popitem(last=False)

    def _embed_cache_invalidate(self, memory_id: str) -> None:
        """写入路径调用：记录被新增/修改/删除后立刻失效，避免检索读到旧向量。"""
        self._embed_cache.pop(str(memory_id), None)

    def _embed_text(self, key: str, value: str, tags_str: str) -> str | None:
        """生成一条记忆的嵌入并序列化为 JSON 字符串。"""
        if self._embedder is None:
            return None
        text = f"{key} {value} {tags_str}".strip()
        vector = self._embedder.embed([text])[0]
        return json.dumps(vector, ensure_ascii=False)

    def save(
        self,
        key: str,
        value: str,
        scope: str = SCOPE_GLOBAL,
        session_id: str = "",
        tags: Any = None,
        user_id: str = "",
    ) -> MemoryRecord:
        key = str(key).strip()
        value = str(value)
        scope = scope if scope in _VALID_SCOPES else SCOPE_GLOBAL
        tag_list = _parse_tags(tags)
        tags_str = ",".join(tag_list)
        session_id = session_id if scope == SCOPE_SESSION else ""

        existing = self._db.query_one(
            "SELECT * FROM memories WHERE scope = ? AND session_id = ? AND key = ? AND user_id = ?",
            (scope, session_id, key, user_id),
        )
        embedding_json = self._embed_text(key, value, tags_str)
        now = _now_iso()
        if existing is not None:
            self._db.execute(
                "UPDATE memories SET value = ?, tags = ?, embedding = ?, updated_at = ? WHERE id = ?",
                (value, tags_str, embedding_json, now, existing["id"]),
            )
            self._embed_cache_invalidate(existing["id"])
            record = self.get(existing["id"], user_id=user_id)
            assert record is not None
            return record

        mid = f"mem_{uuid.uuid4().hex[:12]}"
        self._db.execute(
            "INSERT INTO memories "
            "(id, key, value, scope, session_id, tags, embedding, "
            "created_at, updated_at, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mid,
                key,
                value,
                scope,
                session_id,
                tags_str,
                embedding_json,
                now,
                now,
                user_id,
            ),
        )
        self._embed_cache_invalidate(mid)
        self._evict_if_over_cap()
        record = self.get(mid, user_id=user_id)
        assert record is not None
        return record

    def _evict_if_over_cap(self) -> None:
        """环形淘汰：记忆总条数超过上限时，按 updated_at 最早优先删除多余条目。

        仅作用于「新增」路径（update 不产生新行），且新建条目 updated_at=now 必为最新，
        不会被自身触发淘汰。下限保护在 :func:`_max_memories_total` 内。
        """
        cap = _max_memories_total()
        row = self._db.query_one("SELECT COUNT(*) AS n FROM memories")
        total = int(row["n"]) if row else 0
        if total <= cap:
            return
        excess = total - cap
        # 按物理插入顺序（rowid 单调自增）淘汰最早写入的条目，而非 updated_at ——
        # 因为 _now_iso() 精确到秒，高频写入会大量碰撞同一秒，updated_at 排序无法区分
        # 新旧，误删刚写入的条目；rowid 保证最新写入的永远在队尾，不会被自身淘汰。
        rows = self._db.query(
            "SELECT id FROM memories ORDER BY rowid ASC LIMIT ?",
            (excess,),
        )
        if not rows:
            return
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        self._db.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})", tuple(ids)
        )
        for rid in ids:
            self._embed_cache_invalidate(rid)
        logger.info("记忆总数 %d 超出上限 %d，已淘汰最早 %d 条", total, cap, excess)

    def get(self, memory_id: str, user_id: str = "") -> MemoryRecord | None:
        row = self._db.query_one("SELECT * FROM memories WHERE id = ?", (memory_id,))
        if row is None:
            return None
        record = MemoryRecord.from_row(row)
        if user_id and record.user_id != user_id:
            return None
        return record

    def search(
        self,
        query: str,
        scope: str | None = None,
        limit: int = 10,
        user_id: str = "",
    ) -> list[MemoryRecord]:
        """检索：向量相似度排序为主，无命中或未配置嵌入器时回退 LIKE。"""
        q = (query or "").strip()
        if not q:
            # 空查询：最近优先列出
            return self.list_memories(scope=scope, limit=limit, user_id=user_id)

        if self._embedder is not None:
            vector_hits = self._vector_search(q, scope, limit, user_id)
            if vector_hits:
                return vector_hits
            # 向量无命中（或查询无有效 token）→ 回退 LIKE，覆盖字面量字符。
        return self._like_search(q, scope, limit, user_id)

    def _vector_search(
        self,
        q: str,
        scope: str | None,
        limit: int,
        user_id: str = "",
    ) -> list[MemoryRecord]:
        """嵌入查询，按余弦相似度排序，过滤低于阈值者。"""
        assert self._embedder is not None
        query_vec = self._embedder.embed([q])[0]

        where: list[str] = []
        params: list[Any] = []
        if scope:
            where.append("scope = ?")
            params.append(scope)
        if user_id:
            where.append("user_id = ?")
            params.append(user_id)
        sql = "SELECT * FROM memories"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(_MAX_VECTOR_SCAN_ROWS)
        rows = self._db.query(sql, tuple(params))
        if len(rows) >= _MAX_VECTOR_SCAN_ROWS:
            # 仅告警：截断会让「最久未更新」的记忆退出检索，静默丢弃比慢更危险，
            # 故上限取 2 万（远超正常使用）并在此提示需要归档。
            logger.warning(
                "记忆表行数达到向量检索扫描上限 %d，超出部分不参与相似度排序；建议归档旧记忆",
                _MAX_VECTOR_SCAN_ROWS,
            )

        scored: list[tuple[float, MemoryRecord]] = []
        for row in rows:
            vec = self._row_vector_or_backfill(row)
            if not vec:
                continue
            sim = cosine_similarity(query_vec, vec)
            if sim >= self._min_similarity:
                scored.append((sim, MemoryRecord.from_row(row)))

        # 相似度降序（同分按更新时间 / id 兜底，稳定）
        scored.sort(key=lambda t: t[0], reverse=True)
        return [rec for _, rec in scored[: max(1, limit)]]

    def _row_vector_or_backfill(self, row: Any) -> list[float]:
        """读取行内向量；缺失（旧数据）时现算并持久化（best-effort）。

        SEC-18：向量检索无法在 SQL 侧剪枝，必须全表取出后逐条比相似度，原实现对
        每一行都做一次 ``json.loads``——数千条记忆 × 每个会话每次检索，是纯粹的
        CPU 热点。这里按「行 id + embedding 原文」缓存解码结果（LRU + 条数上限）；
        缓存值里带上原文，记录被改写后原文变化即自动失效，写入路径另外显式失效。
        """
        raw = row["embedding"]
        row_id = str(row["id"])
        cached = self._embed_cache.get(row_id)
        if cached is not None and cached[0] == raw:
            self._embed_cache.move_to_end(row_id)
            return cached[1]

        vec: list[float] = []
        if raw:
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, list) and decoded:
                    vec = [float(x) for x in decoded]
            except (json.JSONDecodeError, ValueError, TypeError):
                vec = []
        if not vec:
            # 旧行没有向量：补算并写回，使后续检索能命中。
            if self._embedder is None:
                self._embed_cache_put(row_id, raw, [])
                return []
            vector = self._embedder.embed([f"{row['key']} {row['value']} {row['tags']}".strip()])[0]
            new_raw = json.dumps(vector, ensure_ascii=False)
            try:
                self._db.execute(
                    "UPDATE memories SET embedding = ? WHERE id = ?",
                    (new_raw, row["id"]),
                )
                raw = new_raw
            except Exception:
                pass
            vec = vector
        self._embed_cache_put(row_id, raw, vec)
        return vec

    def _like_search(
        self,
        q: str,
        scope: str | None,
        limit: int,
        user_id: str = "",
    ) -> list[MemoryRecord]:
        """关键词 LIKE 检索（降级路径）。"""
        params: list[Any] = []
        where: list[str] = []
        like = f"%{_escape_like(q)}%"
        where.append("(key LIKE ? ESCAPE '\\' OR value LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')")
        params.extend([like, like, like])
        if scope:
            where.append("scope = ?")
            params.append(scope)
        if user_id:
            where.append("user_id = ?")
            params.append(user_id)
        sql = "SELECT * FROM memories WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(max(1, limit))
        return [MemoryRecord.from_row(r) for r in self._db.query(sql, tuple(params))]

    def list_memories(
        self,
        scope: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
        user_id: str = "",
    ) -> list[MemoryRecord]:
        where: list[str] = []
        params: list[Any] = []
        if scope:
            where.append("scope = ?")
            params.append(scope)
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        if user_id:
            where.append("user_id = ?")
            params.append(user_id)
        sql = "SELECT * FROM memories"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(max(1, limit))
        return [MemoryRecord.from_row(r) for r in self._db.query(sql, tuple(params))]

    def update(self, memory_id: str, user_id: str = "", **fields: Any) -> MemoryRecord | None:
        record = self.get(memory_id, user_id=user_id)
        if record is None:
            return None
        sets: list[str] = []
        params: list[Any] = []
        new_key = record.key
        new_value = record.value
        new_tags = record.tags
        if "key" in fields and fields["key"]:
            new_key = str(fields["key"]).strip()
            sets.append("key = ?")
            params.append(new_key)
        if "value" in fields and fields["value"] is not None:
            new_value = str(fields["value"])
            sets.append("value = ?")
            params.append(new_value)
        if "tags" in fields:
            new_tags = _parse_tags(fields["tags"])
            sets.append("tags = ?")
            params.append(",".join(new_tags))
        if "scope" in fields and fields["scope"] in _VALID_SCOPES:
            sets.append("scope = ?")
            params.append(fields["scope"])
        if not sets:
            return record
        # 关键字段可能变化，重算嵌入。
        sets.append("embedding = ?")
        params.append(self._embed_text(new_key, new_value, ",".join(new_tags)))
        sets.append("updated_at = ?")
        params.append(_now_iso())
        params.append(memory_id)
        self._db.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", tuple(params))
        self._embed_cache_invalidate(memory_id)
        return self.get(memory_id)

    def delete(self, memory_id: str, user_id: str = "") -> bool:
        if self.get(memory_id, user_id=user_id) is None:
            return False
        self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self._embed_cache_invalidate(memory_id)
        return True

    def render_hint(self, user_id: str = "") -> str:
        memories = self.list_memories(scope=SCOPE_GLOBAL, limit=_HINT_DEFAULT_COUNT, user_id=user_id)
        if not memories:
            return ""
        lines = ["[长期记忆] 以下是跨会话保存的长期事实（用户偏好 / 项目约定），相关时请参考："]
        for m in memories:
            value = m.value
            if len(value) > _HINT_VALUE_LIMIT:
                value = value[:_HINT_VALUE_LIMIT] + "…"
            lines.append(f"- {m.key}: {value}")
        lines.append("（可用 memory_search 检索更多，或用 memory_save 追加新记忆。）")
        return "\n".join(lines)
