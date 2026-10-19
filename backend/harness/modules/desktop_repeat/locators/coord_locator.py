"""绝对坐标兜底（§6.1 策略 7）。

存储与换算（ADR-04/ADR-05）：
- spec.pos 为录制时物理坐标（虚拟桌面系）；
- L0 直接用；L1（缩放变逻辑同，且定义保证显示器原点不变）按
  x' = origin + (x - origin)/rec_scale * cur_scale 换算；
- L2/L3 由 resolve_chain 的环境闸门拦截——本 find 只在放行时到达；
  allow_blind_coord 显式放行时命中打 blind 标记进审计。
"""

from __future__ import annotations

import time

from ..models import Hit, LocatorSpec, Miss
from .registry import ExecContext, register_locator


@register_locator("coord")
class CoordLocator:
    kind = "coord"

    def find(self, ctx: ExecContext, spec: LocatorSpec) -> Hit | Miss:
        t0 = time.perf_counter()
        if spec.pos is None:
            return Miss(kind="coord", reason="no_pos")
        x, y = int(spec.pos[0]), int(spec.pos[1])
        if ctx.env_level == "L1":
            x, y = self._rescale(x, y, spec)
        elapsed = int((time.perf_counter() - t0) * 1000)
        return Hit(rect=(x, y, x, y), center=(x, y), element=None, kind="coord",
                   score=spec.confidence, elapsed_ms=elapsed,
                   diagnostics={"env_level": ctx.env_level,
                                "blind": ctx.env_level in ("L2", "L3")})

    @staticmethod
    def _rescale(x: int, y: int, spec: LocatorSpec) -> tuple[int, int]:
        """L1：显示器原点不变，仅缩放变——按逻辑坐标重映射。"""
        try:
            from ..platform.win32.env import WinEnvProvider
            cur = WinEnvProvider().snapshot()
            if not (1 <= spec.monitor <= len(cur.monitors)):
                return x, y
            m = cur.monitors[spec.monitor - 1]
            rec_scale = spec.scale or 1.0
            if rec_scale <= 0:
                return x, y
            ox, oy = m.rect[0], m.rect[1]
            lx = (x - ox) / rec_scale
            ly = (y - oy) / rec_scale
            return int(round(lx * m.scale + ox)), int(round(ly * m.scale + oy))
        except Exception:  # noqa: BLE001
            return x, y

    def describe_failure(self, miss: Miss) -> str:
        if miss.reason == "env_gated":
            return "环境已变化（分辨率/拓扑），坐标兜底被闸门禁用——建议重新录制或补充图像锚点"
        if miss.reason == "no_pos":
            return "录制数据不含坐标"
        return f"坐标兜底失败: {miss.reason}"
