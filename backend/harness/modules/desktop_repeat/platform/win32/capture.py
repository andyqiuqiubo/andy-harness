"""全局输入钩子（pynput 实现，§3.2 采集流水线）。

纪律（§3.2）：钩子回调只做 5 件事——读时间、读坐标、读键码、读修饰键、入队。
禁止任何系统查询/序列化/日志格式化（低级钩子超时会被系统摘除）。
本实现把回调压缩到 <1ms：仅采集原始字段 + 有界队列 put_nowait。
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from ..base import InputCapture, RawInput

# 焦点窗口快照：键事件必须记录**事件发生时**的焦点控件句柄。
# 钩子回调只做 Win32 廉价查询（GetGUIThreadInfo，微秒级），不做 UIA；
# 后续由 worker 按句柄取语义快照——否则 worker 被后帧采集阻塞时，
# 焦点快照会滞后到下一次点击之后（实测：键入步骤锚到按钮）。
try:
    _user32 = ctypes.windll.user32
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]

    class _GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong), ("flags", ctypes.c_ulong),
            ("hwndActive", ctypes.c_void_p), ("hwndFocus", ctypes.c_void_p),
            ("hwndCapture", ctypes.c_void_p), ("hwndMenuOwner", ctypes.c_void_p),
            ("hwndMoveSize", ctypes.c_void_p), ("hwndCaret", ctypes.c_void_p),
            ("rcCaret", ctypes.c_long * 4),
        ]

    _user32.GetGUIThreadInfo.argtypes = [ctypes.c_ulong, ctypes.POINTER(_GUITHREADINFO)]
    _WIN32 = True
except Exception:  # noqa: BLE001 - 非 Windows / 受限环境：降级为 0
    _WIN32 = False
    _GUITHREADINFO = None  # type: ignore[assignment,misc]

# pynput 是可选依赖（repeat extra）；缺失时 available=False，优雅降级。
try:
    from pynput import keyboard as _kb
    from pynput import mouse as _ms
    _PYNPUT_OK = True
except Exception:  # noqa: BLE001
    _kb = None
    _ms = None
    _PYNPUT_OK = False


_OEM_VK = {0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
           0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'"}


def _name_from_vk(vk: int) -> str:
    """虚拟键码 → 键名（字母/数字/功能键/常用符号）。

    Ctrl+Alt+字母 会被 Windows 当作 AltGr 处理，pynput 的 KeyCode 此时
    ``char`` 为 None；若只用 char 命名，``ctrl+alt+r`` 这类热键永远匹配不上
    （真机验证坐实：全局停止/丢弃热键失效）。VK 是与布局无关的可靠信息。
    """
    if 0x41 <= vk <= 0x5A:
        return chr(vk + 32)
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x60 <= vk <= 0x69:      # 小键盘 0-9
        return str(vk - 0x60)
    if 0x70 <= vk <= 0x87:      # F1-F24
        return f"f{vk - 0x6F}"
    return _OEM_VK.get(vk, "")


def _focus_hwnd() -> int:
    """当前前台线程的焦点控件句柄（廉价查询，供键事件锚定控件语义）。"""
    if not _WIN32:
        return 0
    try:
        h = _user32.GetForegroundWindow()
        if not h:
            return 0
        pid = ctypes.c_ulong(0)
        tid = _user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if not tid:
            return 0
        info = _GUITHREADINFO()  # type: ignore[misc]
        info.cbSize = ctypes.sizeof(info)
        if _user32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndFocus:
            return int(info.hwndFocus)
    except Exception:  # noqa: BLE001
        return 0
    return 0


def _modifier_bitmap(event: Any) -> int:
    """从按键事件提取修饰键位图（回调内零查询：直接读 pynput 已解析的属性）。"""
    mods = 0
    try:
        key = getattr(event, "key", None)
        name = str(key) if key is not None else ""
        if name.startswith("cmd") or name == "ctrl_l" or name == "ctrl_r":
            mods |= 2
        if name.startswith("alt"):
            mods |= 4
        if name.startswith("shift"):
            mods |= 1
    except Exception:  # noqa: BLE001
        pass
    return mods


class PynputCapture(InputCapture):
    """pynput 低级钩子。stop() 必须干净摘钩（R3：不驻留，降低杀软误判面）。"""

    def __init__(self, capacity: int = 4096) -> None:
        self._q: queue.Queue[RawInput] = queue.Queue(maxsize=capacity)
        self._listeners: list[Any] = []
        self._running = False
        self._dropped = 0
        self._slow_count = 0
        self._on_event: Callable[[RawInput], None] | None = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return _PYNPUT_OK

    # ── 入队（背压策略 §3.9：满时按序丢弃——move 先丢，click/key 的 down 永不丢） ──
    def _push(self, ev: RawInput) -> None:
        sink = self._on_event
        if sink is not None:
            # 调用方约定：sink 必须只做非阻塞入队（保持钩子回调 <1ms 纪律）
            try:
                sink(ev)
            except Exception:  # noqa: BLE001 - 回调绝不抛出
                self._slow_count += 1
            return
        try:
            self._q.put_nowait(ev)
            return
        except queue.Full:
            self._dropped += 1
        if ev.get("kind") == "move":
            return  # move 直接丢弃
        # 重要事件：尝试挤掉队列中最早的一条 move；没有 move 则丢本条（宁可少采）
        try:
            dq = self._q.queue
            for i in range(len(dq)):
                if dq[i].get("kind") == "move":
                    del dq[i]
                    self._q.put_nowait(ev)
                    return
        except Exception:  # noqa: BLE001
            pass

    def drain(self, max_items: int = 0) -> list[RawInput]:
        """非阻塞取出当前积压（worker 线程调用）。"""
        out: list[RawInput] = []
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            out.append(item)
            if max_items and len(out) >= max_items:
                break
        return out

    def stats(self) -> dict[str, int]:
        return {"dropped": self._dropped, "slow": self._slow_count, "qsize": self._q.qsize()}

    def queue_size(self) -> int:
        return self._q.qsize()

    # ── 鼠标 ─────────────────────────────────────────────────────────
    def _on_move(self, x: float, y: float) -> None:
        self._push({"kind": "move", "x": int(x), "y": int(y), "t_ns": time.perf_counter_ns()})

    def _on_click(self, x: float, y: float, button: Any, pressed: bool) -> None:
        self._push({
            "kind": "button", "x": int(x), "y": int(y),
            "button": getattr(button, "name", str(button)), "pressed": bool(pressed),
            "t_ns": time.perf_counter_ns(),
        })

    def _on_scroll(self, x: float, y: float, dx: int, dy: int) -> None:
        self._push({"kind": "scroll", "x": int(x), "y": int(y),
                    "dx": int(dx), "dy": int(dy), "t_ns": time.perf_counter_ns()})

    # ── 键盘（注意：pynput 的 on_press/on_release 在回调线程触发） ──
    def _on_press(self, key: Any) -> None:
        self._key_event(key, True)

    def _on_release(self, key: Any) -> None:
        self._key_event(key, False)

    def _key_event(self, key: Any, down: bool) -> None:
        try:
            name_attr = getattr(key, "name", None)
            if name_attr:
                name = str(name_attr)
                char = None
            else:
                char = getattr(key, "char", None)
                name = char if char is not None else str(key)
            vk = 0
            try:
                raw_vk = getattr(key, "vk", None)
                vk = int(raw_vk) if raw_vk is not None else 0
            except Exception:  # noqa: BLE001
                vk = 0
            if not char and vk:
                # AltGr 组合 / 无字符布局：回退到 VK 命名（保证热键可匹配）
                vk_name = _name_from_vk(vk)
                if vk_name:
                    name = vk_name
            ev: RawInput = {"kind": "key", "down": down, "name": name,
                            "vk": vk, "mods": _modifier_bitmap(key),
                            "focus_hwnd": _focus_hwnd(),
                            "t_ns": time.perf_counter_ns()}
            if char:
                ev["char"] = char
            self._push(ev)
        except Exception:  # noqa: BLE001 - 回调绝不抛出（抛穿会崩监听线程）
            self._slow_count += 1

    # ── 生命周期 ─────────────────────────────────────────────────────
    def start(self, on_event: Callable[[RawInput], None] | None = None) -> None:
        if not _PYNPUT_OK:
            raise RuntimeError("pynput 未安装：pip install 'harness[repeat]'（或 pynput）")
        with self._lock:
            if self._running:
                return
            self._on_event = on_event
            ml = _ms.Listener(on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll)
            kl = _kb.Listener(on_press=self._on_press, on_release=self._on_release)
            ml.start()
            kl.start()
            self._listeners = [ml, kl]
            self._running = True

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            for listener in self._listeners:
                try:
                    listener.stop()
                except Exception:  # noqa: BLE001
                    pass
            self._listeners = []
            self._running = False
            self._on_event = None
