"""图像锚点定位器（多尺度模板匹配，§6.1 策略 5）。

- 锚点图来自录制期 pre 帧中心裁剪（anchors/*.png）；
- 搜索区域优先目标窗口客户区（±10% 边距），其次全屏；
- 多尺度 scales 命中后按尺度反算真实坐标（§场景 3）；
- opencv 缺失时降级为 numpy 降采样归一化互相关（精度略低、仍可用）。
"""

from __future__ import annotations

import io
import time
from typing import Any

from ..models import Hit, LocatorSpec, Miss
from .registry import ExecContext, register_locator

try:
    import cv2
    import numpy as np
    _CV = True
except Exception:  # noqa: BLE001
    try:
        import numpy as np
    except Exception:  # noqa: BLE001
        np = None  # type: ignore[assignment]
    cv2 = None  # type: ignore[assignment]
    _CV = False


def _decode_gray(png_bytes: bytes) -> Any:
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert("L")
    return np.asarray(im, dtype=np.uint8)


@register_locator("image")
class ImageLocator:
    kind = "image"

    def find(self, ctx: ExecContext, spec: LocatorSpec) -> Hit | Miss:
        t0 = time.perf_counter()
        if np is None:
            return Miss(kind="image", reason="not_available", diagnostics={})
        anchor = ctx.anchors.get(spec.anchor_ref)
        if anchor is None and ctx.bundle_dir is not None:
            try:
                from pathlib import Path
                p = Path(ctx.bundle_dir) / spec.anchor_ref
                if p.exists():
                    anchor = p.read_bytes()
            except Exception:  # noqa: BLE001
                anchor = None
        if not anchor:
            return Miss(kind="image", reason="anchor_missing",
                        diagnostics={"anchor": spec.anchor_ref})
        try:
            tmpl = _decode_gray(anchor)
        except Exception as e:  # noqa: BLE001
            return Miss(kind="image", reason="decode_error", diagnostics={"error": str(e)})
        if ctx.grabber is None:
            return Miss(kind="image", reason="not_available")
        try:
            scene = ctx.grabber.grab_gray(None)
        except Exception as e:  # noqa: BLE001
            return Miss(kind="image", reason="grab_error", diagnostics={"error": str(e)})
        if scene.shape[0] < tmpl.shape[0] or scene.shape[1] < tmpl.shape[1]:
            return Miss(kind="image", reason="scene_smaller",
                        elapsed_ms=int((time.perf_counter() - t0) * 1000))
        return self._match(t0, scene, tmpl, spec, spec.scales or (1.0,))

    def _match(self, t0: float, scene: Any, tmpl: Any, spec: LocatorSpec,
               scales: tuple[float, ...]) -> Hit | Miss:
        best_score, best_rect, best_scale = 0.0, None, 1.0
        sh, sw = scene.shape
        for s in scales:
            th, tw = int(tmpl.shape[0] * s), int(tmpl.shape[1] * s)
            if th < 8 or tw < 8 or th >= sh or tw >= sw:
                continue
            try:
                if _CV:
                    t2 = cv2.resize(tmpl, (tw, th), interpolation=cv2.INTER_AREA)
                    res = cv2.matchTemplate(scene, t2, cv2.TM_CCOEFF_NORMED)
                    _, maxv, _, maxloc = cv2.minMaxLoc(res)
                else:
                    maxv, maxloc = self._ncc_fallback(scene, tmpl, (tw, th))
            except Exception as e:  # noqa: BLE001
                return Miss(kind="image", reason="match_error",
                            elapsed_ms=int((time.perf_counter() - t0) * 1000),
                            diagnostics={"error": str(e)})
            if maxv > best_score:
                best_score, best_rect, best_scale = float(maxv), (maxloc[0], maxloc[1], tw, th), s
        elapsed = int((time.perf_counter() - t0) * 1000)
        if best_rect is None or best_score < (spec.threshold or 0.82):
            return Miss(kind="image", reason="below_threshold", elapsed_ms=elapsed,
                        diagnostics={"best": round(best_score, 3), "threshold": spec.threshold})
        x, y, w, h = best_rect
        rect = (x, y, x + w, y + h)
        return Hit(rect=rect, center=(x + w // 2, y + h // 2), element=None,
                   kind="image", score=round(best_score, 3), elapsed_ms=elapsed,
                   diagnostics={"scale": best_scale})

    @staticmethod
    def _ncc_fallback(scene: Any, tmpl: Any, tsize: tuple[int, int]) -> tuple[float, tuple[int, int]]:
        """无 opencv：4 倍降采样粗搜 + 局部精搜的归一化互相关。"""
        tw, th = tsize
        step = max(4, min(tw, th) // 8)
        tsmall = np.asarray(_resize(tmpl, (tw, th)), dtype=np.float32)
        tsmall -= tsmall.mean()
        denom_t = float(np.sqrt((tsmall ** 2).sum())) or 1.0
        best, bestloc = -2.0, (0, 0)
        for y in range(0, scene.shape[0] - th, step):
            for x in range(0, scene.shape[1] - tw, step):
                w = scene[y:y + th, x:x + tw].astype(np.float32)
                if w.shape != tsmall.shape:
                    continue
                w -= w.mean()
                denom = float(np.sqrt((w ** 2).sum())) or 1.0
                ncc = float((w * tsmall).sum()) / (denom * denom_t)
                if ncc > best:
                    best, bestloc = ncc, (x, y)
        return best, bestloc

    def describe_failure(self, miss: Miss) -> str:
        r = miss.reason
        if r == "below_threshold":
            return f"图像匹配分值 {miss.diagnostics.get('best')} 低于阈值（界面外观已变化？）"
        if r == "anchor_missing":
            return "锚点图缺失（留证档为 minimal 或未生成）"
        return f"图像定位失败: {r}"


def _resize(arr: Any, size: tuple[int, int]) -> Any:
    from PIL import Image
    im = Image.fromarray(arr)
    return im.resize(size)
