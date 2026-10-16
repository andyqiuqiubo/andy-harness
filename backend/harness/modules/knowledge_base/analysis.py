"""提问分析 —— 技能（Skill）检索前的轻量查询理解（方案 5.5.3）。

四项分析（**不得改变用户原意，仅做检索友好化**）：

1. 语言/实体识别：识别查询语言与关键实体（引号短语、含数字的编号、驼峰/全大写词）；
2. 查询改写：去除「请问/帮我/what is」等口语填充词，得到检索友好的核心问句；
3. 多跳分解：把复合问句拆成多个子查询（上限 :data:`~limits.MAX_SUBQUERIES`）；
4. 知识库路由：见 :func:`route_kbs`（依据 KB 名称/描述与查询的词重叠打分）。

分析结果随检索日志留存（service 层 logger）。
"""

from __future__ import annotations

import re
from typing import Any

from .limits import MAX_SUBQUERIES
from .tokenizer import tokenize_query

# 口语填充词（中英）：从查询中剥除，仅用于检索改写，不回写用户输入
_FILLER_RE = re.compile(
    r"(请问你?|请|帮我|我想知道|我想问|我想了解一下|麻烦(你)?|你能(不能)?|可不可以|能不能|一下"
    r"|what is|what are|please|tell me|can you|could you|how do i|explain)\s*",
    re.IGNORECASE,
)

# 实体：引号短语 / 含数字的词（订单号、版本号、编号）/ 全大写缩写 / 驼峰词
_ENTITY_RE = re.compile(
    r"[\"“']([^\"“”']{2,64})[\"“']"
    r"|(?<![A-Za-z0-9_])[A-Za-z0-9_]*(?:\d[A-Za-z0-9_]*|[A-Z]{2,})[A-Za-z0-9_]*(?![A-Za-z0-9_])"
)

# 多跳分解：句末标点 / 并列连接词
_SPLIT_RE = re.compile(r"[。？?！!；;\n]+|(?:以及|还有|并且|而且|另外|以及同时)\s*")


def analyze_query(query: str) -> dict[str, Any]:
    """轻量提问分析。

    Returns::

        {"language": "zh"|"en"|"other",
         "cleaned": 改写后的核心问句,
         "entities": [实体...],
         "subqueries": [子查询...]  # 含 cleaned，去重保序，上限 MAX_SUBQUERIES
    """
    raw = (query or "").strip()
    cleaned = _FILLER_RE.sub(" ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip() or raw

    if re.search(r"[一-鿿]", raw):
        language = "zh"
    elif re.search(r"[A-Za-z]", raw):
        language = "en"
    else:
        language = "other"

    entities: list[str] = []
    for m in _ENTITY_RE.finditer(raw):
        ent = next((g for g in m.groups() if g), "")
        if ent and ent not in entities:
            entities.append(ent)

    # 多跳分解：拆不出片段时以原句为唯一子查询；去重保序，上限 MAX_SUBQUERIES
    parts = [p.strip() for p in _SPLIT_RE.split(cleaned) if p and p.strip()] or [cleaned]
    subqueries: list[str] = []
    for part in parts:
        if part not in subqueries:
            subqueries.append(part)
        if len(subqueries) >= MAX_SUBQUERIES:
            break

    return {
        "language": language,
        "cleaned": cleaned,
        "entities": entities,
        "subqueries": subqueries,
    }


def route_kbs(query: str, kbs: list[dict[str, object]]) -> list[str]:
    """知识库路由：按 KB 名称/描述与查询的词重叠打分（方案 5.5.3 第 4 步）。

    - 无候选库 → 空；
    - 有命中（重叠分 > 0）→ 返回命中 KB 的 id 列表；
    - 无任何命中 → 返回**全部** KB id（路由失败时回退全库，不丢召回）。
    """
    if not kbs:
        return []
    q_tokens = set(tokenize_query(query))
    if not q_tokens:
        return [str(kb["id"]) for kb in kbs]
    scored: list[tuple[int, str]] = []
    for kb in kbs:
        kb_tokens = set(tokenize_query(f"{kb.get('name', '')} {kb.get('description', '')}"))
        overlap = len(q_tokens & kb_tokens)
        if overlap:
            scored.append((overlap, str(kb["id"])))
    if not scored:
        return [str(kb["id"]) for kb in kbs]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [kb_id for _score, kb_id in scored]
