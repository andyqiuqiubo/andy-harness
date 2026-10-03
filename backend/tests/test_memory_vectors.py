"""E7 · 向量记忆检索测试（离线确定性，不发网络请求）。"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import pytest

from harness.infra.database import Database
from harness.modules.memory_manager.embedding import (
    Embedder,
    HashingEmbedder,
    OpenAICompatibleEmbedder,
    cosine_similarity,
    tokenize,
)
from harness.modules.memory_manager.service import MemoryServiceImpl


@pytest.fixture
def db() -> Any:
    """内存数据库。"""
    d = Database(":memory:")
    yield d
    d.close()


# 固定按关键词给向量的可控嵌入器，便于验证语义排序。
class KeywordEmbedder(Embedder):
    """dim=4，按关键词命中各坐标轴。"""

    def __init__(self, dim: int = 4) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            v = [0.0] * self._dim
            if "连接" in t:
                v[0] = 1.0
            if "天气" in t or "雨" in t:
                v[1] = 1.0
            if "配置" in t:
                v[2] = 1.0
            norm = math.sqrt(sum(x * x for x in v))
            if norm > 0:
                v = [x / norm for x in v]
            out.append(v)
        return out


class TestHashingEmbedder:
    """本地哈希嵌入器。"""

    def test_tokenize(self) -> None:
        assert tokenize("Vue3 FastAPI") == ["vue3", "fastapi"]
        assert tokenize("数据库") == ["数", "据", "库"]
        assert tokenize("a_b") == ["a", "_", "b"]

    def test_dim(self) -> None:
        assert HashingEmbedder(128).dim == 128
        assert HashingEmbedder().dim == 256

    def test_vectors_normalized(self) -> None:
        e = HashingEmbedder()
        v = e.embed(["hello world"])[0]
        norm = math.sqrt(sum(x * x for x in v))
        assert abs(norm - 1.0) < 1e-6

    def test_empty_text_zero_vector(self) -> None:
        v = HashingEmbedder().embed([""])[0]
        assert all(x == 0.0 for x in v)

    def test_deterministic(self) -> None:
        e = HashingEmbedder()
        assert e.embed(["稳定输入"])[0] == e.embed(["稳定输入"])[0]

    def test_similarity_ordering(self) -> None:
        e = HashingEmbedder()
        base = e.embed(["数据库连接配置"])[0]
        close = e.embed(["数据库连接"])[0]
        far = e.embed(["今天天气晴朗"])[0]
        assert cosine_similarity(base, base) == pytest.approx(1.0, abs=1e-6)
        assert cosine_similarity(base, close) > 0
        assert cosine_similarity(base, far) == pytest.approx(0.0, abs=1e-6)

    def test_invalid_dim(self) -> None:
        with pytest.raises(ValueError):
            HashingEmbedder(0)


class TestCosine:
    def test_orthogonal(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_identical(self) -> None:
        assert cosine_similarity([1.0, 1.0], [1.0, 1.0]) == pytest.approx(1.0)

    def test_length_mismatch(self) -> None:
        assert cosine_similarity([1.0], [1.0, 0.0]) == 0.0
        assert cosine_similarity([], []) == 0.0


class TestOpenAICompatibleEmbedder:
    def test_construction(self) -> None:
        e = OpenAICompatibleEmbedder("sk-test", base_url="https://x/v1", model="m")
        assert e.model == "m"
        assert e.dim == 0  # 未调用前维度未知


class TestVectorMemory:
    """记忆服务的向量检索。"""

    def test_save_stores_embedding(self, db: Database) -> None:
        svc = MemoryServiceImpl(db, embedder=KeywordEmbedder())
        svc.save("a", "连接配置")
        row = db.query_one("SELECT embedding FROM memories WHERE key = 'a'")
        assert row is not None and row["embedding"]

    def test_semantic_search_ranks_correct(self, db: Database) -> None:
        svc = MemoryServiceImpl(db, embedder=KeywordEmbedder())
        # 两条记忆：连接池 vs 天气；措辞与查询不连续重合。
        svc.save("db", "后端服务维护连接池")
        svc.save("weather", "今天预报有雨")

        hits = svc.search("连接参数怎么配")
        # 语义（连接轴）命中连接池记忆；LIKE 对整句无连续子串。
        assert hits
        assert hits[0].key == "db"
        assert "weather" not in [h.key for h in hits]

    def test_search_empty_query_lists_recent(self, db: Database) -> None:
        svc = MemoryServiceImpl(db, embedder=KeywordEmbedder())
        svc.save("a", "连接")
        svc.save("b", "雨")
        assert len(svc.search("")) == 2

    def test_no_embedder_falls_back_to_like(self, db: Database) -> None:
        svc = MemoryServiceImpl(db, embedder=None)
        svc.save("k", "明确内容")
        hits = svc.search("明确内容")
        assert len(hits) == 1 and hits[0].key == "k"

    def test_update_reembeds(self, db: Database) -> None:
        svc = MemoryServiceImpl(db, embedder=KeywordEmbedder())
        rec = svc.save("a", "连接")
        before = db.query_one("SELECT embedding FROM memories WHERE id = ?", (rec.id,))["embedding"]
        svc.update(rec.id, value="今天有雨")
        after = db.query_one("SELECT embedding FROM memories WHERE id = ?", (rec.id,))["embedding"]
        assert before != after

    def test_legacy_null_embedding_backfilled(self, db: Database) -> None:
        svc = MemoryServiceImpl(db, embedder=KeywordEmbedder())
        # 直接插入一条无 embedding 的旧行
        db.execute(
            "INSERT INTO memories (id, key, value, scope, session_id, tags, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("old1", "old", "连接旧数据", "global", "", "", "t", "t"),
        )
        hits = svc.search("连接")
        assert hits and hits[0].key == "old"
        # 已回填
        row = db.query_one("SELECT embedding FROM memories WHERE id = 'old1'")
        assert row["embedding"]
