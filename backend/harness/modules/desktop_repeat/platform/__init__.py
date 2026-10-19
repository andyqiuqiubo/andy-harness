"""L0 平台适配层（按 sys.platform 选择实现；本期仅 Windows）。"""

from __future__ import annotations

import sys

from .base import (
    EnvProvider,
    InputCapture,
    InputInjector,
    InputLease,
    RawInput,
    ScreenGrabber,
    UiInspector,
    WindowManager,
)


def _not_implemented() -> None:  # pragma: no cover - 非 Windows 占位
    raise NotImplementedError("andy-repeat 当前仅支持 Windows；macOS/Linux 适配见设计 §8.3")


def make_capture() -> InputCapture:
    if sys.platform == "win32":
        from .win32.capture import PynputCapture
        return PynputCapture()
    _not_implemented()


def make_inspector() -> UiInspector:
    if sys.platform == "win32":
        from .win32.uia import WinUiInspector
        return WinUiInspector()
    _not_implemented()


def make_grabber() -> ScreenGrabber:
    if sys.platform == "win32":
        from .win32.grab import MssGrabber
        return MssGrabber()
    _not_implemented()


def make_injector() -> InputInjector:
    if sys.platform == "win32":
        from .win32.inject import SendInputInjector
        return SendInputInjector()
    _not_implemented()


def make_windows() -> WindowManager:
    if sys.platform == "win32":
        from .win32.windows import WinWindowManager
        return WinWindowManager()
    _not_implemented()


def make_env() -> EnvProvider:
    if sys.platform == "win32":
        from .win32.env import WinEnvProvider
        return WinEnvProvider()
    _not_implemented()


def make_lease(name: str = "Global\\andy_repeat_input") -> InputLease:
    if sys.platform == "win32":
        from .win32.env import MutexInputLease
        return MutexInputLease(name)
    _not_implemented()


__all__ = [
    "EnvProvider", "InputCapture", "InputInjector", "InputLease", "RawInput",
    "ScreenGrabber", "UiInspector", "WindowManager",
    "make_capture", "make_inspector", "make_grabber", "make_injector",
    "make_windows", "make_env", "make_lease",
]
