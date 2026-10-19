"""定位链装配与打分（S4，§6.1 打分表）。

输入：事件携带的 uia 快照 / pos / anchor 候选；输出有序 LocatorSpec 链。
得分 ≥0.35 入链；relative/coord 恒追加链尾（降级项）；uia=null → 仅 image/relative/coord
且整步 confidence ≤0.5、needs_review。
"""

from __future__ import annotations

import re
from typing import Any

from ..models import LocatorSpec, RecEvent, UiaSnapshot

_GUIDE_RE = re.compile(r"^[0-9a-fA-F\-]{16,}$")
_DIGIT_ONLY = re.compile(r"^\d+$")
_GENERIC_NAMES = {"", "ok", "确定", "取消", "关闭", "button", "text", "pane", "list item",
                  "menu item", "选项", "按钮"}


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def build_chain(ev: RecEvent, anchor_rel: str | None,
                win_scale: float = 1.0) -> tuple[list[LocatorSpec], float]:
    """返回 (链, 整步置信度)。链按得分降序，relative/coord 恒在尾部。"""
    scored: list[LocatorSpec] = []
    snap = ev.uia
    pos = ev.pos
    screen = (ev.data.get("_screen") or [1920, 1080]) if isinstance(ev.data.get("_screen"), list) else [1920, 1080]

    if snap is not None:
        # 1. automation_id
        aid = snap.automation_id or ""
        if aid and not _DIGIT_ONLY.match(aid) and not _GUIDE_RE.match(aid):
            conf = 0.85 + (0.10 if snap.name else 0.0)
            scored.append(LocatorSpec(kind="uia", by="automation_id", value=aid,
                                      timeout_ms=3000, confidence=clamp(conf)))
        # 2. name + control_type
        name = (snap.name or "").strip()
        if name and name.lower() not in _GENERIC_NAMES and len(name) <= 80:
            conf = 0.70 + (0.12 if snap.control_type else 0.0)
            scored.append(LocatorSpec(kind="uia", by="name_type",
                                      value={"name": name, "control_type": snap.control_type},
                                      timeout_ms=2000, confidence=clamp(conf)))
        # 3. structure_path
        if snap.path and snap.depth >= 2:
            stable = bool(snap.name) or bool(snap.automation_id)
            conf = 0.50 + (0.12 if stable else 0.0)
            scored.append(LocatorSpec(kind="uia", by="structure_path", value=snap.path,
                                      timeout_ms=2000, confidence=clamp(conf)))
        # 4. class_name + index（老 Win32）
        if snap.class_name and (snap.framework_id or "").lower() in ("", "win32"):
            idx = _sibling_index_from_path(snap)
            scored.append(LocatorSpec(kind="uia", by="class_index",
                                      value={"class_name": snap.class_name, "index": idx},
                                      timeout_ms=1500, confidence=0.50))
    else:
        # 无 UIA：低置信警告由调用方统一加
        pass

    tail: list[LocatorSpec] = []
    # 5. image 锚点
    if anchor_rel:
        conf = 0.40 + 0.15  # 锚点尺寸在裁剪时已保证 ≥ 目标阈值，纹理判定简化
        tail.append(LocatorSpec(kind="image", anchor_ref=anchor_rel, threshold=0.82,
                                confidence=clamp(conf)))
    # 6. relative（需客户区比例）
    if pos is not None and pos.rel is not None:
        tail.append(LocatorSpec(kind="relative", anchor="window", ratio=pos.rel, confidence=0.45))
    # 7. coord 兜底（恒存在）
    if pos is not None:
        tail.append(LocatorSpec(kind="coord", pos=(pos.x, pos.y), scale=pos.scale or win_scale,
                                screen=(int(screen[0]), int(screen[1])), monitor=pos.monitor,
                                confidence=0.25))

    scored.sort(key=lambda s: s.confidence, reverse=True)
    chain = [s for s in scored if s.confidence >= 0.35] + tail

    step_conf = max((s.confidence for s in chain), default=0.2)
    if snap is None:
        step_conf = min(step_conf, 0.5)
    return chain, round(step_conf, 3)


def _sibling_index_from_path(snap: UiaSnapshot) -> int:
    m = re.search(r"\[(\d+)\]\s*$", snap.path or "")
    return int(m.group(1)) if m else 1


def chain_to_graph_data(step_dict: dict[str, Any]) -> dict[str, Any]:
    return {"locators": step_dict.get("target", {}).get("locators", [])}
