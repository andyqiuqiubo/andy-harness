"""检索分词 —— 无 jieba 环境下的内置中文 bigram 分词器。

为什么需要预分词：SQLite FTS5 的 ``unicode61`` tokenizer 只按空白/标点切词，
对中文整句会得到一个「超长 token」，既无法被 MATCH 命中，也无法算 BM25。
方案 5.4 要求中文可检索，且零新增第三方依赖。

策略（针对检索而非 NLP 精度）：
- 连续 CJK 文本切成**字符 bigram**（滑窗），两字组合即一个检索单元，
  足以让「密码」「重置」「管理员」等查询命中对应窗口；
- 连续的 ASCII 单词 / 数字整体保留为小写 token；
- 标点 / 空白 / 控制字符作为分隔，不产生 token。

分词结果以空格连接写入 FTS 的 ``clean_fulltext``，FTS5 再按空格切分即可
正确建倒排；查询侧同样过一遍本分词器，保证「索引态 = 查询态」。
"""

from __future__ import annotations

import re
from functools import lru_cache

# 单字 CJK（bigram 切分时逐个字符判断）
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
# ASCII 单词 / 数字（含连字符与点，便于版本号、e-mail 等保持完整）
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+\-]*")


def _bigrams(chars: list[str]) -> list[str]:
    """把一段 CJK 字符切成 bigram；单字则保留单字自身。"""
    if len(chars) == 1:
        return [chars[0]]
    return ["".join(chars[i : i + 2]) for i in range(len(chars) - 1)]


def _tokenize(text: str) -> list[str]:
    """一次正则扫描出 ASCII 词与 CJK 段，再对 CJK 段做 bigram。

    - CJK 连续段 → 字符 bigram（单字保留）
    - ASCII 词 → 小写整词
    - 其余（标点/空白/符号）→ 分隔，不产生 token

    重复 token 保留（供 BM25 的 TF 计算）。
    """
    tokens: list[str] = []
    cjk_run: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        ch = text[pos]
        if _CJK_CHAR_RE.match(ch):
            cjk_run.append(ch)
            pos += 1
            continue
        # 遇到非 CJK：先收尾中文段
        if cjk_run:
            tokens.extend(_bigrams(cjk_run))
            cjk_run = []
        m = _ASCII_WORD_RE.match(text, pos)
        if m:
            tokens.append(m.group(0).lower())
            pos = m.end()
            continue
        # 分隔符
        pos += 1
    if cjk_run:
        tokens.extend(_bigrams(cjk_run))
    return tokens


def tokenize_text(text: str) -> str:
    """返回以空格连接的分词结果字符串（写入 FTS 用）。"""
    return " ".join(_tokenize(text or ""))


def tokenize_query(text: str) -> list[str]:
    """查询分词：去重保序，用于构造 FTS MATCH 的 OR 查询。"""
    seen: set[str] = set()
    result: list[str] = []
    for tok in _tokenize(text or ""):
        if tok and tok not in seen:
            seen.add(tok)
            result.append(tok)
    return result


@lru_cache(maxsize=512)
def tokenize_cached(text: str) -> tuple[str, ...]:
    """带缓存的分词（查询侧高频复用）。"""
    return tuple(_tokenize(text))
