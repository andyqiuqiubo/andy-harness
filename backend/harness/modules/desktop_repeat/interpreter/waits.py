"""S5 等待点推断（§5.4）：基于事件间隔（有效时间轴）与帧差分。

gap >1500ms → pre_wait（min_delay = min(gap*0.6, 5000)）；
post_diff >2% → wait_idle（界面稳定等待）。
"""

from __future__ import annotations

from typing import Any

GAP_MS = 1500
MIN_DELAY_CAP_MS = 5000


def infer_timing(prev_t_eff: int | None, cur: Any) -> dict[str, Any]:
    """cur: SemEvent。返回 timing dict。"""
    timing: dict[str, Any] = {}
    if prev_t_eff is not None:
        gap = max(0, int(cur.t_eff - prev_t_eff))
        if gap > GAP_MS:
            kind = "idle_or_time"
            if cur.type in ("click", "right_click", "double_click", "type"):
                kind = "element_appear"
            timing["pre_wait"] = {"kind": kind,
                                  "min_delay_ms": min(int(gap * 0.6), MIN_DELAY_CAP_MS),
                                  "timeout_ms": 8000}
    diff = cur.ev.data.get("post_diff")
    if isinstance(diff, (int, float)) and diff > 0.02:
        timing["settle_ms"] = 250 if cur.type in ("click", "type", "drag") else 150
    else:
        timing["settle_ms"] = 150
    return timing
