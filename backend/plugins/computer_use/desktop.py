"""桌面控制抽象层。

Computer Use 的「手和眼」。定义统一接口 DesktopController，并提供：
- RealDesktopController：基于 pyautogui（鼠标/键盘）+ mss（截图）的真实实现，
  依赖为**可选**第三方库（见 pyproject / 文档），缺失或无显示器时优雅降级。
- FakeDesktopController：测试替身，记录调用并返回占位 PNG，无需真实桌面。

这样模型调用循环只依赖抽象接口，既可在无显示环境做单元测试，也便于
将来替换底层实现（如远程桌面协议）。
"""

from __future__ import annotations

import io
from abc import ABC, abstractmethod
from typing import Any

# 可选桌面依赖：缺失时保持为 None，由 RealDesktopController 在运行时优雅降级。
# pyautogui 无类型桩，用 import-untyped 抑制；except 分支回退为 None 用 assignment 抑制。
try:
    import mss as _mss_module
except Exception:  # noqa: BLE001 - 可选依赖，缺失属正常
    _mss_module = None  # type: ignore[assignment]

try:
    import pyautogui as _pyautogui_module  # type: ignore
except Exception:  # noqa: BLE001 - 可选依赖，缺失属正常
    _pyautogui_module = None


class DesktopUnavailableError(RuntimeError):
    """桌面控制不可用（缺少依赖或当前无图形会话）。"""


class DesktopController(ABC):
    """桌面操作统一接口。"""

    @property
    @abstractmethod
    def available(self) -> bool:
        """当前是否可用（依赖齐全且有显示器）。"""

    @abstractmethod
    async def screenshot(self) -> bytes:
        """返回当前主屏幕的 PNG 字节。"""

    @abstractmethod
    async def mouse_move(self, x: int, y: int) -> None:
        """移动鼠标到绝对坐标 (x, y)。"""

    @abstractmethod
    async def mouse_click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None:
        """在 (x, y) 处点击。"""

    @abstractmethod
    async def mouse_drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
        """从 (x1, y1) 拖拽到 (x2, y2)。"""

    @abstractmethod
    async def keyboard_type(self, text: str) -> None:
        """输入一段文本。"""

    @abstractmethod
    async def keyboard_press(self, keys: str) -> None:
        """按下一个/组合键，如 'ctrl+a'、'enter'、'alt+f4'。"""


class RealDesktopController(DesktopController):
    """真实桌面控制（pyautogui + mss）。依赖缺失时 available=False。"""

    def __init__(self) -> None:
        self._pyautogui: Any = _pyautogui_module
        self._mss: Any = _mss_module
        self._ok = self._pyautogui is not None and self._mss is not None

    @property
    def available(self) -> bool:
        return self._ok

    def _ensure(self) -> None:
        if not self._ok:
            raise DesktopUnavailableError(
                "桌面控制不可用：未安装 pyautogui/mss，或当前无图形会话。"
                "请 pip install pyautogui mss 后在带显示的环境中运行。"
            )

    async def screenshot(self) -> bytes:
        self._ensure()
        # monitors[0] 是「所有显示器拼接」，[1] 为主显示器
        with self._mss.mss() as s:
            monitor = s.monitors[1] if len(s.monitors) > 1 else s.monitors[0]
            img = s.grab(monitor)
            png = self._mss.tools.to_png(img.rgb, img.size)
        return bytes(png)

    async def mouse_move(self, x: int, y: int) -> None:
        self._ensure()
        self._pyautogui.moveTo(x, y)

    async def mouse_click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None:
        self._ensure()
        self._pyautogui.click(x, y, clicks=clicks, button=button)

    async def mouse_drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
        self._ensure()
        pg = self._pyautogui
        pg.moveTo(x1, y1)
        pg.dragTo(x2, y2, button=button, duration=0.3)

    async def keyboard_type(self, text: str) -> None:
        self._ensure()
        self._pyautogui.write(text, interval=0.01)

    async def keyboard_press(self, keys: str) -> None:
        self._ensure()
        # 支持 "ctrl+a" 这类组合键
        self._pyautogui.hotkey(*keys.lower().split("+"))


class FakeDesktopController(DesktopController):
    """测试替身：记录调用，返回 1x1 红色 PNG。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._png = self._make_png()

    @staticmethod
    def _make_png() -> bytes:
        # 生成最小合法 PNG，供测试断言「截图被当作图像回填」
        try:
            from PIL import Image

            buf = io.BytesIO()
            Image.new("RGB", (2, 2), (255, 0, 0)).save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return b"\x89PNG\r\n\x1a\n"

    @property
    def available(self) -> bool:
        return True

    async def screenshot(self) -> bytes:
        self.calls.append({"op": "screenshot"})
        return self._png

    async def mouse_move(self, x: int, y: int) -> None:
        self.calls.append({"op": "mouse_move", "x": x, "y": y})

    async def mouse_click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None:
        self.calls.append({"op": "mouse_click", "x": x, "y": y, "button": button, "clicks": clicks})

    async def mouse_drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
        self.calls.append({"op": "mouse_drag", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "button": button})

    async def keyboard_type(self, text: str) -> None:
        self.calls.append({"op": "keyboard_type", "text": text})

    async def keyboard_press(self, keys: str) -> None:
        self.calls.append({"op": "keyboard_press", "keys": keys})
