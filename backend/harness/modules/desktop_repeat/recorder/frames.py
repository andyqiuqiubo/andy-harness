"""关键帧策略（§3.6）。

- 裁剪帧：语义事件 pre/post（目标周围含上下文，pad 公式见设计）；
- 窗口变化：全屏帧（长边 1920 归一）；
- 锚点：从 pre 裁剪帧中心再裁 60%（去边缘噪声），存 PNG；
- 差分：灰度 numpy 差值比例（S5/S6 与回放 settle 复用同一算法）。

预算（§3.9）：单帧 grab+crop+encode ≤60ms，超限跳帧计数。
"""

from __future__ import annotations

import io
import logging
from typing import Any

logger = logging.getLogger("harness.repeat.frames")

PAD_MIN = 24
PAD_MAX = 120
CROP_MIN_W, CROP_MIN_H = 64, 48
CROP_MAX_W, CROP_MAX_H = 480, 360


def crop_rect_for(element_rect: tuple[int, int, int, int],
                  click_pos: tuple[int, int] | None,
                  screen_bounds: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """元素 rect（或点击点周围）→ 裁剪框。pad = clamp(0.5*min(w,h), 24, 120)。"""
    el, et, er, eb = element_rect
    w, h = er - el, eb - et
    if w <= 0 or h <= 0:
        # 退化：以点击点为中心取 pad*2 见方
        cx, cy = click_pos or ((el + er) // 2, (et + eb) // 2)
        pad = 96
        el, et, er, eb = cx - pad, cy - pad, cx + pad, cy + pad
        w, h = er - el, eb - et
    pad = max(PAD_MIN, min(PAD_MAX, int(0.5 * min(w, h))))
    cl, ct = el - pad, et - pad
    cr, cb = er + pad, eb + pad
    # 等比缩放进 480×360 上限
    cw, ch = cr - cl, cb - ct
    scale = min(CROP_MAX_W / max(cw, 1), CROP_MAX_H / max(ch, 1), 1.0)
    # 尺寸不足则放大到最小 64×48
    if cw * scale < CROP_MIN_W or ch * scale < CROP_MIN_H:
        scale = max(scale, max(CROP_MIN_W / max(cw, 1), CROP_MIN_H / max(ch, 1)))
    vw, vh = screen_bounds[2] - screen_bounds[0], screen_bounds[3] - screen_bounds[1]
    nw, nh = max(CROP_MIN_W, int(cw * scale)), max(CROP_MIN_H, int(ch * scale))
    cx_, cy_ = (cl + cr) // 2, (ct + cb) // 2
    out_l = max(screen_bounds[0], cx_ - nw // 2)
    out_t = max(screen_bounds[1], cy_ - nh // 2)
    out_r = min(screen_bounds[0] + vw, out_l + nw)
    out_b = min(screen_bounds[1] + vh, out_t + nh)
    return (out_l, out_t, out_r, out_b)


def encode_png(img: Any) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def encode_jpeg(img: Any, quality: int = 80) -> bytes:  # pragma: no cover - 备用编码路径
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def grab_crop_png(grabber: Any, rect: tuple[int, int, int, int]) -> bytes:
    return bytes(grabber.grab(rect))


def anchor_from_crop(crop_png: bytes, ratio: float = 0.6) -> bytes:
    """从 pre 裁剪帧中心裁 ratio 比例 → 图像锚点（PNG 保真，§3.6）。"""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(crop_png)).convert("RGB")
    except Exception as e:  # noqa: BLE001
        logger.warning("锚点裁剪失败（坏帧）: %s", e)
        return b""
    w, h = im.size
    aw, ah = max(16, int(w * ratio)), max(12, int(h * ratio))
    x0, y0 = (w - aw) // 2, (h - ah) // 2
    a = im.crop((x0, y0, x0 + aw, y0 + ah))
    buf = io.BytesIO()
    a.save(buf, format="PNG")
    return buf.getvalue()


def diff_ratio(png_a: bytes, png_b: bytes, block: int = 8) -> float:
    """两 PNG 的粗差比例（0..1）：灰度按 block 像素块聚合，变化块占比。

    用于 S5/S6（pre vs post）与回放 settle（相邻采样）。比逐像素快 ~block² 倍。
    """
    import numpy as np
    from PIL import Image
    try:
        ia = Image.open(io.BytesIO(png_a)).convert("L")
        ib = Image.open(io.BytesIO(png_b)).convert("L")
    except Exception:  # noqa: BLE001
        return 1.0
    if ia.size != ib.size:
        ib = ib.resize(ia.size)
    a = np.asarray(ia, dtype=np.int16)
    b = np.asarray(ib, dtype=np.int16)
    mask = (a - b).__abs__() > 16
    h, w = mask.shape
    hb, wb = max(1, h // block), max(1, w // block)
    blocks = mask[: hb * block, : wb * block].reshape(hb, block, wb, block).mean(axis=(1, 3))
    return float((blocks > 0.25).mean())
