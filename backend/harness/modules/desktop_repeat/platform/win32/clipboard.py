"""Win32 剪贴板（type 三通道之二：Ctrl+V 粘贴，自动恢复原内容，§6.3/ADR-09）。"""

from __future__ import annotations

import ctypes

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _check_open() -> bool:
    return bool(_user32.OpenClipboard(None))


def read_text() -> str | None:
    if not _check_open():
        return None
    try:
        h = _user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = _kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            _kernel32.GlobalUnlock(h)
    finally:
        _user32.CloseClipboard()


def write_text(text: str) -> bool:
    if not _check_open():
        return False
    try:
        _user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = _kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        _kernel32.GlobalUnlock(h)
        return bool(_user32.SetClipboardData(CF_UNICODETEXT, h))
    finally:
        _user32.CloseClipboard()


class ClipboardGuard:
    """粘贴前后保存/恢复原剪贴板（§6.3：自动恢复原剪贴板）。"""

    def __init__(self) -> None:
        self._saved: str | None = None
        self._captured = False

    def snapshot(self) -> None:
        self._saved = read_text()
        self._captured = True

    def restore(self) -> None:
        if self._captured and self._saved is not None:
            try:
                write_text(self._saved)
            except Exception:  # noqa: BLE001
                pass
