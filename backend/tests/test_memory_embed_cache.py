"""SEC-18：memory_manager 向量检索性能 —— embedding 解码缓存专项测试。

问题：向量检索无法在 SQL 侧剪枝，必须全表取出后逐条比相似度，原实现对每一行都
做一次 ``json.loads``——数千条记忆 × 每个会话每次检索，是纯粹的 CPU 热点。

本文件验证三件事：
1. 解码结果被缓存（重复检索不再重复解码）；
2. 写入路径（save/update/delete）正确失效缓存，检索不会读到陈旧向量；
3. 缓存有 LRU 条数上限，不会随记忆表无限膨胀。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pytest

from harness.infra.database import Database
from harness.modules.memory_manager import service as svc
from harness.modules.memory_manager.embedding import Embedder
from harness.modules.memory_manager.service import MemoryServiceImpl


class KeywordEmbedder(Embedder):
    """dim=4，按关键词命中各坐标轴（可控，便于验证「内容变了向量就变」）。"""

    def __init__(self, dim: int = 4) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            v = [0.0] * self._dim
            if "天气" in t:
                v[0] = 1.0
            if "配置" in t:
                v[1] = 1.0
            if "部署" in t:
                v[2] = 1.0
            norm = math.sqrt(sum(x * x for x in v))
            if norm > 0:
                v = [x / norm for x in v]
            out.append(v)
        return out


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def mem(db):
    return MemoryServiceImpl(db, embedder=KeywordEmbedder())


def _count_decodes(mem: MemoryServiceImpl, monkeypatch) -> dict[str, int]:
    """包一层 json.loads 统计真实解码次数（缓存命中就不该走到这里）。"""
    real_loads = svc.json.loads
    counter = {"n": 0}

    def counting_loads(*a, **kw):
        counter["n"] += 1
        return real_loads(*a, **kw)

    monkeypatch.setattr(svc.json, "loads", counting_loads)
    return counter


def test_embedding_decode_is_cached_across_searches(mem, monkeypatch):
    """第二次检索应命中缓存：解码次数不再随记忆条数增长。"""
    for i in range(5):
        mem.save(f"key{i}", f"关于配置的第{i}条", user_id="u")

    counter = _count_decodes(mem, monkeypatch)
    mem.search("配置", user_id="u")
    first = counter["n"]
    assert first >= 5, f"首次检索应逐条解码（实际 {first}）"

    mem.search("配置", user_id="u")
    assert counter["n"] == first, f"第二次检索仍解码了 {counter['n'] - first} 次，缓存未生效"


def test_cache_invalidated_on_update(mem, db):
    """记忆被改写后，检索必须反映新内容（读到旧向量即为脏缓存）。"""
    rec = mem.save("偏好", "我喜欢晴天", user_id="u")
    assert mem.search("晴天", user_id="u")

    mem.update(rec.id, value="我关注天气与部署", user_id="u")
    # 新向量含「天气/部署」，旧向量不含；若缓存未失效，下面会查不到
    hits = mem.search("部署", user_id="u")
    assert hits and hits[0].id == rec.id, "update 后仍读到旧向量（缓存未失效）"


def test_cache_invalidated_on_save_upsert(mem):
    """同 key 覆盖保存（upsert）同样要让旧向量失效。"""
    rec = mem.save("主题", "聊天气", user_id="u")
    mem.save("主题", "聊部署", user_id="u")  # 同 (scope, session, key) → upsert
    hits = mem.search("部署", user_id="u")
    assert hits and hits[0].id == rec.id, "upsert 后仍读到旧向量（缓存未失效）"


def test_cache_invalidated_on_delete(mem):
    mem.save("临时", "关于部署的临时记录", user_id="u")
    assert mem.search("部署", user_id="u")
    rec = mem.list_memories(user_id="u")[0]
    mem.delete(rec.id, user_id="u")
    assert str(rec.id) not in mem._embed_cache


def test_cache_is_bounded_by_lru(mem, monkeypatch):
    """缓存条数不得超过上限，避免随记忆表无限膨胀。"""
    monkeypatch.setattr(svc, "_EMBED_CACHE_MAX", 8)
    for i in range(30):
        mem.save(f"k{i}", f"内容{i}", user_id="u")
    mem.search("内容", user_id="u")
    assert len(mem._embed_cache) <= 8, f"缓存未受上限约束：{len(mem._embed_cache)}"


def test_vector_search_has_scan_cap(mem, monkeypatch):
    """向量检索必须带扫描上限：SQL 里出现 LIMIT，避免表膨胀后拖死主线程。"""
    captured: dict[str, str] = {}
    real_query = mem._db.query

    def spy(sql: str, params=()):
        if "FROM memories" in sql:
            captured["sql"] = sql
        return real_query(sql, params)

    monkeypatch.setattr(mem._db, "query", spy)
    mem.save("k", "v", user_id="u")
    mem.search("v", user_id="u")
    assert "LIMIT" in captured["sql"], f"向量检索未加扫描上限: {captured['sql']}"


def test_backfilled_vector_is_cached(mem, db, monkeypatch):
    """旧行（无 embedding）补算后写回并缓存，不重复计算。"""
    db.execute(
        "INSERT INTO memories (id, key, value, scope, session_id, tags, embedding, "
        "created_at, updated_at, user_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("m_old", "旧记", "关于天气", "global", "", "", None, "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:00+00:00", "u"),
    )
    embedder = mem._embedder
    backfills = {"n": 0}
    real_embed = embedder.embed

    def counting_embed(texts):
        # 只统计「补算记忆向量」；查询文本本身的嵌入每次都会算，不参与断言
        if any("旧记" in t for t in texts):
            backfills["n"] += 1
        return real_embed(texts)

    monkeypatch.setattr(embedder, "embed", counting_embed)

    mem.search("天气", user_id="u")
    assert backfills["n"] == 1, "首次检索应为无向量的旧行补算一次"
    mem.search("天气", user_id="u")
    assert backfills["n"] == 1, "补算结果未缓存，第二次检索又算了一遍"
