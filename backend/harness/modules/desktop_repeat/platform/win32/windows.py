"""窗口管理（win32）：枚举 / 查找（四级放宽，§7.3 场景 1）/ 前台激活（§场景 6）/ 探活。"""

from __future__ import annotations

import re
import time
from typing import Any

from ...models import WindowRef
from ..base import WindowManager
from . import winapi as w

_ENUM_BUF = 512


def _window_title(hwnd: Any) -> str:
    n = w.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = w.ctypes.create_unicode_buffer(min(n + 1, _ENUM_BUF))
    w.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value or ""


def _window_class(hwnd: Any) -> str:
    buf = w.ctypes.create_unicode_buffer(_ENUM_BUF)
    w.GetClassNameW(hwnd, buf, _ENUM_BUF)
    return buf.value or ""


def _pid_of(hwnd: Any) -> int:
    pid = w.wt.DWORD(0)
    w.GetWindowThreadProcessId(hwnd, w.ctypes.byref(pid))
    return int(pid.value)


def _exe_of(pid: int) -> str:
    if not pid:
        return ""
    h = w.OpenProcess(w.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = w.wt.DWORD(_ENUM_BUF)
        buf = w.ctypes.create_unicode_buffer(_ENUM_BUF)
        if w.QueryFullProcessImageNameW(h, 0, buf, w.ctypes.byref(size)):
            path = buf.value or ""
            # 隐私：只存 basename（§3.8）
            return path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        return ""
    except Exception:  # noqa: BLE001
        return ""
    finally:
        w.CloseHandle(h)


def _is_elevated_hwnd(hwnd: Any) -> bool:
    """best-effort：跨完整性级别查询失败即视为可能提权（保守告警，不误报为安全）。"""
    if w.OpenProcessToken is None:
        return False
    pid = _pid_of(hwnd)
    if not pid:
        return False
    h = w.OpenProcess(w.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        tok = w.wt.HANDLE()
        if not w.OpenProcessToken(h, w.TOKEN_QUERY, w.ctypes.byref(tok)):
            return False
        try:
            needed = w.wt.DWORD(0)
            w.GetTokenInformation(tok, w.TokenElevation, None, 0, w.ctypes.byref(needed))
            if not needed.value:
                return False
            buf = w.ctypes.create_string_buffer(int(needed.value))
            got = w.wt.DWORD(0)
            if w.GetTokenInformation(tok, w.TokenElevation, buf, needed, w.ctypes.byref(got)):
                return int.from_bytes(buf.raw[:4], "little") != 0
            return False
        finally:
            w.CloseHandle(tok)
    except Exception:  # noqa: BLE001
        return False
    finally:
        w.CloseHandle(h)


def _is_cloaked(hwnd: Any) -> bool:
    """DWM cloaked（UWP 挂起/其他虚拟桌面的近似信号）。"""
    try:
        dwm = w.ctypes.windll.dwmapi
        val = w.wt.DWORD(0)
        DWMWA_CLOAKED = 14  # noqa: N806
        # 参数类型宽松：dwmapi 未设 argtypes，传 byref 即可
        if dwm.DwmGetWindowAttribute(int(hwnd or 0), DWMWA_CLOAKED, w.ctypes.byref(val),
                                     w.ctypes.sizeof(val)) == 0:
            return val.value != 0
    except Exception:  # noqa: BLE001
        return False
    return False


def _rects(hwnd: Any) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    r = w.RECT()
    w.GetWindowRect(hwnd, w.ctypes.byref(r))
    outer = (r.left, r.top, r.right, r.bottom)
    c = w.RECT()
    w.GetClientRect(hwnd, w.ctypes.byref(c))
    tl, br = w.POINT(c.left, c.top), w.POINT(c.right, c.bottom)
    w.ClientToScreen(hwnd, w.ctypes.byref(tl))
    w.ClientToScreen(hwnd, w.ctypes.byref(br))
    return outer, (tl.x, tl.y, br.x, br.y)


def build_ref(hwnd: Any, title: str = "", cls: str = "", pid: int | None = None) -> WindowRef:
    outer, client = _rects(hwnd)
    p = _pid_of(hwnd) if pid is None else pid
    return WindowRef(
        hwnd=int(hwnd or 0), title=title or _window_title(hwnd), cls=cls or _window_class(hwnd),
        pid=p, exe=_exe_of(p), rect=outer, client=client,
        minimized=bool(w.IsIconic(hwnd)), elevated=_is_elevated_hwnd(hwnd), cloaked=_is_cloaked(hwnd),
    )


class WinWindowManager(WindowManager):
    def foreground(self) -> WindowRef | None:
        hwnd = w.GetForegroundWindow()
        if not hwnd:
            return None
        try:
            return build_ref(hwnd)
        except Exception:  # noqa: BLE001
            return None

    def window_at(self, x: int, y: int) -> WindowRef | None:
        pt = w.POINT(int(x), int(y))
        hwnd = w.WindowFromPoint(pt)
        if not hwnd:
            return None
        try:
            # WindowFromPoint 常给子窗口，取顶层框架窗口（根）
            top = w.GetAncestor(hwnd, 2)  # GA_ROOT
            return build_ref(top or hwnd)
        except Exception:  # noqa: BLE001
            return None

    def enumerate(self, pid: int | None = None) -> list[WindowRef]:
        out: list[WindowRef] = []

        def _cb(hwnd: Any, _lparam: int) -> bool:  # noqa: ANN401
            try:
                if not w.IsWindowVisible(hwnd):
                    return True
                if pid is not None and _pid_of(hwnd) != pid:
                    return True
                title = _window_title(hwnd)
                if not title:
                    return True
                out.append(build_ref(hwnd, title=title))
            except Exception:  # noqa: BLE001
                pass
            return True

        cb = w.WNDENUMPROC(_cb)
        w.EnumWindows(cb, 0)
        return out

    def find_window(self, hints: dict[str, Any]) -> WindowRef | None:
        """四级放宽匹配（§7.3 场景 1）：(pid,title)→(pid,class)→(exe,title)→(exe,class)。

        hints: {exe?, window_title_regex?, cls?, pid?}
        命中即返回；minimized 命中也返回（调用方决定 restore）。
        """
        exe = str(hints.get("exe", "") or "").lower()
        cls = str(hints.get("cls", "") or "")
        pid = int(hints.get("pid", 0) or 0)
        title_re = None
        pat = str(hints.get("window_title_regex", "") or hints.get("title", "") or "")
        if pat:
            try:
                title_re = re.compile(pat)
            except re.error:
                title_re = re.compile(re.escape(pat))

        candidates = self.enumerate(None)
        # 四级按优先序
        levels: list[tuple[bool, bool, bool, bool]] = [
            (True, bool(title_re), False, False),   # pid+title
            (True, False, bool(cls), False),        # pid+class
            (False, bool(title_re), False, exe != ""),  # exe+title
            (False, False, bool(cls), exe != ""),   # exe+class
        ]
        for use_pid, use_title, use_cls, use_exe in levels:
            if use_pid and not pid:
                # 没有 pid 时，pid 级匹配会退化成"标题随意的第一个窗口"，
                # 于是任何 app_hint 都可能命中一个完全无关的窗口，
                # 随后 _scope_ok 以 RP-4002 拒绝（真机坐实：s01 期望 explorer.exe，
                # 实际被设成枚举到的第一个窗口）。
                continue
            for ref in candidates:
                if use_pid and pid and ref.pid != pid:
                    continue
                if use_exe and exe and ref.exe != exe:
                    continue
                if use_title and title_re is not None:
                    if not (title_re.search(ref.title or "")):
                        continue
                if use_cls and cls and ref.cls != cls:
                    continue
                if not (use_title or use_cls):
                    continue
                return ref
        # 兜底：只有 pid/exe 时返回其任一可见顶层窗口
        if pid:
            for ref in candidates:
                if ref.pid == pid:
                    return ref
        if exe:
            for ref in candidates:
                if ref.exe == exe:
                    return ref
        return None

    def focus(self, win: WindowRef) -> bool:
        """前台激活（§场景 6 顺序：直接→AttachThreadInput+ALT→重试）。"""
        hwnd = win.hwnd
        if not hwnd:
            return False
        if win.minimized:
            self.restore(win)
        for attempt in range(3):
            if bool(w.SetForegroundWindow(hwnd)) and int(w.GetForegroundWindow() or 0) == hwnd:
                return True
            # ALT 技巧 + AttachThreadInput
            try:
                cur_tid = w.GetCurrentThreadId()
                tgt_tid = w.GetWindowThreadProcessId(hwnd, None)
                w.AttachThreadInput(cur_tid, tgt_tid, True)
                w.keybd_event(w.VK_MENU, 0, 0, None)
                w.keybd_event(w.VK_MENU, 0, 2, None)
                w.SetForegroundWindow(hwnd)
                w.BringWindowToTop(hwnd)
                w.AttachThreadInput(cur_tid, tgt_tid, False)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2)
            if int(w.GetForegroundWindow() or 0) == hwnd:
                return True
        return False

    def restore(self, win: WindowRef) -> None:
        if win.hwnd:
            try:
                w.ShowWindow(win.hwnd, w.SW_RESTORE)
            except Exception:  # noqa: BLE001
                pass

    def alive(self, hwnd: int, timeout_ms: int = 5000) -> bool:
        """应用探活：WM_NULL + SMTO_ABORTIFHUNG（§场景 9）。"""
        if not hwnd:
            return False
        try:
            result = w.wt.LPARAM(0)
            r = w.SendMessageTimeoutW(hwnd, w.WM_NULL, 0, 0, w.SMTO_ABORTIFHUNG,
                                      max(1, timeout_ms // 1000), w.ctypes.byref(result))
            return bool(r)
        except Exception:  # noqa: BLE001
            return True  # 探测通道异常不误杀（超时另有步骤级超时兜底）

    def is_enabled(self, hwnd: int) -> bool:
        """IsWindowEnabled：目标窗口是否被模态框禁用（§场景 8 轻量信号）。"""
        if not hwnd:
            return True
        try:
            return bool(w.IsWindowEnabled(hwnd))
        except Exception:  # noqa: BLE001
            return True

    def top_windows_of(self, pid: int) -> list[WindowRef]:
        return self.enumerate(pid)

    def title_of(self, win: WindowRef) -> str:
        try:
            return _window_title(win.hwnd)
        except Exception:  # noqa: BLE001
            return win.title
