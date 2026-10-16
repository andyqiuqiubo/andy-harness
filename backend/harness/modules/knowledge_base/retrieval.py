"""检索 —— keyword(BM25) / semantic(cosine) / hybrid(RRF) + 可选重排 + 阈值过滤。

方案 5.4：
- ``keyword`` 走 SQLite FTS5 + bm25 排序（中文经内置 bigram 分词）；
- ``semantic`` 走分段向量的 cosine 相似度（numpy）；
- ``hybrid`` 用 RRF（k=60）融合两路排名，再可选重排、阈值过滤。

健壮性要求：**缺索引 / 空库时不报错**——
- 全文索引始终可用（写入即建）；
- 无向量时 semantic / hybrid 自动降级为 keyword 并标记 ``degraded``；
- 查询为空或库为空时返回空列表。
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from .limits import DEFAULT_SCORE_THRESHOLD, DEFAULT_TOP_K, RRF_K
from .tokenizer import tokenize_query

logger = logging.getLogger("harness.kb.retrieval")


def _escape_fts_terms(query: str) -> str:
    """把查询分词为 FTS5 MATCH 的 OR 表达式。"""
    terms = tokenize_query(query)
    if not terms:
        return ""
    quoted = [f'"{t}"' for t in terms]
    return " OR ".join(quoted)


def _kb_filter(kb_ids: list[str], column: str = "kb_id") -> tuple[str, tuple[str, ...]]:
    """构造 ``<column> IN (...)`` 片段；空列表 → 全部知识库（不加过滤）。"""
    if not kb_ids:
        return "1=1", ()
    placeholders = ", ".join("?" for _ in kb_ids)
    return f"{column} IN ({placeholders})", tuple(kb_ids)


def _decorate(row: dict[str, Any]) -> dict[str, Any]:
    """把 metadata_json 解析为 metadata（供来源标注 / 前端展示直接使用）。"""
    import json

    try:
        row["metadata"] = json.loads(row.pop("metadata_json", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        row["metadata"] = {}
    return row


def search_keyword(db: Any, kb_ids: list[str], query: str, top_k: int) -> list[dict[str, Any]]:
    """FTS5 BM25 全文检索（可跨多个知识库）。空查询 / 无命中返回 []（不报错）。"""
    match = _escape_fts_terms(query)
    if not match:
        return []
    where, params = _kb_filter(kb_ids, column="f.kb_id")
    try:
        rows = db.query(
            "SELECT s.id AS id, s.doc_id AS doc_id, s.kb_id AS kb_id, s.content AS content, "
            "s.metadata_json AS metadata_json, s.parent_id AS parent_id, s.position AS position, "
            "bm25(kb_segments_fts) AS score "
            "FROM kb_segments_fts f JOIN kb_segments s ON s.id = f.segment_id "
            f"WHERE kb_segments_fts MATCH ? AND {where} "
            "ORDER BY score LIMIT ?",
            (match, *params, top_k),
        )
    except Exception as e:  # noqa: BLE001  FTS 语法边界：降级为空而非 500
        logger.warning("全文检索失败（降级为空）: %s", e)
        return []
    out = []
    for r in rows:
        d = _decorate(dict(r))
        # bm25 值越小越相关（负数），取负转成「越大越好」
        d["score"] = -float(d.get("score") or 0.0)
        out.append(d)
    return out


def search_semantic(
    db: Any,
    kb_ids: list[str],
    query_vec: np.ndarray,
    top_k: int,
) -> list[dict[str, Any]]:
    """向量 cosine 检索（可跨多个知识库）。

    仅对有 embedding 的分段计算；无向量分段自动跳过；
    换模型后残留的**异维度**向量也跳过（旧向量在重建前仍可共存，
    但不能与新查询向量做点积，见方案 5.3「重建完成前旧索引仍可用」）。
    """
    if query_vec is None:
        return []
    where, params = _kb_filter(kb_ids)
    rows = db.query(
        "SELECT id, doc_id, kb_id, content, metadata_json, parent_id, position, embedding "
        f"FROM kb_segments WHERE embedding IS NOT NULL AND {where}",
        params,
    )
    scored: list[tuple[float, dict[str, Any]]] = []
    q = query_vec.astype(np.float32)
    for r in rows:
        d = _decorate(dict(r))
        emb = d.pop("embedding", None)
        if not emb:
            continue
        vec = np.frombuffer(emb, dtype=np.float32)
        if len(vec) != len(q):
            continue  # 维度不一致（换模型未重建），跳过而非报错
        denom = float(np.linalg.norm(q) * np.linalg.norm(vec))
        if denom == 0:
            continue
        sim = float(np.dot(q, vec) / denom)
        d["score"] = sim
        scored.append((sim, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _sim, d in scored[:top_k]]


def rrf_fuse(
    *rankings: list[dict[str, Any]],
    k: int = RRF_K,
    top_k: int = DEFAULT_TOP_K,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion 融合多路排名。得分累加 1/(k+rank)。"""
    fused: dict[str, float] = {}
    items: dict[str, dict[str, Any]] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            seg_id = item["id"]
            fused[seg_id] = fused.get(seg_id, 0.0) + 1.0 / (k + rank + 1)
            items.setdefault(seg_id, item)
    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
    out = []
    for seg_id, score in ordered[:top_k]:
        d = dict(items[seg_id])
        d["score"] = score
        out.append(d)
    return out


async def retrieve(
    db: Any,
    kb_ids: list[str],
    query: str,
    mode: str = "keyword",
    top_k: int = DEFAULT_TOP_K,
    embedding_client: Any = None,
    rerank_client: Any = None,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
) -> dict[str, Any]:
    """统一检索入口（可跨多个知识库；``kb_ids`` 为空表示全部）。

    mode: keyword | semantic | hybrid。返回::

        {results, mode, degraded, note, rerank_applied, warn, reason}

    降级规则（方案 5.4.4「缺索引优雅降级 / 空库不报错」）：
    - semantic / hybrid 无向量索引或嵌入调用失败 → 退回 keyword，
      ``degraded=True``、``warn="missing_vector_index"``；
    - 重排不可用 → 保留融合排序，``rerank_applied=False``；
    - 阈值过滤后为空 → ``reason="below_threshold"``。
    """
    top_k = max(1, top_k)
    degraded = False
    note = ""
    warn = ""
    results: list[dict[str, Any]] = []

    if mode == "keyword":
        results = search_keyword(db, kb_ids, query, top_k)
    elif mode == "semantic":
        if embedding_client is None:
            degraded = True
            warn = "missing_vector_index"
            note = "未配置嵌入模型，已降级为关键词检索"
            results = search_keyword(db, kb_ids, query, top_k)
        else:
            try:
                vecs = await embedding_client.embed([query])
                q = np.asarray(vecs[0], dtype=np.float32)
                results = search_semantic(db, kb_ids, q, top_k)
                if not results:
                    degraded = True
                    warn = "missing_vector_index"
                    note = "暂无向量索引，已降级为关键词检索"
                    results = search_keyword(db, kb_ids, query, top_k)
            except Exception as e:  # noqa: BLE001
                logger.warning("向量检索失败，降级关键词: %s", e)
                degraded = True
                warn = "missing_vector_index"
                note = "向量检索不可用，已降级为关键词检索"
                results = search_keyword(db, kb_ids, query, top_k)
    else:  # hybrid
        kw = search_keyword(db, kb_ids, query, top_k)
        sem: list[dict[str, Any]] = []
        if embedding_client is not None:
            try:
                vecs = await embedding_client.embed([query])
                q = np.asarray(vecs[0], dtype=np.float32)
                sem = search_semantic(db, kb_ids, q, top_k)
            except Exception as e:  # noqa: BLE001
                logger.warning("hybrid 向量部分失败，仅用关键词: %s", e)
        if not sem:
            degraded = True
            warn = "missing_vector_index"
            note = "暂无向量索引，混合检索以关键词为主"
        results = rrf_fuse(kw, sem, top_k=top_k) if sem else kw

    # 可选重排（失败则回退融合排序并标记 rerank_applied=False，方案 5.3.3）
    rerank_applied = False
    if rerank_client is not None and results:
        try:
            docs = [r["content"] for r in results]
            reranked = await rerank_client.rerank(query, docs)
            remapped = []
            for idx, score in reranked:
                if 0 <= idx < len(results):
                    item = dict(results[idx])
                    item["score"] = score
                    remapped.append(item)
            if remapped:
                results = remapped
                rerank_applied = True
        except Exception as e:  # noqa: BLE001
            logger.warning("重排失败，回退融合排序: %s", e)

    # 阈值过滤（RRF 融合分较小，默认阈值 0 不过滤）
    reason = ""
    if score_threshold and score_threshold > 0:
        before = len(results)
        results = [r for r in results if float(r.get("score") or 0.0) >= score_threshold]
        if before and not results:
            reason = "below_threshold"

    return {
        "results": results,
        "mode": mode,
        "degraded": degraded,
        "note": note,
        "rerank_applied": rerank_applied,
        "warn": warn,
        "reason": reason,
    }
