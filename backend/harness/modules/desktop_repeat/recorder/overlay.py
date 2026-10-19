"""tkinter 录制覆盖层（§3.7）：倒计时、状态胶囊、结果横幅、丢弃确认。

自治原则（§9.5.4 规则 4）：backend 崩溃时结果横幅仍由本进程呈现。
窗口形态：三个独立的小尺寸置顶窗（倒计时/胶囊/横幅），全部点击穿透
（WS_EX_TRANSPARENT|WS_EX_LAYERED|WS_EX_NOACTIVATE）——不吞用户事件，
倒计时期间用户的操作不被拦截、也未被开始记录（T05 前钩子未装）。
tkinter 必须跑在自己的主线程（UI 线程，§2.2）。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

_QUEUE: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=256)
_STOP = threading.Event()

# 供服务/其他线程查询 UI 是否就绪
READY = threading.Event()


def post(kind: str, **fields: Any) -> None:
    """线程安全 UI 指令（任意线程 → UI 线程）。投递失败静默（提示永不阻塞主流程）。"""
    try:
        _QUEUE.put_nowait((kind, fields))
    except Exception:  # noqa: BLE001
        pass


def stop() -> None:
    """请求 UI 线程退出（横幅落定后由 runner 调用）。"""
    post("stop")


def _make_toplevel(root: Any, w: int, h: int, x: int, y: int) -> Any:
    win = root.Toplevel() if hasattr(root, "Toplevel") else _tk.Toplevel()
    win.overrideredirect(True)
    win.geometry(f"{w}x{h}+{x}+{y}")
    win.attributes("-topmost", True)
    win.configure(bg="#101012")
    try:
        import ctypes
        hwnd = win.winfo_id()
        GWL_EXSTYLE = -20  # noqa: N806
        ex = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE, ex | 0x00080000 | 0x00000020 | 0x08000000)
    except Exception:  # noqa: BLE001
        pass
    return win


_tk: Any = None


def run_overlay(countdown_s: int) -> None:
    """UI 主循环（子进程 UI 线程阻塞于此，直到 stop()）。"""
    global _tk
    try:
        import tkinter as tk
        import tkinter.font as tkfont
    except Exception:  # noqa: BLE001 - 无 tkinter：静默退化，录制不受影响
        READY.set()
        _STOP.wait(timeout=3600)
        return
    _tk = tk
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    f_big = tkfont.Font(family="Segoe UI", size=72, weight="bold")
    f_pill = tkfont.Font(family="Segoe UI", size=10)
    f_banner = tkfont.Font(family="Segoe UI", size=11)

    # 倒计时窗（屏幕中央）
    cd = _make_toplevel(root, 160, 140, sw // 2 - 80, sh // 2 - 70)
    cd_l = tk.Label(cd, text="", font=f_big, fg="white", bg="#101012")
    cd_l.pack(expand=True)
    if countdown_s <= 0:
        cd.withdraw()

    # 状态胶囊（右上角）
    pill_w, pill_h = 300, 30
    pill = _make_toplevel(root, pill_w, pill_h, sw - pill_w - 14, 14)
    pill_l = tk.Label(pill, text="● 准备中", font=f_pill, fg="white", bg="#b00020")
    pill_l.pack(fill="both", expand=True)
    pill.withdraw()

    # 结果横幅（底部中央）
    ban_w, ban_h = 640, 88
    ban = _make_toplevel(root, ban_w, ban_h, sw // 2 - ban_w // 2, sh - ban_h - 60)
    ban_l = tk.Label(ban, text="", font=f_banner, fg="white", bg="#101012",
                     justify="left", anchor="w", wraplength=ban_w - 24)
    ban_l.pack(fill="both", expand=True)
    ban.withdraw()

    READY.set()
    # 倒计时按挂钟计算：历史实现每个 150ms tick 把 cd 减 1，"3-2-1"实际只有
    # ~0.45 秒，用户来不及把目标窗口摆正（体检 P1-2）。起点与 runner 主线程的
    # _stop.wait(cd+0.2) 几乎同时（READY 在 state 构造前几毫秒置位），
    # 0.2s 余量吸收 UI 线程调度抖动。
    cd_total = max(0, int(countdown_s))
    state: dict[str, Any] = {"cd_end": (time.monotonic() + cd_total) if cd_total > 0 else 0.0,
             "cd_total": cd_total, "paused": False, "hidden_at": 0.0, "pill": None}

    def _drain() -> None:
        try:
            while True:
                kind, f = _QUEUE.get_nowait()
                _apply(kind, f)
        except queue.Empty:
            pass

    def _apply(kind: str, f: dict[str, Any]) -> None:
        if kind == "tick":
            state["pill"] = f
        elif kind == "pill":
            # runner._heartbeat 每 500ms 投递 ("pill", pill_text=...)。旧版没有
            # 这个分支：状态胶囊永远停在初始"● 准备中"，用户看不到已录事件数
            # （体检 P1-2b）。
            state["pill"] = {"text": str(f.get("pill_text", ""))}
        elif kind == "pause":
            state["paused"] = bool(f.get("paused"))
        elif kind == "cd_done":
            state["cd_end"] = 0
            cd.withdraw()
            pill.deiconify()
        elif kind == "banner":
            ban_l.configure(text=str(f.get("text", ""))[:400],
                            bg="#5a1d1d" if f.get("error") else "#101012")
            ban.deiconify()
            state["hidden_at"] = time.monotonic() + float(f.get("stay_s", 5))
        elif kind == "stop":
            _STOP.set()

    def _loop() -> None:
        if _STOP.is_set():
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass
            return
        _drain()
        # 挂钟倒计时：显示 ceil(剩余秒)，到点立即收束胶囊。旧版每 150ms tick
        # 减 1，"3-2-1"实际只持续约 0.45 秒（体检 P1-2）。
        left = state["cd_end"] - time.monotonic()
        if left > 0:
            cd_l.configure(text=str(min(state["cd_total"], int(left) + 1)))
        elif state["cd_end"] > 0:
            state["cd_end"] = 0
            cd.withdraw()
            pill.deiconify()
        elif not pill.winfo_viewable() and state["cd_total"] == 0:
            pill.deiconify()
            cd.withdraw()
        pf = state.get("pill") or {}
        if pf:
            color = "#c77700" if state["paused"] else "#b00020"
            pill_l.configure(text=pf.get("text", "")[:80], bg=color)
        if state["hidden_at"] and time.monotonic() > state["hidden_at"]:
            ban.withdraw()
            state["hidden_at"] = 0.0
        root.after(150, _loop)

    root.after(80, _loop)
    try:
        root.mainloop()
    except Exception:  # noqa: BLE001
        pass
