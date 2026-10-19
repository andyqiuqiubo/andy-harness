"""文本匹配（失败阶梯的 name 编辑距离放宽，§7.4）。"""

from __future__ import annotations


def edit_distance(a: str, b: str) -> int:
    """Levenshtein 距离（短字符串；name 匹配放宽用，max≤2）。"""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    # 滚动数组
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def fuzzy_contains(hay: str, needle: str, max_edit: int = 0) -> bool:
    """needle 是否（容差内）出现在 hay 中。max_edit=0 时等价于子串包含。"""
    if not needle:
        return True
    hay = hay or ""
    if not max_edit:
        return needle in hay
    n = len(needle)
    for i in range(max(0, len(hay) - n - max_edit), len(hay)):
        window = hay[i:i + n + max_edit + 1]
        if window and edit_distance(window[:n], needle) <= max_edit:
            return True
    return False
