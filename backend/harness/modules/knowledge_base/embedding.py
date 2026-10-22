"""Embedding / Rerank 客户端 —— OpenAI 兼容端点 + 保存前连通性探针。

方案 5.3：自定义嵌入模型（provider / endpoint / key / 维度 / batch / normalize），
可选重排。模型配置保存在 kb_embedding_models，密钥经 :mod:`crypto` 加密。

- :meth:`EmbeddingClient.embed` 批量取向量（OpenAI ``/embeddings`` 兼容）。
- :meth:`EmbeddingClient.probe` 保存前连通性 + 维度校验：用一句话探测，
  返回实际维度；与声明的 dimensions 不一致时抛错（防止存错维度污染索引）。
- :meth:`RerankClient.rerank` 可选重排（OpenAI 兼容 ``/rerank`` 端点，不可用时降级）。

向量以 float32 字节序列存取（numpy），检索时用 cosine 相似度。
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from harness.infra.crypto import APIKeyEncryptor
from harness.infra.net_guard import assert_url_safe

logger = logging.getLogger("harness.kb.embedding")


class EmbeddingError(Exception):
    """向量 / 重排调用失败。"""


def encrypt_key(plaintext: str) -> str:
    """加密 API Key；空串原样返回。"""
    if not plaintext:
        return ""
    return APIKeyEncryptor().encrypt(plaintext)


def decrypt_key(ciphertext: str) -> str:
    """解密 API Key；失败返回空串（不阻断，交由调用方决定降级）。"""
    if not ciphertext:
        return ""
    try:
        return APIKeyEncryptor().decrypt(ciphertext)
    except Exception as e:  # noqa: BLE001
        logger.warning("嵌入模型密钥解密失败: %s", e)
        return ""


def mask_key(ciphertext: str) -> str:
    """返回脱敏后的密钥展示（有则 ****）。"""
    return "****" if ciphertext else ""


def vec_to_bytes(vec: list[float]) -> bytes:
    """float 向量 → float32 字节序列。"""
    return np.asarray(vec, dtype=np.float32).tobytes()


def bytes_to_vec(data: bytes) -> np.ndarray:
    """float32 字节序列 → numpy 向量。"""
    return np.frombuffer(data, dtype=np.float32)


def normalize(vec: np.ndarray) -> np.ndarray:
    """L2 归一；零向量原样返回（避免除零）。"""
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


class EmbeddingClient:
    """OpenAI 兼容嵌入端点客户端。"""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model_name: str,
        dimensions: int = 0,
        batch_size: int = 32,
        do_normalize: bool = True,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self.dimensions = dimensions
        self.batch_size = max(1, batch_size)
        self.do_normalize = do_normalize

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        url = f"{self.endpoint}{path}"
        # 体检 P1-4：端点 URL 来自知识库模型配置（管理员可写），旧实现直接请求，
        # 可被指向内网/回环/云元数据地址（SSRF）。接入统一 net_guard 护栏。
        try:
            assert_url_safe(url)
        except ValueError as e:
            raise EmbeddingError(f"嵌入端点地址不安全: {e}") from e
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
        if resp.status_code != 200:
            raise EmbeddingError(f"嵌入端点返回 {resp.status_code}: {resp.text[:200]}")
        try:
            body: dict[str, Any] = resp.json()
            return body
        except Exception as e:  # noqa: BLE001
            raise EmbeddingError(f"嵌入端点返回非 JSON: {e}") from e

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入。分批调用以尊重 batch_size，返回与输入等长的向量列表。"""
        if not texts:
            return []
        results: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            data = await self._post("/embeddings", {"model": self.model_name, "input": batch})
            items = data.get("data", [])
            # 按 index 排序，防止并发返回乱序
            items = sorted(items, key=lambda d: d.get("index", 0))
            for item in items:
                emb = item.get("embedding")
                if not emb:
                    raise EmbeddingError("嵌入端点返回缺少 embedding")
                vec = np.asarray(emb, dtype=np.float32)
                if self.do_normalize:
                    vec = normalize(vec)
                results.append(vec.tolist())
        return results

    async def probe(self) -> int:
        """连通性 + 维度校验：用一句话探测，返回实际维度。

        与声明的 dimensions 不一致时抛 EmbeddingError（保存前拦截）。
        """
        vecs = await self.embed(["ping"])
        actual = len(vecs[0]) if vecs else 0
        if self.dimensions and actual and actual != self.dimensions:
            raise EmbeddingError(f"向量维度不一致：声明 {self.dimensions}，端点实际 {actual}。请修正维度或更换模型。")
        return actual


class RerankClient:
    """可选重排客户端（OpenAI 兼容 /rerank 端点）。不可用时由上层降级。"""

    def __init__(self, endpoint: str, api_key: str, model_name: str, top_n: int = 10) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self.top_n = top_n

    async def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        """返回 [(原下标, 分数), ...]，按分数降序。失败抛 EmbeddingError 由上层降级。"""
        import httpx

        if not documents:
            return []
        url = f"{self.endpoint}/rerank"
        # 体检 P1-4：同 EmbeddingClient，重排端点也必须过 net_guard。
        try:
            assert_url_safe(url)
        except ValueError as e:
            raise EmbeddingError(f"重排端点地址不安全: {e}") from e
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                json={
                    "model": self.model_name,
                    "query": query,
                    "documents": documents,
                    "top_n": min(self.top_n, len(documents)),
                },
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            raise EmbeddingError(f"重排端点返回 {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        out: list[tuple[int, float]] = []
        for item in data.get("results", []):
            idx = item.get("index")
            score = item.get("relevance_score", item.get("score", 0.0))
            if idx is not None:
                out.append((int(idx), float(score)))
        out.sort(key=lambda x: x[1], reverse=True)
        return out
