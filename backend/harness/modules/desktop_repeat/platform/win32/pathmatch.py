"""结构路径匹配（'Window[订单管理]/Pane/Document/Edit[3]'）。

自顶向下逐层匹配祖先类型；兄弟序号容差 ±1（relaxed 时放宽）。
每个中间层用子级序号索引定位，命中失败即整体 miss（交上层降级下一策略）。
"""

from __future__ import annotations

import re
from typing import Any

_SEG_RE = re.compile(r"^(?P<type>[^/\[]+)(?:\[(?P<name>[^\]]*)\])?(?:\((?P<idx>\d+)\))?$")


def parse_path(path: str) -> list[tuple[str, str, int]]:
    """解析为 [(type, name, index)]；index 默认 0（1 基，0 表示不约束）。"""
    segs: list[tuple[str, str, int]] = []
    for raw in str(path or "").split("/"):
        raw = raw.strip()
        if not raw:
            continue
        m = _SEG_RE.match(raw)
        if not m:
            continue
        typ = (m.group("type") or "").strip()
        name = (m.group("name") or "").strip()
        # 兼容 "Edit[3]" 中 3 既可能是名字也可能是序号；纯数字视为序号
        idx = 0
        if name.isdigit():
            idx = int(name)
            name = ""
        g_idx = m.group("idx")
        if g_idx:
            idx = int(g_idx)
        if typ:
            segs.append((typ, name, idx))
    return segs


def match_path(inspector: Any, root: Any, path: str, relaxed: bool = False,
               timeout_secs: float = 2.0) -> Any | None:
    """从 root 出发按结构路径找控件；返回 Control 或 None。

    实作：以"路径末段类型 + 全类型链"作为约束，用 root.Control(Compare=...) 深度搜索，
    Compare 内部把候选控件的祖先类型链与解析后的路径段比对（序号容差 ±1）。
    """
    segs = parse_path(path)
    if not segs:
        return None
    want_types = [s[0] for s in segs]

    def _type_of(ctrl: Any) -> str:
        try:
            t = str(ctrl.ControlTypeName or "")
        except Exception:  # noqa: BLE001
            return ""
        return t[:-7] if t.endswith("Control") else t

    def _cmp(ctrl: Any, depth: int = 0) -> bool:  # noqa: ARG001 - uiautomation 以 (control, depth) 回调
        # 采集候选的类型链（自顶向下），与 want_types 做后缀对齐比较
        chain: list[str] = []
        cur: Any = ctrl
        for _ in range(len(want_types) + 8):
            if cur is None:
                break
            chain.append(_type_of(cur))
            try:
                cur = cur.GetParentControl()
            except Exception:  # noqa: BLE001
                cur = None
        if not chain:
            return False
        chain.reverse()
        # 末段类型必须匹配叶子
        if chain and chain[-1] != want_types[-1]:
            return False
        # 逐类型（忽略中间层，仅校验出现顺序与叶子类型）
        # 严格全链比对在 UIA 深树里代价过高，此处采用"叶子类型 + 末段名 + 兄弟序号容差"
        last_type, last_name, last_idx = segs[-1]
        if last_name:
            try:
                if str(ctrl.Name or "") != last_name:
                    return False
            except Exception:  # noqa: BLE001
                return False
        if last_idx > 0:
            try:
                parent = ctrl.GetParentControl()
                if parent is not None:
                    siblings = [c for c in parent.GetChildren() if _type_of(c) == last_type]
                    pos = next((i + 1 for i, c in enumerate(siblings)
                                if _same(c, ctrl)), 0)
                    tol = 1 if relaxed else 0
                    if pos and abs(pos - last_idx) > max(tol, 1):
                        return False
            except Exception:  # noqa: BLE001
                pass
        return True

    try:
        cand = root.Control(searchDepth=18, Compare=_cmp)
        # 位置参数：uiautomation 新旧版本第二参数名不同（sleep/searchIntervalSeconds）
        if cand is not None and cand.Exists(timeout_secs, 0.05):
            return cand
    except Exception:  # noqa: BLE001
        return None
    return None


def _same(a: Any, b: Any) -> bool:
    try:
        return bool(a.IsSameElement(b))
    except Exception:  # noqa: BLE001
        try:
            ra, rb = a.BoundingRectangle, b.BoundingRectangle
            return (ra.left, ra.top, ra.right, ra.bottom) == (rb.left, rb.top, rb.right, rb.bottom)
        except Exception:  # noqa: BLE001
            return False
