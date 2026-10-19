"""输入注入（SendInput 直连实现，§6.3/§6.2；ADR-09 文本三通道之一）。

要点：
- 绝对坐标注入必须归一化到虚拟桌面 0..65535 且带 VIRTUALDESK 标志（§6.2 公式）；
- 所有注入事件天然带 LLMHF_INJECTED 标记（SendInput 所致），场景 12 的人工介入
  检测依赖该标记区分"自己的输入"与"人的输入"；
- SendInput 返回实际插入数；返回 0 且 GetLastError()==5(ERROR_ACCESS_DENIED)
  即 UIPI 拦截 → RP-4001（§6.3 注入安全细节）；
- 文本注入用 KEYEVENTF_UNICODE（UTF-16 code unit；代理对拆两次），
  不依赖键盘布局、不触发 IME（ADR-09）。
"""

from __future__ import annotations

import ctypes

from ..base import InputInjector
from . import winapi as w

ERROR_ACCESS_DENIED = 5

_BUTTON_FLAGS = {
    "left": (w.MOUSEEVENTF_LEFTDOWN, w.MOUSEEVENTF_LEFTUP),
    "right": (w.MOUSEEVENTF_RIGHTDOWN, w.MOUSEEVENTF_RIGHTUP),
    "middle": (w.MOUSEEVENTF_MIDDLEDOWN, w.MOUSEEVENTF_MIDDLEUP),
}

# VK 映射表（组合键/功能键；字符键走 Unicode 通道不需要 VK）
_VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "cmd": 0x5B, "meta": 0x5B, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "space": 0x20, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "prior": 0x21,
    "pagedown": 0x22, "next": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "printscreen": 0x2C, "snapshot": 0x2C, "menu": 0x0D, "capslock": 0x14,
}
_EXTENDED_VK = {0x2D, 0x2E, 0x21, 0x22, 0x23, 0x24, 0x70, 0x71, 0x72, 0x73, 0x74, 0x75,
                0x76, 0x77, 0x78, 0x79, 0x7A, 0x7B, 0x2C, 0x25, 0x26, 0x27, 0x28, 0x5B}


def _virtual_bounds() -> tuple[int, int, int, int]:
    vx = w.GetSystemMetrics(w.SM_XVIRTUALSCREEN)
    vy = w.GetSystemMetrics(w.SM_YVIRTUALSCREEN)
    vw = w.GetSystemMetrics(w.SM_CXVIRTUALSCREEN)
    vh = w.GetSystemMetrics(w.SM_CYVIRTUALSCREEN)
    return vx, vy, max(vw, 2), max(vh, 2)


def _abs_pos(x: int, y: int) -> tuple[int, int]:
    vx, vy, vw, vh = _virtual_bounds()
    nx = round((x - vx) * 65535 / (vw - 1))
    ny = round((y - vy) * 65535 / (vh - 1))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def _make_mouse_input(flags: int, x: int, y: int, data: int = 0) -> w.INPUT:
    # 任何事件（move / down / up / wheel）都携带绝对坐标：先归位再触发，
    # 避免"点击落到光标旧位置"的竞态；ABSOLUTE|VIRTUALDESK 一并置位（§6.2）。
    nx, ny = _abs_pos(x, y)
    mi = w.MOUSEINPUT(
        dx=nx, dy=ny, mouseData=data,
        dwFlags=flags | w.MOUSEEVENTF_ABSOLUTE | w.MOUSEEVENTF_VIRTUALDESK,
        time=0, dwExtraInfo=0,
    )
    inp = w.INPUT(type=w.INPUT_MOUSE)
    inp.ii.mi = mi
    return inp


def _send(inputs: list[w.INPUT]) -> int:
    if not inputs:
        return 0
    arr = (w.INPUT * len(inputs))(*inputs)
    n = w.SendInput(len(inputs), arr, ctypes.sizeof(w.INPUT))
    if n == 0:
        err = w.GetLastError()
        if err == ERROR_ACCESS_DENIED:
            raise PermissionError("RP-4001 UIPI 拦截：目标以管理员权限运行，请以同级权限启动 harness")
        raise OSError(f"RP-4001 SendInput 失败 GetLastError={err}")
    return int(n)


class SendInputInjector(InputInjector):
    def available(self) -> bool:
        return True

    def move(self, x: int, y: int) -> int:
        return _send([_make_mouse_input(w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE, x, y)])

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> int:
        down_f, up_f = _BUTTON_FLAGS.get(button, _BUTTON_FLAGS["left"])
        inputs: list[w.INPUT] = []
        # 先绝对移动到位（不带按键标志），再按下/抬起；双击=两组
        inputs.append(_make_mouse_input(w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE, x, y))
        for _ in range(max(1, count)):
            inputs.append(_make_mouse_input(down_f, x, y))
            inputs.append(_make_mouse_input(up_f, x, y))
        return _send(inputs)

    def press(self, button: str, down: bool, x: int | None = None, y: int | None = None) -> int:
        """按下或抬起单个按键（drag 用：down→move*→up）。"""
        down_f, up_f = _BUTTON_FLAGS.get(button, _BUTTON_FLAGS["left"])
        flag = down_f if down else up_f
        if x is None or y is None:
            p = w.POINT()
            w.GetCursorPos(w.ctypes.byref(p))
            x, y = int(p.x), int(p.y)
        return _send([_make_mouse_input(flag, int(x), int(y))])

    def scroll(self, x: int, y: int, dx: int, dy: int) -> int:
        inputs = [_make_mouse_input(w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE, x, y)]
        if dy:
            inputs.append(_make_mouse_input(w.MOUSEEVENTF_WHEEL, x, y, int(dy) * w.WHEEL_DELTA))
        if dx:
            inputs.append(_make_mouse_input(w.MOUSEEVENTF_HWHEEL, x, y, int(dx) * w.WHEEL_DELTA))
        return _send(inputs)

    def press_key(self, key: str, down: bool = True) -> int:
        vk = self._resolve_vk(key)
        if vk is None:
            return 0
        flags = 0
        if vk in _EXTENDED_VK:
            flags |= w.KEYEVENTF_EXTENDEDKEY
        scan = w.MapVirtualKeyW(vk, 0)
        ki = w.KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags | (0 if down else w.KEYEVENTF_KEYUP),
                          time=0, dwExtraInfo=0)
        inp = w.INPUT(type=w.INPUT_KEYBOARD)
        inp.ii.ki = ki
        return _send([inp])

    def hotkey(self, keys: list[str]) -> int:
        """按下顺序左→右，释放逆序（§6.3）。"""
        vks = [self._resolve_vk(k) for k in keys]
        if any(v is None for v in vks):
            return 0
        inputs: list[w.INPUT] = []
        for vk in vks:
            inputs.append(self._key_input(int(vk), True))  # type: ignore[arg-type]
        for vk in reversed(vks):
            inputs.append(self._key_input(int(vk), False))  # type: ignore[arg-type]
        return _send(inputs)

    def type_unicode(self, text: str) -> int:
        """UTF-16 code unit 注入；代理对（emoji）拆两次（§6.3）。"""
        if not text:
            return 0
        units: list[int] = []
        enc = text.encode("utf-16-le", errors="surrogatepass")
        for i in range(0, len(enc), 2):
            u = int.from_bytes(enc[i:i + 2], "little")
            if u:
                units.append(u)
        inputs: list[w.INPUT] = []
        for u in units:
            for down in (True, False):
                ki = w.KEYBDINPUT(wVk=0, wScan=u,
                                  dwFlags=w.KEYEVENTF_UNICODE | (0 if down else w.KEYEVENTF_KEYUP),
                                  time=0, dwExtraInfo=0)
                inp = w.INPUT(type=w.INPUT_KEYBOARD)
                inp.ii.ki = ki
                inputs.append(inp)
        return _send(inputs)

    # ── helpers ────────────────────────────────────────────────────
    @staticmethod
    def _key_input(vk: int, down: bool) -> w.INPUT:
        flags = w.KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VK else 0
        scan = w.MapVirtualKeyW(vk, 0)
        ki = w.KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags | (0 if down else w.KEYEVENTF_KEYUP),
                          time=0, dwExtraInfo=0)
        inp = w.INPUT(type=w.INPUT_KEYBOARD)
        inp.ii.ki = ki
        return inp

    @staticmethod
    def _resolve_vk(key: str) -> int | None:
        k = str(key).strip().lower()
        if not k:
            return None
        if k in _VK:
            return _VK[k]
        if len(k) == 1:
            ch = k
            if "a" <= ch <= "z":
                return ord(ch.upper())
            if "0" <= ch <= "9":
                return ord(ch)
            vk = w.MapVirtualKeyW(ord(ch), 0)  # MAPVK_VK_TO_VSC：反查布局（0x?? 键）
            return int(vk) or None
        return None
