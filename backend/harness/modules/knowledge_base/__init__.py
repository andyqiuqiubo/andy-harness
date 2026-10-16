"""知识库（Knowledge Base）模块 —— 分阶段文档存储与分段清洗。

本包把方案文档「知识库系统」拆成可独立演进的层：

- :mod:`limits`      常量与目录约定（分段上限、清洗规则、默认分段策略）
- :mod:`tokenizer`   中文检索分词（无 jieba 环境下的内置 bigram 分词器）
- :mod:`chunking`    分段策略 S1/S2/S3/S4 与 7 条公共清洗规则
- :mod:`parsers`     P1 文本/图文抽取（txt/md/csv/docx/pdf）与资产描述
- :mod:`repository`  知识库 / 文档 / 分段 / 嵌入模型的 SQLite 存取
- :mod:`embedding`   自定义 Embedding / Rerank 客户端（OpenAI 兼容）
- :mod:`retrieval`   keyword(BM25) / semantic(cosine) / hybrid(RRF) 检索
- :mod:`service`     编排：上传→抽取→分段→索引、检索、重建索引

设计原则：**索引缺失时优雅降级**（返回空结果而非报错），
全文索引始终可用，向量索引仅在配置并写入 embedding 后生效。
"""

from __future__ import annotations

from .limits import (
    CLEANING_RULES,
    DEFAULT_EMBEDDING_BATCH,
    DEFAULT_RERANK_TOP_N,
    EMBEDDING_DIMS_FALLBACK,
    MAX_SEGMENT_CHARS,
    MIN_SEGMENT_CHARS,
    SEGMENT_OVERLAP_CHARS,
    SEGMENT_STRATEGIES,
    knowledge_assets_dir,
    knowledge_dir,
    knowledge_originals_dir,
)

__all__ = [
    "CLEANING_RULES",
    "DEFAULT_EMBEDDING_BATCH",
    "DEFAULT_RERANK_TOP_N",
    "EMBEDDING_DIMS_FALLBACK",
    "MAX_SEGMENT_CHARS",
    "MIN_SEGMENT_CHARS",
    "SEGMENT_OVERLAP_CHARS",
    "SEGMENT_STRATEGIES",
    "knowledge_assets_dir",
    "knowledge_dir",
    "knowledge_originals_dir",
]
