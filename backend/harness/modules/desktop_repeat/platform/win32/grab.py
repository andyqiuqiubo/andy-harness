"""屏幕抓取（mss 实现）。

- 坐标一律物理像素（虚拟桌面系，可为负）。mss 的 monitors[0] 为整机虚拟桌面，
  [1..] 为各物理显示器；本模块统一用「虚拟桌面原点 + 偏移」裁剪任意 rect。
- grab() 返回 PNG bytes（Pillow 编码，避免调用方处理 BGRA 裸数据）。
- grab_gray() 返回 numpy 灰度数组，供帧差分与图像锚点匹配复用（§3.6/§6.4）。
"""

from __future__ import annotations

import io
from typing import Any

from ...models import MonitorInfo
from ..base import ScreenGrabber

try:
    import mss as _mss
    _MSS_OK = True
except Exception:  # noqa: BLE001
    _mss = None
    _MSS_OK = False

try:
    import numpy as _np
    _NP_OK = True
except Exception:  # noqa: BLE001
    _np = None  # type: ignore[assignment]
    _NP_OK = False

try:
    from PIL import Image as _Image
    _PIL_OK = True
except Exception:  # noqa: BLE001
    _Image = None  # type: ignore[assignment]
    _PIL_OK = False

_MAX_LONG_EDGE_FULL = 1920  # 全屏帧长边归一（§3.6）


class MssGrabber(ScreenGrabber):
    """线程不安全：mss 实例与调用线程绑定。每线程一个实例（worker/主线程各一）。"""

    def __init__(self) -> None:
        self._sct: object | None = None
        self._thread_id: int | None = None

    def _sct_for_thread(self) -> Any:
        import threading
        tid = threading.get_ident()
        if self._sct is None or self._thread_id != tid:
            if not _MSS_OK:
                raise RuntimeError("mss 未安装：pip install 'harness[repeat]'")
            self._sct = _mss.MSS()
            self._thread_id = tid
        return self._sct

    def available(self) -> bool:
        return _MSS_OK

    def grab(self, rect: tuple[int, int, int, int] | None = None) -> bytes:
        """返回 PNG bytes；rect=None 抓虚拟桌面全屏（长边归一）。"""
        sct = self._sct_for_thread()
        if rect is None:
            mon = sct.monitors[0]
            region = {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]}
        else:
            gl, gt, gr, gb = rect
            region = {"left": int(gl), "top": int(gt), "width": max(1, int(gr - gl)), "height": max(1, int(gb - gt))}
        img = sct.grab(region)
        if not _PIL_OK:
            # 无 Pillow 时退回 mss 自带 PNG 编码（仅全帧，无缩放）
            return bytes(_mss.tools.to_png(img.rgb, img.size) or b"")
        pil = _Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
        if rect is None:
            w_, h_ = pil.size
            if max(w_, h_) > _MAX_LONG_EDGE_FULL:
                ratio = _MAX_LONG_EDGE_FULL / max(w_, h_)
                lanczos = getattr(_Image, "Resampling", _Image).LANCZOS
                pil = pil.resize((max(1, int(w_ * ratio)), max(1, int(h_ * ratio))), lanczos)
        buf = io.BytesIO()
        pil.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    def grab_jpeg(self, rect: tuple[int, int, int, int] | None = None, quality: int = 80) -> bytes:
        sct = self._sct_for_thread()
        if rect is None:
            mon = sct.monitors[0]
            region = {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]}
        else:
            gl, gt, gr, gb = rect
            region = {"left": int(gl), "top": int(gt), "width": max(1, int(gr - gl)), "height": max(1, int(gb - gt))}
        img = sct.grab(region)
        if not _PIL_OK:
            return self.grab(rect)
        pil = _Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    def grab_gray(self, rect: tuple[int, int, int, int] | None = None) -> Any:
        """灰度 numpy 数组（H×W, uint8）。帧差分与模板匹配共用。"""
        if not _NP_OK:
            raise RuntimeError("numpy 未安装")
        sct = self._sct_for_thread()
        if rect is None:
            mon = sct.monitors[0]
            region = {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]}
        else:
            gl, gt, gr, gb = rect
            region = {"left": int(gl), "top": int(gt), "width": max(1, int(gr - gl)), "height": max(1, int(gb - gt))}
        img = sct.grab(region)
        arr = _np.array(img, dtype=_np.uint8)[:, :, :3]  # BGRA → BGR
        gray = arr @ _np.array([0.114, 0.587, 0.299], dtype=_np.float32)
        return gray.astype(_np.uint8)

    def monitors(self) -> list[MonitorInfo]:
        # 复用 env 层实现（DPI 信息在 mss 里拿不到，保持单一事实源）
        from .env import WinEnvProvider
        return WinEnvProvider()._monitors()

    def close(self) -> None:
        try:
            if self._sct is not None:
                sct: Any = self._sct
                sct.close()
        except Exception:  # noqa: BLE001
            pass
        self._sct = None
