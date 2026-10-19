"""环境基线 / 图形会话 / 提权判断 / 输入租约（win32）。

对应设计 §4.4 env、§6.2 坐标模型的显示器/DPI 事实源，以及 §5.6 输入互斥。
"""

from __future__ import annotations

import ctypes
import os
import platform
import time
from typing import Any

from ...models import Env, MonitorInfo
from ..base import EnvProvider, InputLease
from . import winapi as w

ERROR_ALREADY_EXISTS = 183


class WinEnvProvider(EnvProvider):
    def snapshot(self) -> Env:
        monitors = self._monitors()
        vx = w.GetSystemMetrics(w.SM_XVIRTUALSCREEN)
        vy = w.GetSystemMetrics(w.SM_YVIRTUALSCREEN)
        vw = w.GetSystemMetrics(w.SM_CXVIRTUALSCREEN)
        vh = w.GetSystemMetrics(w.SM_CYVIRTUALSCREEN)
        sid = 0
        if w.WTSGetActiveConsoleSessionId is not None:
            try:
                sid = int(w.WTSGetActiveConsoleSessionId())
            except Exception:  # noqa: BLE001
                sid = 0
        return Env(
            os=f"{platform.system()} {platform.release()} {platform.version()}",
            session_id=sid,
            input_desktop=self._desktop_name(),
            virtual_screen=(vx, vy, vx + vw, vy + vh),
            monitors=monitors,
        )

    def _monitors(self) -> list[MonitorInfo]:
        out: list[MonitorInfo] = []

        def _cb(hmon: Any, _hdc: Any, _rect: Any, _lparam: int) -> bool:  # noqa: ANN401
            mi = w.MONITORINFOEXW()
            mi.cbSize = ctypes.sizeof(mi)
            try:
                if w.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    dpi = 96
                    if w.GetDpiForMonitor is not None:
                        ux = ctypes.c_uint()
                        uy = ctypes.c_uint()
                        try:
                            if w.GetDpiForMonitor(hmon, 0, ctypes.byref(ux), ctypes.byref(uy)) == 0:
                                dpi = int(ux.value) or 96
                        except Exception:  # noqa: BLE001
                            dpi = 96
                    rect = (mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom)
                    work = (mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom)
                    out.append(MonitorInfo(
                        index=len(out) + 1, name=str(mi.szDevice),
                        primary=bool(mi.dwFlags & w.MONITORINFOF_PRIMARY),
                        rect=rect, work=work, dpi=dpi, scale=round(dpi / 96.0, 4),
                    ))
            except Exception:  # noqa: BLE001 - 枚举回调内绝不允许抛穿到系统层
                pass
            return True

        cb = w.MONITORENUMPROC(_cb)
        try:
            w.EnumDisplayMonitors(None, None, cb, 0)
        except Exception:  # noqa: BLE001
            pass
        return out

    def monitors(self) -> list[MonitorInfo]:
        return self._monitors()

    def graphics_available(self) -> bool:
        # 能打开输入桌面即视为有可交互图形会话（RP-1001 / 场景 11）
        h = w.OpenInputDesktop(0, False, w.DESKTOP_SWITCHDESKTOP)
        if not h:
            return False
        w.CloseDesktop(h)
        return True

    def _desktop_name(self) -> str:
        h = w.OpenInputDesktop(0, False, w.DESKTOP_SWITCHDESKTOP)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(64)
            needed = w.wt.UINT(0)
            if w.GetUserObjectInformationW(h, w.UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
                return buf.value or ""
            return ""
        finally:
            w.CloseDesktop(h)

    def desktop_name(self) -> str:
        return self._desktop_name()

    def is_elevated(self) -> bool:
        if w.OpenProcessToken is None or w.GetTokenInformation is None:
            return False
        tok = w.wt.HANDLE()
        proc = w.GetCurrentProcess()
        if not w.OpenProcessToken(proc, w.TOKEN_QUERY, ctypes.byref(tok)):
            return False
        try:
            needed = w.wt.DWORD(0)
            w.GetTokenInformation(tok, w.TokenElevation, None, 0, ctypes.byref(needed))
            if not needed.value:
                return False
            buf = ctypes.create_string_buffer(int(needed.value))
            got = w.wt.DWORD(0)
            if w.GetTokenInformation(tok, w.TokenElevation, buf, needed, ctypes.byref(got)):
                return int.from_bytes(buf.raw[:4], "little") != 0
            return False
        finally:
            w.CloseHandle(tok)

    def apply_dpi_awareness(self) -> None:
        # ADR-04：物理坐标为准。进程须声明 PER_MONITOR_AWARE_V2(-4)，
        # 否则钩子/UIA 拿到的是被 DPI 虚拟化的假坐标。重复设置返回失败，无副作用。
        try:
            w.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:  # noqa: BLE001
            pass


class MutexInputLease(InputLease):
    """命名互斥体实现的输入租约（INV-P1：全局同一时刻仅一个持有者）。"""

    def __init__(self, name: str = "Global\\andy_repeat_input") -> None:
        self._name = name
        self._handle: object | None = None
        self._pid = os.getpid()

    def acquire(self) -> tuple[bool, int]:
        if self._handle is not None:
            return True, self._pid
        try:
            h = w.CreateMutexW(None, False, self._name)
        except Exception:  # noqa: BLE001
            return False, 0
        if not h:
            return False, 0
        if w.GetLastError() == ERROR_ALREADY_EXISTS:
            w.CloseHandle(h)
            return False, 0
        self._handle = h
        return True, self._pid

    def release(self) -> None:
        if self._handle is not None:
            try:
                w.ReleaseMutex(self._handle)
            except Exception:  # noqa: BLE001
                pass
            try:
                w.CloseHandle(self._handle)
            except Exception:  # noqa: BLE001
                pass
            self._handle = None

    def held(self) -> bool:
        return self._handle is not None


def idle_seconds() -> float:
    """距最后一次真实输入的空闲秒数（执行前"请勿动鼠标"提示的辅助信号）。"""
    info = w.LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not w.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    tick = w.GetTickCount()
    return float(max(0.0, (int(tick) - info.dwTime) / 1000.0))


def now_ms() -> int:
    return int(time.time() * 1000)
