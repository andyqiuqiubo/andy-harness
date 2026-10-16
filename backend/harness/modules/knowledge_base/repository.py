"""知识库数据访问层 —— 知识库 / 文档 / 分段 / 嵌入模型的 SQLite 存取。

遵循项目既有 Repository 约定：接收 ``Database`` 实例，方法返回 ``dict`` / ``list``，
不混入业务编排（分段、清洗、检索都在上层）。FTS5 虚表的增删随分段同步维护。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC
from typing import Any

from harness.infra.database import Database

from .chunking import Segment
from .tokenizer import tokenize_text


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


# ── 知识库 ────────────────────────────────────────────────────────────


class KnowledgeBaseRepository:
    """knowledge_bases 表。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self,
        name: str,
        description: str = "",
        embedding_model_id: str | None = None,
        default_retrieval_mode: str = "keyword",
    ) -> dict[str, Any]:
        kb_id = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO knowledge_bases (id, name, description, embedding_model_id, "
            "default_retrieval_mode, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kb_id, name, description, embedding_model_id, default_retrieval_mode, _now()),
        )
        created = self.get(kb_id)
        assert created is not None
        return created

    def get(self, kb_id: str) -> dict[str, Any] | None:
        return _row_to_dict(self._db.query_one("SELECT * FROM knowledge_bases WHERE id = ?", (kb_id,)))

    def list(self) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM knowledge_bases ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    def update(self, kb_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get(kb_id)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._db.execute(
            f"UPDATE knowledge_bases SET {cols} WHERE id = ?",
            (*fields.values(), kb_id),
        )
        return self.get(kb_id)

    def delete(self, kb_id: str) -> bool:
        cur = self._db.execute("DELETE FROM knowledge_bases WHERE id = ?", (kb_id,))
        return cur.rowcount > 0


# ── 文档 ──────────────────────────────────────────────────────────────


class KBDocumentRepository:
    """kb_documents 表。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self,
        kb_id: str,
        name: str,
        file_type: str,
        origin_path: str,
        storage_stage: str = "P1",
        status: str = "pending",
    ) -> dict[str, Any]:
        doc_id = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO kb_documents (id, kb_id, name, file_type, origin_path, storage_stage, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, kb_id, name, file_type, origin_path, storage_stage, status, _now()),
        )
        created = self.get(doc_id)
        assert created is not None
        return created

    def get(self, doc_id: str) -> dict[str, Any] | None:
        return _row_to_dict(self._db.query_one("SELECT * FROM kb_documents WHERE id = ?", (doc_id,)))

    def list_by_kb(self, kb_id: str) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM kb_documents WHERE kb_id = ? ORDER BY created_at DESC",
            (kb_id,),
        )
        return [dict(r) for r in rows]

    def update(self, doc_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get(doc_id)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._db.execute(
            f"UPDATE kb_documents SET {cols} WHERE id = ?",
            (*fields.values(), doc_id),
        )
        return self.get(doc_id)

    def delete(self, doc_id: str) -> bool:
        cur = self._db.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))
        return cur.rowcount > 0


# ── 分段 ──────────────────────────────────────────────────────────────


class KBSegmentRepository:
    """kb_segments 表 + FTS5 虚表同步。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self,
        doc_id: str,
        kb_id: str,
        segment: Segment,
        position: int,
        embedding: bytes | None = None,
        seg_id: str = "",
    ) -> str:
        # 优先用调用方给的 id（父子分段中子段 parent_id 须指向父段行主键），
        # 其次用分段自带的本地 id，最后才现生成——保证「segment.parent_id == 行 id」。
        seg_id = seg_id or segment.id or uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO kb_segments (id, doc_id, kb_id, content, clean_fulltext, position, "
            "parent_id, embedding, metadata_json, hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seg_id,
                doc_id,
                kb_id,
                segment.content,
                segment.fulltext,
                position,
                segment.parent_id,
                embedding,
                json.dumps(segment.metadata, ensure_ascii=False),
                segment.compute_hash(),
                _now(),
            ),
        )
        self._index_fts(seg_id, kb_id, doc_id, segment.fulltext)
        return seg_id

    def _index_fts(self, seg_id: str, kb_id: str, doc_id: str, fulltext: str) -> None:
        # FTS5 unicode61 不会切中文：写入前先过 bigram 分词，保证「索引态=查询态」
        self._db.execute(
            "INSERT INTO kb_segments_fts (segment_id, kb_id, doc_id, clean_fulltext) VALUES (?, ?, ?, ?)",
            (seg_id, kb_id, doc_id, tokenize_text(fulltext)),
        )

    def delete_by_doc(self, doc_id: str) -> int:
        # 先删 FTS 行（外部内容表无级联），再删正文行
        self._db.execute("DELETE FROM kb_segments_fts WHERE doc_id = ?", (doc_id,))
        cur = self._db.execute("DELETE FROM kb_segments WHERE doc_id = ?", (doc_id,))
        return cur.rowcount

    def list_by_doc(self, doc_id: str) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM kb_segments WHERE doc_id = ? ORDER BY position", (doc_id,))
        return [self._decorate(dict(r)) for r in rows]

    def list_by_kb(self, kb_id: str) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM kb_segments WHERE kb_id = ? ORDER BY position", (kb_id,))
        return [self._decorate(dict(r)) for r in rows]

    def get(self, seg_id: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM kb_segments WHERE id = ?", (seg_id,))
        return self._decorate(dict(row)) if row else None

    def update_embedding(self, seg_id: str, embedding: bytes) -> None:
        self._db.execute("UPDATE kb_segments SET embedding = ? WHERE id = ?", (embedding, seg_id))

    def _decorate(self, d: dict[str, Any]) -> dict[str, Any]:
        """把 metadata_json 解析为对象，并对 embedding 做轻量化处理。

        ``embedding`` 是 float32 原始字节：**不对外暴露**——它既无法被 JSON
        序列化（FastAPI 编码 bytes 会解码失败导致 500），体积也大；
        统一替换为 ``has_embedding`` 布尔，供上层判断向量是否已就绪。
        """
        try:
            d["metadata"] = json.loads(d.pop("metadata_json", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["metadata"] = {}
        if "embedding" in d:
            d["has_embedding"] = d.pop("embedding") is not None
        return d


# ── 嵌入 / 重排模型 ───────────────────────────────────────────────────


class KBEmbeddingModelRepository:
    """kb_embedding_models 表。密钥字段加密存储，读取时脱敏返回。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, **fields: Any) -> dict[str, Any]:
        model_id = uuid.uuid4().hex
        fields = dict(fields)
        fields["id"] = model_id
        fields.setdefault("created_at", _now())
        cols = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        self._db.execute(
            f"INSERT INTO kb_embedding_models ({cols}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        created = self.get(model_id)
        assert created is not None
        return created

    def get(self, model_id: str) -> dict[str, Any] | None:
        return _row_to_dict(self._db.query_one("SELECT * FROM kb_embedding_models WHERE id = ?", (model_id,)))

    def list(self) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM kb_embedding_models ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    def update(self, model_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get(model_id)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._db.execute(
            f"UPDATE kb_embedding_models SET {cols} WHERE id = ?",
            (*fields.values(), model_id),
        )
        return self.get(model_id)

    def delete(self, model_id: str) -> bool:
        cur = self._db.execute("DELETE FROM kb_embedding_models WHERE id = ?", (model_id,))
        return cur.rowcount > 0
