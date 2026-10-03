"""嵌入器 —— 为长期记忆提供向量。

提供两种实现：
- `HashingEmbedder`：零依赖、确定性的特征哈希嵌入（本地默认），支持中英混合分词，
  语义上反映「词项重叠」，比 LIKE 连续子串更鲁棒，且可完全离线复现；
- `OpenAICompatibleEmbedder`：调用 OpenAI 兼容的 `/embeddings` 端点，
  提供**真正的语义（同义词 / 改写）检索**，按配置注入。

设计：
- 嵌入器只负责「文本 → 向量」，不感知记忆；
- 余弦相似度是纯函数，便于单测；
- 无法访问远端时可降级到本地哈希嵌入器，不影响主流程。
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod
from typing import Any

# 匹配英文单词/数字，或单个 CJK 统一表意文字（中文按字成词），或单个下划线
# （下划线常见于标识符，单查 "_" 也应能命中 "a_b" 这类 key）。
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]|_")


def tokenize(text: str) -> list[str]:
    """中英混合的简单分词：拉丁词整体、CJK 单字。"""
    return _TOKEN_RE.findall(text.lower())


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度（假设输入等长；返回 [-1, 1]，实际多为 [0,1]）。"""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class Embedder(ABC):
    """嵌入器抽象。"""

    @property
    @abstractmethod
    def dim(self) -> int:
        """向量维度。"""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """把一批文本转为向量。"""


class HashingEmbedder(Embedder):
    """特征哈希（hashing trick）嵌入：确定性、零依赖。

    每个词项经哈希映射到固定维度的桶（带正负号），累加后返回。
    相同词项集合 → 高余弦相似度；无外部模型，离线可复现。
    """

    def __init__(self, dim: int = 256) -> None:
        if dim <= 0:
            raise ValueError("dim 必须为正")
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def _hashes(self, token: str) -> tuple[int, float]:
        h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(h[:4], "big") % self._dim
        # 用另一组字节决定符号，减少碰撞相互抵消的偏差。
        sign = 1.0 if h[4] & 1 else -1.0
        return bucket, sign

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in tokenize(text):
            bucket, sign = self._hashes(token)
            vec[bucket] += sign
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


class OpenAICompatibleEmbedder(Embedder):
    """调用 OpenAI 兼容 `/embeddings` 端点的嵌入器（同步 HTTP）。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "text-embedding-3-small",
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._dim = 0

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        url = f"{self._base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        payload: dict[str, Any] = {"model": self._model, "input": texts}
        resp = httpx.post(url, headers=headers, json=payload, timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()
        items = sorted(data["data"], key=lambda d: d["index"])
        vectors = [list(map(float, d["embedding"])) for d in items]
        if vectors:
            self._dim = len(vectors[0])
        return vectors


def build_embedder_from_env() -> Embedder | None:
    """根据环境变量构造嵌入器。

    - 设置 ``HARNESS_EMBEDDING_MODEL`` 且有 API key（``HARNESS_EMBEDDING_API_KEY``
      或 ``OPENAI_API_KEY``）时，构造 OpenAI 兼容嵌入器，获得真正的语义检索。
    - 设置 ``HARNESS_EMBEDDING=off`` 时显式禁用（返回 None，仅用 LIKE）。
    - 其余情况返回本地零依赖 ``HashingEmbedder``。
    """
    import os

    if os.environ.get("HARNESS_EMBEDDING", "").lower() in {"off", "false", "0"}:
        return None

    model = os.environ.get("HARNESS_EMBEDDING_MODEL", "").strip()
    api_key = (os.environ.get("HARNESS_EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if model and api_key:
        base_url = os.environ.get("HARNESS_EMBEDDING_BASE_URL", "https://api.openai.com/v1").strip()
        return OpenAICompatibleEmbedder(api_key, base_url=base_url, model=model)

    return HashingEmbedder()
