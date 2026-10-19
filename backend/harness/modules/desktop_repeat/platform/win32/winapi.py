"""Win32 API 绑定（纯 ctypes，无第三方 win32 包依赖）。

集中定义本节用到的 user32 / kernel32 / shcore 结构体与函数签名，
其余 win32/* 模块从这里取用，避免签名重复与漂移。
模块同时 re-export ``ctypes`` 与 ``wt``(wintypes)，win32 子模块统一经
``w.ctypes`` / ``w.wt`` 使用，不再各自 import。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from typing import Any

# 供子模块使用（w.ctypes / w.wt）
__all__: list[str] = ["ctypes", "wt"]

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
try:
    shcore = ctypes.windll.shcore
except Exception:  # noqa: BLE001 - 老系统可能缺失
    shcore = None  # type: ignore[assignment]
try:
    wtsapi32 = ctypes.windll.wtsapi32
except Exception:  # noqa: BLE001
    wtsapi32 = None  # type: ignore[assignment]

# ── 常量 ────────────────────────────────────────────────────────
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CXSCREEN = 0
SM_CYSCREEN = 1

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120

# 窗口样式 / 扩展样式
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

# ShowWindow
SW_RESTORE = 9
SW_SHOW = 5

# GetWindowLong GWL_EXSTYLE
GWL_EXSTYLE = -20

WM_NULL = 0x0000
WM_CLOSE = 0x0010
SMTO_ABORTIFHUNG = 0x0002
SMTO_NORMAL = 0x0000

# AttachThreadInput / foreground tricks
VK_MENU = 0x12  # ALT
KEYBKDOWN = 0
KEYBKUP = 2

# 进程/令牌完整性
TOKEN_QUERY = 0x0008
TokenElevation = 20
TokenIntegrityLevel = 25

# ── 结构体 ──────────────────────────────────────────────────────
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("ii", _INPUTUNION)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wt.DWORD),
        ("szDevice", wt.WCHAR * 32),
    ]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


# 回调函数类型（EnumWindows / EnumDisplayMonitors）
WNDENUMPROC = ctypes.CFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
MONITORENUMPROC = ctypes.CFUNCTYPE(wt.BOOL, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(RECT), wt.LPARAM)


# ── 函数签名设定 ────────────────────────────────────────────────
def _sig(func: Any, restype: Any, argtypes: list[Any]) -> Any:
    func.restype = restype
    func.argtypes = argtypes
    return func


GetCursorPos = _sig(user32.GetCursorPos, wt.BOOL, [ctypes.POINTER(POINT)])
SetCursorPos = _sig(user32.SetCursorPos, wt.BOOL, [ctypes.c_int, ctypes.c_int])
SendInput = _sig(user32.SendInput, wt.UINT, [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int])
mouse_event = _sig(user32.mouse_event, None, [wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p])

GetForegroundWindow = _sig(user32.GetForegroundWindow, wt.HWND, [])
GetWindowThreadProcessId = _sig(user32.GetWindowThreadProcessId, wt.DWORD, [wt.HWND, ctypes.POINTER(wt.DWORD)])
GetWindowRect = _sig(user32.GetWindowRect, wt.BOOL, [wt.HWND, ctypes.POINTER(RECT)])
GetClientRect = _sig(user32.GetClientRect, wt.BOOL, [wt.HWND, ctypes.POINTER(RECT)])
ClientToScreen = _sig(user32.ClientToScreen, wt.BOOL, [wt.HWND, ctypes.POINTER(POINT)])
GetClassNameW = _sig(user32.GetClassNameW, ctypes.c_int, [wt.HWND, ctypes.c_wchar_p, ctypes.c_int])
GetWindowTextW = _sig(user32.GetWindowTextW, ctypes.c_int, [wt.HWND, ctypes.c_wchar_p, ctypes.c_int])
GetWindowTextLengthW = _sig(user32.GetWindowTextLengthW, ctypes.c_int, [wt.HWND])
IsWindowVisible = _sig(user32.IsWindowVisible, wt.BOOL, [wt.HWND])
IsWindowEnabled = _sig(user32.IsWindowEnabled, wt.BOOL, [wt.HWND])
IsIconic = _sig(user32.IsIconic, wt.BOOL, [wt.HWND])
SetForegroundWindow = _sig(user32.SetForegroundWindow, wt.BOOL, [wt.HWND])
BringWindowToTop = _sig(user32.BringWindowToTop, wt.BOOL, [wt.HWND])
ShowWindow = _sig(user32.ShowWindow, wt.BOOL, [wt.HWND, ctypes.c_int])
EnumWindows = _sig(user32.EnumWindows, wt.BOOL, [WNDENUMPROC, wt.LPARAM])
WindowFromPoint = _sig(user32.WindowFromPoint, wt.HWND, [POINT])
GetAncestor = _sig(user32.GetAncestor, wt.HWND, [wt.HWND, wt.UINT])
GetWindowLongW = _sig(user32.GetWindowLongW, wt.LONG, [wt.HWND, ctypes.c_int])
SendMessageTimeoutW = _sig(user32.SendMessageTimeoutW, ctypes.c_void_p,
                           [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT, ctypes.POINTER(wt.LPARAM)])
GetSystemMetrics = _sig(user32.GetSystemMetrics, ctypes.c_int, [ctypes.c_int])
AttachThreadInput = _sig(user32.AttachThreadInput, wt.BOOL, [wt.DWORD, wt.DWORD, wt.BOOL])
GetCurrentThreadId = _sig(kernel32.GetCurrentThreadId, wt.DWORD, [])
keybd_event = _sig(user32.keybd_event, None, [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_void_p])
MapVirtualKeyW = _sig(user32.MapVirtualKeyW, ctypes.c_uint, [ctypes.c_uint, ctypes.c_uint])
GetKeyState = _sig(user32.GetKeyState, ctypes.c_short, [ctypes.c_int])
GetLastInputInfo = _sig(user32.GetLastInputInfo, wt.BOOL, [ctypes.POINTER(LASTINPUTINFO)])
GetTickCount = _sig(kernel32.GetTickCount, wt.DWORD, [])
GetCurrentProcess = _sig(kernel32.GetCurrentProcess, ctypes.c_void_p, [])

EnumDisplayMonitors = _sig(user32.EnumDisplayMonitors, wt.BOOL,
                           [ctypes.c_void_p, ctypes.c_void_p, MONITORENUMPROC, wt.LPARAM])
GetMonitorInfoW = _sig(user32.GetMonitorInfoW, wt.BOOL, [ctypes.c_void_p, ctypes.POINTER(MONITORINFOEXW)])
MONITORINFOF_PRIMARY = 1

OpenInputDesktop = _sig(user32.OpenInputDesktop, wt.HANDLE, [wt.DWORD, wt.BOOL, wt.DWORD])
CloseDesktop = _sig(user32.CloseDesktop, wt.BOOL, [ctypes.c_void_p])
GetUserObjectInformationW = _sig(user32.GetUserObjectInformationW, wt.BOOL,
                                 [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                  ctypes.c_uint, ctypes.POINTER(wt.UINT)])
DESKTOP_SWITCHDESKTOP = 0x0100
UOI_NAME = 2

if shcore is not None:
    GetDpiForMonitor = _sig(shcore.GetDpiForMonitor, ctypes.c_long,
                            [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT)])
    SetProcessDpiAwarenessContext = _sig(
        user32.SetProcessDpiAwarenessContext, wt.BOOL, [ctypes.c_void_p])
else:
    GetDpiForMonitor = None
    SetProcessDpiAwarenessContext = _sig(
        user32.SetProcessDpiAwarenessContext, wt.BOOL, [ctypes.c_void_p])

GetDpiForSystem = getattr(user32, "GetDpiForSystem", None)

# 进程信息（QueryFullProcessImageNameW 需要 PROCESS_QUERY_LIMITED_INFORMATION=0x1000）
OpenProcess = _sig(kernel32.OpenProcess, wt.HANDLE, [wt.DWORD, wt.BOOL, wt.DWORD])
QueryFullProcessImageNameW = _sig(kernel32.QueryFullProcessImageNameW, wt.BOOL,
                                  [wt.HANDLE, wt.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wt.DWORD)])
CloseHandle = _sig(kernel32.CloseHandle, wt.BOOL, [wt.HANDLE])
CreateMutexW = _sig(kernel32.CreateMutexW, wt.HANDLE, [ctypes.c_void_p, wt.BOOL, ctypes.c_wchar_p])
ReleaseMutex = _sig(kernel32.ReleaseMutex, wt.BOOL, [wt.HANDLE])
GetLastError = _sig(kernel32.GetLastError, wt.DWORD, [])
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 令牌完整性 / 提权判断（advapi32）
try:
    advapi32 = ctypes.windll.advapi32
    OpenProcessToken = _sig(advapi32.OpenProcessToken, wt.BOOL,
                            [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)])
    GetTokenInformation = _sig(advapi32.GetTokenInformation, wt.BOOL,
                               [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)])
except Exception:  # noqa: BLE001
    OpenProcessToken = None
    GetTokenInformation = None

# WTSGetActiveConsoleSessionId 实为 kernel32 导出（并非 wtsapi32）
WTSGetActiveConsoleSessionId = _sig(kernel32.WTSGetActiveConsoleSessionId, wt.DWORD, [])
