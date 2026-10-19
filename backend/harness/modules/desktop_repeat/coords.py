"""坐标换算与环境漂移分级（§6.2）。

纯函数，无平台依赖，可单测；对应设计：三坐标空间 phys/logical/rel、
L0–L3 漂移分级、SendInput 归一化、坐标闸门 coord_allowed。
"""

from __future__ import annotations

from .models import Env, MonitorInfo

EPS = 1e-6


def phys_to_logical(x: int, y: int, monitors: list[MonitorInfo]) -> tuple[float, float, int]:
    """物理坐标 → 该显示器逻辑坐标（返回 scale 供换算回填）。"""
    m = monitor_at(x, y, monitors) or (monitors[0] if monitors else None)
    if m is None:
        return float(x), float(y), 0
    lx = (x - m.rect[0]) / m.scale
    ly = (y - m.rect[1]) / m.scale
    return lx, ly, m.index


def logical_to_phys(lx: float, ly: float, m: MonitorInfo) -> tuple[int, int]:
    return int(round(lx * m.scale + m.rect[0])), int(round(ly * m.scale + m.rect[1]))


def to_rel(x: int, y: int, client: tuple[int, int, int, int]) -> tuple[float, float]:
    cl, ct, cr, cb = client
    w, h = max(1, cr - cl), max(1, cb - ct)
    return round((x - cl) / w, 4), round((y - ct) / h, 4)


def from_rel(rx: float, ry: float, client: tuple[int, int, int, int]) -> tuple[int, int]:
    cl, ct, cr, cb = client
    w, h = max(1, cr - cl), max(1, cb - ct)
    return int(round(cl + rx * w)), int(round(ct + ry * h))


def monitor_at(x: int, y: int, monitors: list[MonitorInfo]) -> MonitorInfo | None:
    for m in monitors:
        ml, mt, mr, mb = m.rect
        if ml <= x < mr and mt <= y < mb:
            return m
    return None


def monitor_at_index(x: int, y: int, monitors: list[MonitorInfo]) -> int:
    m = monitor_at(x, y, monitors)
    if m is not None:
        return m.index
    return monitors[0].index if monitors else 1


def normalize_for_inject(x: int, y: int, virtual: tuple[int, int, int, int]) -> tuple[int, int]:
    """SendInput 绝对模式归一化（§6.2 公式；virtual=(left,top,width,height)）。"""
    vl, vt, vw, vh = virtual
    vw, vh = max(2, vw), max(2, vh)
    nx = round((x - vl) * 65535 / (vw - 1))
    ny = round((y - vt) * 65535 / (vh - 1))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


# ── 漂移分级（§6.2） ────────────────────────────────────────────
def classify_env(base: Env, cur: Env) -> str:
    """L0 完全一致 / L1 缩放变逻辑同 / L2 同比例分辨率变 / L3 拓扑或比例变。"""
    b_rects = [m.rect for m in base.monitors]
    c_rects = [m.rect for m in cur.monitors]
    if b_rects == c_rects:
        return "L0"
    if len(b_rects) != len(c_rects):
        return "L3"
    # 显示器数量相同：先看是否仅缩放变化
    if all(abs(b.scale - c.scale) > EPS for b, c in zip(base.monitors, cur.monitors)) or \
       any(abs(b.scale - c.scale) > EPS for b, c in zip(base.monitors, cur.monitors)):
        if logical_geometry_equal(base, cur):
            return "L1"
    if same_aspect_and_count(base, cur):
        return "L2"
    return "L3"


def logical_geometry_equal(base: Env, cur: Env) -> bool:
    """缩放不同但逻辑分辨率一致（phys/scale 相同，且原点一致）。"""
    if len(base.monitors) != len(cur.monitors):
        return False
    for b, c in zip(base.monitors, cur.monitors):
        bw, bh = (b.rect[2] - b.rect[0]) / b.scale, (b.rect[3] - b.rect[1]) / b.scale
        cw, ch = (c.rect[2] - c.rect[0]) / c.scale, (c.rect[3] - c.rect[1]) / c.scale
        if abs(bw - cw) > 2 or abs(bh - ch) > 2:
            return False
        if b.rect[0] != c.rect[0] or b.rect[1] != c.rect[1]:
            return False
    return True


def same_aspect_and_count(base: Env, cur: Env) -> bool:
    """显示器数量一致且每台比例相同（仅物理尺寸缩放）。"""
    if len(base.monitors) != len(cur.monitors) or not base.monitors:
        return False
    for b, c in zip(base.monitors, cur.monitors):
        bw, bh = b.rect[2] - b.rect[0], b.rect[3] - b.rect[1]
        cw, ch = c.rect[2] - c.rect[0], c.rect[3] - c.rect[1]
        if bh == 0 or ch == 0:
            return False
        if abs(bw / bh - cw / ch) > 0.02:
            return False
    return True


def coord_allowed(level: str, gate: bool, allow_blind: bool) -> bool:
    """兜底闸门（ADR-05）：默认仅 L0/L1 放行 coord；allow_blind_coord 显式放行。"""
    if allow_blind:
        return True
    if not gate:
        return True
    return level in ("L0", "L1")


ENV_LEVEL_LABELS = {
    "L0": "环境与录制时一致",
    "L1": "缩放已变化，坐标已按逻辑分辨率换算",
    "L2": "分辨率已变化（比例一致），坐标定位已停用",
    "L3": "显示器拓扑/比例已变化，坐标定位已停用，建议重新录制",
}


def client_area_scale_ok(rel_ratio_change: float) -> bool:
    """relative 策略可用性：客户区尺寸变化 <50%（§场景 2）。"""
    return rel_ratio_change < 0.5
