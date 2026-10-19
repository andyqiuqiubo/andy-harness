"""录制器子进程入口（§2.2 / §5.1）。

用法：``python -m harness.modules.desktop_repeat.recorder.runner --bundle <session_dir>``
bundle/runtime/bootstrap.json 由服务层预写（session_id/config/record/env 快照）。

线程模型：
- 主线程：心跳写（500ms）+ 状态驱动 + 收尾封版
- 钩子线程：pynput 原生监听线程（回调只入队，§3.2 纪律）
- worker 线程：窗口上下文 → UIA 快照（硬超时）→ 关键帧/锚点 → 归一化 → 写盘
- UI 线程：tkinter 覆盖层（§3.7，自治呈现结果横幅）

状态机（内存态经 heartbeat 暴露；session.json 持久态 draft→ready/failed/discarded）：
arming → countdown → recording ⇄ paused → finalizing → ready/failed
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # 允许脚本直跑
    _ROOT = Path(__file__).resolve().parents[4]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

from harness.modules.desktop_repeat.config import RecordConfig, RepeatConfig
from harness.modules.desktop_repeat.coords import monitor_at_index, to_rel
from harness.modules.desktop_repeat.errors import RepeatError
from harness.modules.desktop_repeat.models import Env, Pos, RecEvent, UiaSnapshot, WindowRef
from harness.modules.desktop_repeat.platform import (
    make_capture,
    make_grabber,
    make_inspector,
    make_windows,
)
from harness.modules.desktop_repeat.recorder import overlay
from harness.modules.desktop_repeat.recorder.frames import (
    anchor_from_crop,
    crop_rect_for,
    diff_ratio,
)
from harness.modules.desktop_repeat.recorder.normalizer import Normalizer, Stub
from harness.modules.desktop_repeat.recorder.privacy import PrivacyGate, sanitize_text
from harness.modules.desktop_repeat.store.bundle import BundleLock, BundleWriter

CONTROL_POLL = 0.15
HEARTBEAT_POLL = 0.5
POST_FRAME_MS = 400
PAUSE_BACKDATED_NS = 150_000_000     # §3.10：热键生效前 ≤150ms 已入队事件一并丢
_PROBE_TYPES = {"click", "double_click", "right_click", "middle_click", "drag", "scroll"}


class Recorder:
    def __init__(self, session_dir: Path) -> None:
        self.dir = Path(session_dir)
        boot = json.loads((self.dir / "runtime" / "bootstrap.json").read_text(encoding="utf-8"))
        self.session_id: str = boot["session_id"]
        self.cfg = RepeatConfig.load(boot.get("config") or {})
        self.rec = RecordConfig.from_dict(boot.get("record") or {})
        self.env = Env.from_dict(boot.get("env"))

        self.lock = BundleLock(self.dir)
        if not self.lock.acquire():
            _log("bundle 锁被占用（INV-R4 单写者），退出")
            sys.exit(3)

        # self.dir 即本会话目录（服务层 --bundle 传入）。BundleWriter 第一参数是
        # sessions 根目录（内部再拼 session_id），故传 parent，避免 r-xxx/r-xxx/ 双层嵌套。
        self.writer = BundleWriter(self.dir.parent, self.session_id, self.cfg, self.rec, self.env)
        self.writer.set_label(self.rec.label)
        self.writer.write_draft()
        self.tier = str(self.writer._merged_cfg.get("frame_tier", "standard"))

        self.privacy = PrivacyGate(
            list(self.writer._merged_cfg.get("mask_windows") or []),
            list(self.writer._merged_cfg.get("mask_keys") or []),
            list(self.writer._merged_cfg.get("auto_pause_mask") or []),
            bool(self.writer._merged_cfg.get("capture_clipboard")),
        )

        self.capture = make_capture()
        self.inspector = make_inspector()
        self.windows = make_windows()
        self._grabber: object | None = None

        self.normalizer = Normalizer(key_flusher=self._key_flush,
                                     hotkeys=dict(self.cfg.hotkeys),
                                     on_segment_start=self._on_segment_start)
        self.normalizer.mask_keys = list(self.writer._merged_cfg.get("mask_keys") or [])

        self.state = "idle"
        self._stop = threading.Event()
        self._epoch0 = time.time_ns()
        # 会话 t=0 必须对齐 perf_counter_ns()——钩子/worker 的全部打点（capture 的
        # t_ns、_write_stub/心跳/暂停回溯窗）都在这个时钟域里。历史 bug：这里取了
        # epoch 值，t_ns - _mono0 恒为巨大负数被 max(0,...) 钳成 0，导致每条事件
        # t 恒 0、ts 完全错误，S5 等待推断与暂停扣除随之全部失效（体检 P0-1）。
        self._mono0 = time.perf_counter_ns()
        self._last_fg: WindowRef | None = None
        self._elev_warned: set[int] = set()
        self._discard_pending_mono = 0.0
        self._drops = 0
        self._uia_timeouts = 0
        self._frame_drops = 0
        self._hook_lag_high = 0
        self._auto_paused = False
        self._seg_v_before: str | None = None
        self._seg_snap: UiaSnapshot | None = None
        self._stop_reason = "api"
        # 丢弃标记：discard 是终态，收尾时**不得**再走 finalize（否则 session.json
        # 会被覆写成 ready，用户明确丢弃的录制反而被保存——真机验证坐实）。
        self._discarded = False
        # worker 线程异常：必须显式收口，不能静默丢事件
        self._worker_error = ""
        # 控制线程 → worker 线程的动作投递（normalizer 单线程所有权，§2.2）：
        # pause/resume 会读写键段缓冲，必须与 feed/tick 同线程，否则竞态撕裂。
        self._actions: queue.Queue[tuple[str, int, bool]] = queue.Queue()

    # ── 时间换算 ─────────────────────────────────────────────────
    def _t_of(self, t_ns: int) -> int:
        return max(0, int((t_ns - self._mono0) // 1_000_000))

    def _ts_of(self, t_ns: int) -> int:
        return int(self._epoch0 / 1e6 + (t_ns - self._mono0) / 1e6)

    # ── 主流程 ───────────────────────────────────────────────────
    def run(self) -> None:
        if not self.capture.available():
            self._fail("RP-7003", "pynput 未安装，录制不可用（pip install 'harness[repeat]'）")
            return
        # 进程级 DPI 声明（ADR-04 物理坐标真值）——主线程执行一次
        try:
            from harness.modules.desktop_repeat.platform.win32.env import WinEnvProvider
            WinEnvProvider().apply_dpi_awareness()
        except Exception as e:  # noqa: BLE001
            _log(f"DPI 声明失败（继续，坐标可能不准）: {e}")

        threading.Thread(target=self._ui_main, name="repeat-ui", daemon=True).start()
        overlay.READY.wait(5.0)
        self._set_state("arming")
        cd = int(self.writer._merged_cfg.get("countdown_s", self.cfg.countdown_s))
        # 倒计时期间钩子未安装：操作不被拦截也不被记录（§3.7）
        if cd > 0:
            self._stop.wait(cd + 0.2)
        if self._stop.is_set():
            self._quick_exit("countdown_abort")
            return
        try:
            # 不传 sink：原始事件进 capture 内部有界队列，worker 用 drain() 取（§3.2）
            self.capture.start()
        except Exception as e:  # noqa: BLE001
            self._fail("RP-7003", f"钩子安装失败: {e}")
            return
        try:
            self.inspector.warmup()
        except Exception:  # noqa: BLE001
            pass
        self._set_state("recording")
        self._emit_system("session_start", {
            "env": self.env.to_dict(),
            "config": {k: v for k, v in self.writer._merged_cfg.items() if not str(k).startswith("_")},
        })

        worker = threading.Thread(target=self._worker_loop, name="repeat-worker", daemon=True)
        worker.start()
        ctl = threading.Thread(target=self._control_loop, name="repeat-control", daemon=True)
        ctl.start()
        while not self._stop.is_set():
            self._heartbeat()
            self._stop.wait(HEARTBEAT_POLL)
        self._set_state("finalizing")
        self._stop.set()
        worker.join(timeout=10)
        ctl.join(timeout=3)
        try:
            self.capture.stop()
        except Exception:  # noqa: BLE001
            pass
        self._shutdown_final(self._stop_reason)

    def _ui_main(self) -> None:
        try:
            cd = int(self.writer._merged_cfg.get("countdown_s", self.cfg.countdown_s))
            overlay.run_overlay(cd)
        except Exception as e:  # noqa: BLE001
            _log(f"覆盖层异常: {e}")

    def _set_state(self, st: str) -> None:
        self.state = st
        try:
            self._heartbeat()
        except Exception:  # noqa: BLE001
            pass

    # ── worker ───────────────────────────────────────────────────
    def _grabber_for_thread(self) -> Any:
        if self._grabber is None:
            self._grabber = make_grabber()
        return self._grabber

    def _worker_loop(self) -> None:
        """worker 包装：异常必须显式收口。

        旧实现是裸线程，任何异常都会让采集线程静默退出——队列不再消费，
        用户继续操作却一条事件都写不进去，最后 session 仍然 ready，
        解析时才报"事件流不含可执行操作"（真机坐实：一个 NameError 就
        让整场录制只剩 3 条系统事件）。
        """
        try:
            self._worker_loop_inner()
        except BaseException as e:  # noqa: BLE001
            self._worker_error = f"{type(e).__name__}: {e}"
            _log(f"worker 异常退出: {self._worker_error}")
            self._stop.set()

    def _worker_loop_inner(self) -> None:
        while True:
            batch = self.capture.drain(64) if hasattr(self.capture, "drain") else []
            if batch:
                oldest = min(int(b.get("t_ns", 0) or 0) for b in batch)
                if (time.perf_counter_ns() - oldest) / 1e6 > 2000:
                    self._hook_lag_high += 1
                # 先喂输入（暂停前缓冲的键进段），再处理 pause/resume 动作：
                # 否则 pause 先置位 → 本批键被 _on_key 当"暂停期数据"丢弃、
                # enter_pause 只能冲刷到空段（真实丢键竞态）。
                for raw in batch:
                    if raw.get("kind") == "move":
                        self.normalizer.track_drag_move(int(raw.get("x", 0)), int(raw.get("y", 0)))
                    for stub in self.normalizer.feed(raw):
                        self._write_stub(stub)
                self._drain_actions()
                # 先结算键段，再检测窗口变化：按键（回车/提交）引发的页面切换，
                # 其 window_change 必须排在按键**之后**。旧顺序先 _detect_window_change
                # 再 tick，导致"回车引发的导航"被写成 window_change 在前、回车在后，
                # 编译期回车被绑到导航后的页面上（真机 s09 因果倒置死锁）。
                for stub in self.normalizer.tick():
                    self._write_stub(stub)
                self._detect_window_change()
                continue
            if self._stop.is_set():
                self._drain_actions()
                break
            self._drain_actions()
            for stub in self.normalizer.tick():
                self._write_stub(stub)
            self._detect_window_change()
            # 空闲：用队列超时等待替代固定 0.1s 轮询，pause/resume 近零延迟唤醒
            try:
                self._apply_action(self._actions.get(timeout=0.1))
            except queue.Empty:
                pass
        for stub in self.normalizer.force_flush():
            self._write_stub(stub)

    def _write_stub(self, stub: Stub) -> None:
        t_ns = time.perf_counter_ns()
        # 键/文本事件改用**按下时刻**（normalizer 记录的 _t_ms，perf_counter 域），
        # 而不是结算写盘时刻。键段要等 150ms 防抖 / 1200ms 静默才结算，worker 还
        # 可能被后帧采集阻塞数百毫秒～数秒；用写盘时刻会把"按下回车触发导航"的
        # 回车排到它自己引发的 window_change **之后**（真机 run-092d827a9a 坐实：
        # 回车 t=15867 晚于 window_change t=15758），编译期回车就被绑到"按键之后
        # 才出现的页面"上——回放时那个页面要等回车按下才存在，"先定位到元素才允许
        # 按键"永远失败（因果倒置死锁，s09 挂起等人工）。
        press_ms = stub.data.get("_t_ms")
        if isinstance(press_ms, (int, float)) and press_ms > 0:
            t_ns = int(float(press_ms) * 1e6)
        # 控制类 stub 先行处理（§5.1 副作用）
        if stub.type == "pause":
            self._handle_pause(stub, t_ns)
            return
        if stub.type == "resume":
            self._handle_resume(stub, t_ns)
            return
        if stub.type == "discard":
            self._handle_discard_hotkey(t_ns)
            return
        if stub.type == "toggle":
            self._stop_reason = "hotkey"
            self._stop.set()
            return
        ev = RecEvent(seq=-1, t=self._t_of(t_ns), ts=self._ts_of(t_ns),
                      type=stub.type, src=stub.src, flags=dict(stub.flags))
        stub.data.pop("_t_ms", None)
        ev.data = {k: v for k, v in stub.data.items() if not str(k).startswith("_")}
        # 键/文本事件的窗口上下文同样取"按下时刻"已知的前景（_last_fg），不在此
        # 重新查询前景：此刻页面往往已被这次按键导航到新标题，重新查询会把回车
        # 锚到"回车之后才出现的页面"上（同上死锁链）；且 _win_context 内部的写时
        # 检测会把 window_change 抢先发射到按键之前。指针类事件仍走 _win_context
        # （体检 P1-4：点击引发的窗口切换靠它补发）。
        if stub.type in ("key", "text_input") and self._last_fg is not None:
            win = self._last_fg
        else:
            win = self._win_context()
        if win is not None and self.privacy.mask_window(win):
            ev.flags["masked"] = True
            ev.data = {}
            ev.pos = self._pos_of(stub, win)
            self.writer.emit(ev)
            return
        ev.win = win
        ev.pos = self._pos_of(stub, win)
        # 取代关系（§3.3）：double_click 携带 supersedes_eid，编译期据此丢弃首次
        # click。eid 必须在此附着到 data——merge 层拿不到归一器的私有计数，历史
        # 实现让 seq 与 eid 隔空相等，任何前置系统事件（session_start/告警 marker）
        # 都会错位、导致被取代的 click 混进步骤序列（体检 P0-3）。
        ev.data = {**ev.data, "eid": stub.eid}
        if stub.type in _PROBE_TYPES:
            snap = self._probe_snapshot(stub, ev)
            self._attach_frames(ev, snap, stub)
        elif stub.type in ("key", "text_input"):
            # 优先用键段结算时附着的快照（见 _key_flush._tag）；_seg_snap 仅作兜底。
            if stub.uia is not None:
                ev.uia = stub.uia
            elif self._seg_snap is not None:
                ev.uia = self._seg_snap
        self.writer.emit(ev)

    def _pos_of(self, stub: Stub, win: WindowRef | None) -> Pos | None:
        if stub.x is None or stub.y is None:
            return None
        m = monitor_at_index(stub.x, stub.y, self.env.monitors)
        rel = to_rel(stub.x, stub.y, win.client) if win and win.client[2] > win.client[0] else None
        scale = self.env.monitors[m - 1].scale if 1 <= m <= len(self.env.monitors) else 1.0
        return Pos(x=int(stub.x), y=int(stub.y), monitor=m or 1, scale=scale, rel=rel)

    def _win_context(self) -> WindowRef | None:
        try:
            w = self.windows.foreground()
        except Exception:  # noqa: BLE001
            return self._last_fg
        if w is None:
            return self._last_fg
        # 体检 P1-4：旧实现这里"静默覆盖 _last_fg"，随后 _detect_window_change
        # 再比较时 prev 已被更新 → 点击引发的窗口切换永远录不到 window_change
        # （只有用户 Alt+Tab/点别的窗口才由轮询捕获）。改为先比较后更新：
        # 变化即在此补发（时间线上恰在当前事件之前），轮询路径仍兜其他来源。
        prev = self._last_fg
        self._last_fg = w
        if prev is not None and (prev.hwnd != w.hwnd or prev.title != w.title):
            self._emit_window_change(prev, w)
        return w

    def _emit_window_change(self, prev: WindowRef, curr: WindowRef) -> None:
        """window_change 事件发射（写时检测与轮询检测共用，§3.3）。"""
        t_ns = time.perf_counter_ns()
        ev = RecEvent(seq=-1, t=self._t_of(t_ns), ts=self._ts_of(t_ns),
                      type="window_change", src="derived",
                      data={"subtype": "focus",
                            "prev": {"title": prev.title, "exe": prev.exe},
                            "curr": {"title": curr.title, "exe": curr.exe,
                                     "rect": list(curr.rect)}})
        masked = self.privacy.mask_window(curr)
        if masked:
            ev.flags["masked"] = True
            ev.data = {}
        else:
            ev.win = curr
            if curr.elevated and curr.hwnd not in self._elev_warned:
                self._elev_warned.add(curr.hwnd)
                ev.flags["elevated"] = True
            if self.tier in ("standard", "forensic"):
                try:
                    data = (self._grabber_for_thread().grab(None) if self.tier == "forensic"
                            else safe_jpeg_full(self._grabber_for_thread()))
                    if data:
                        fname = f"{self.writer.next_frame_id():06d}-full"
                        fname += ".png" if self.tier == "forensic" else ".jpg"
                        saved = self.writer.save_frame(fname, data)
                        if saved:
                            ev.data["frames"] = [saved]
                except Exception:  # noqa: BLE001
                    self._frame_drops += 1
        self.writer.emit(ev)
        if not masked and self.state == "recording" and self.privacy.auto_pause(curr) and not self._auto_paused:
            self._do_pause(auto=True, t_ns=time.perf_counter_ns())

    def _probe_snapshot(self, stub: Stub, ev: RecEvent) -> UiaSnapshot | None:
        try:
            tok, snap = self.inspector.element_token_from_point(stub.x or 0, stub.y or 0,
                                                                self.cfg.uia_timeout_ms)
        except Exception:  # noqa: BLE001
            tok, snap = None, None
        if tok is not None:
            self.inspector.release(tok)
        if snap is None:
            ev.flags["uia_timeout"] = True
            self._uia_timeouts += 1
            return None
        # 坐标一致性校验：Chrome 切标签/切页后 UIA 树会滞后几百毫秒，
        # ElementFromPoint 可能返回**上一个页面**的控件（真机实录：点击百度
        # 搜索框，快照却是 Andy-Harness 的聊天框 chat-textarea，回放时在
        # 百度窗口里找不到、或误命中同名控件）。取到的元素矩形不包含点击点
        # 就视为脏快照，降级为图像/坐标定位。
        if not _rect_contains_point(snap.rect, stub.x or 0, stub.y or 0):
            ev.flags["uia_stale"] = True
            self._uia_timeouts += 1
            return None
        win = self._last_fg
        if win is not None and win.elevated and win.hwnd not in self._elev_warned:
            self._elev_warned.add(win.hwnd)
            self._emit_system("marker", {"warn": "elevated",
                                         "note": f"目标 {win.title or win.exe} 以管理员权限运行，可能无法回放"})
        if snap.is_password:
            ev.flags["masked"] = True
            cleaned = UiaSnapshot(control_type=snap.control_type, is_password=True,
                                  rect=snap.rect, patterns=list(snap.patterns),
                                  is_enabled=snap.is_enabled)
            ev.uia = cleaned
            return cleaned
        snap.value = ""
        ev.uia = snap
        return snap

    def _attach_frames(self, ev: RecEvent, snap: UiaSnapshot | None, stub: Stub) -> None:
        """关键帧 + 图像锚点（§3.6 三档）。预算 60ms/帧，超限跳帧计数。"""
        fid = self.writer.next_frame_id()  # 帧文件名专用唯一 id（事件 seq 在写时才分配，此处不可用）
        rect = (crop_rect_for(snap.rect, (stub.x or 0, stub.y or 0), _virtual(self.env))
                if (snap and snap.rect[2] > snap.rect[0])
                else (max((stub.x or 0) - 96, 0), max((stub.y or 0) - 72, 0),
                      (stub.x or 0) + 96, (stub.y or 0) + 72))
        g = self._grabber_for_thread()
        try:
            pre = g.grab(rect)  # PNG（锚点源，任意档都要内存里过一遍）
        except Exception:  # noqa: BLE001
            self._frame_drops += 1
            return
        frames: list[str] = []
        anchor_rel = ""
        if snap and snap.rect[2] - snap.rect[0] >= 40 and snap.rect[3] - snap.rect[1] >= 24:
            a = anchor_from_crop(pre)
            if a:
                anchor_rel = f"anchors/prov_{fid}.png"
                p = self.writer.dir / anchor_rel
                p.parent.mkdir(exist_ok=True)
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_bytes(a)
                os.replace(tmp, p)
        if self.tier in ("standard", "forensic"):
            data = pre if self.tier == "forensic" else safe_jpeg(g, rect)
            name = f"{fid:06d}-pre.{'png' if self.tier == 'forensic' else 'jpg'}"
            saved = self.writer.save_frame(name, data) if data else None
            if saved:
                frames.append(saved)
        if self.tier == "forensic":
            try:
                full = g.grab(None)
                saved = self.writer.save_frame(f"{fid:06d}-full-pre.png", full)
                if saved:
                    frames.append(saved)
            except Exception:  # noqa: BLE001
                self._frame_drops += 1
        self._stop.wait(POST_FRAME_MS / 1000.0)
        if self._stop.is_set():
            ev.data["frames"] = frames
            if anchor_rel:
                ev.data["anchor_prov"] = anchor_rel
            return
        try:
            post = g.grab(rect)
        except Exception:  # noqa: BLE001
            self._frame_drops += 1
            post = b""
        if post:
            try:
                ev.data["post_diff"] = round(diff_ratio(pre, post), 4)
            except Exception:  # noqa: BLE001
                pass
            if self.tier in ("standard", "forensic"):
                data = post if self.tier == "forensic" else safe_jpeg(g, rect)
                name = f"{fid:06d}-post.{'png' if self.tier == 'forensic' else 'jpg'}"
                if data:
                    saved = self.writer.save_frame(name, data)
                    if saved:
                        frames.append(saved)
            if self.tier == "forensic":
                try:
                    full = g.grab(None)
                    saved = self.writer.save_frame(f"{fid:06d}-full-post.png", full)
                    if saved:
                        frames.append(saved)
                except Exception:  # noqa: BLE001
                    self._frame_drops += 1
        ev.data["frames"] = frames
        if anchor_rel:
            ev.data["anchor_prov"] = anchor_rel

    # ── 键段：v_before 采样 + IME 差分（§3.4） ────────────────────
    @staticmethod
    def _rect_within(inner: tuple[int, int, int, int] | None,
                     outer: tuple[int, int, int, int] | None) -> bool:
        """inner 的中心是否落在 outer 内（矩形未知时视为通过）。"""
        if not inner or not outer:
            return True
        ix, iy, iw, ih = inner
        ox, oy, ow, oh = outer
        if ow <= 0 or oh <= 0:
            return True
        cx, cy = ix + iw // 2, iy + ih // 2
        return ox <= cx <= ox + ow and oy <= cy <= oy + oh

    def _on_segment_start(self, focus_hwnd: int = 0) -> None:
        """键段起点：取焦点控件快照（IME 差分 + 键入步骤定位锚点）。

        focus_hwnd 由钩子回调在**输入发生瞬间**采集（GetGUIThreadInfo，廉价），
        因此即使 worker 被后帧采集阻塞数百毫秒，锚点仍指向用户当时真正输入的控件。
        """
        tok = snap = None
        if focus_hwnd:
            fn = getattr(self.inspector, "element_token_from_handle", None)
            if fn is not None:
                tok, snap = fn(int(focus_hwnd), timeout_ms=400)
        if snap is None:
            tok, snap = self.inspector.focused_element(timeout_ms=400)
        elif not snap.is_editable and not snap.is_password:
            # Chrome / Electron 等单 HWND 应用：控件句柄只对应顶层窗口，按句柄取到
            # 的是窗口根 Pane（录制实录：键入 8 个字符全部锚到
            # "Pane[百度一下… - Google Chrome]"，回放时文字落不进搜索框）。
            # 此时退回 UIA 焦点元素，并用矩形包含关系排除"worker 滞后锚到下一个
            # 控件"的旧问题。
            tok2, snap2 = self.inspector.focused_element(timeout_ms=400)
            if (snap2 is not None and (snap2.is_editable or snap2.is_password)
                    and self._rect_within(snap2.rect, snap.rect)):
                if tok is not None:
                    self.inspector.release(tok)
                tok, snap = tok2, snap2
            elif tok2 is not None:
                self.inspector.release(tok2)
        self._seg_snap = None
        self._seg_v_before = None
        if snap is None:
            return
        if snap.is_password:
            snap.value = ""
            self._seg_snap = snap
            return
        v = self.inspector.read_value(tok, timeout_ms=300) if tok is not None else None
        self.inspector.release(tok) if tok is not None else None
        self._seg_v_before = v
        clean = UiaSnapshot.from_dict(snap.to_dict())
        if clean is not None:
            clean.value = ""
        self._seg_snap = clean

    def _key_flush(self, segment: list[Stub], reason: str) -> list[Stub]:
        if not segment:
            return []
        typed = "".join(str(s.data.get("key", "")) for s in segment
                        if len(str(s.data.get("key", ""))) == 1)
        snap = self._seg_snap
        # 段内最后一个键的按下时刻：合成 stub（密码占位 / IME 上屏文本）本身不带
        # _t_ms，必须继承段内真实按下时刻，否则落盘会用结算写盘时刻，时间轴与
        # 窗口上下文错位到"按键引发的页面切换之后"（真机 s09 因果倒置死锁）。
        seg_t = float(segment[-1].data.get("_t_ms", 0) or 0) if segment else 0.0

        def _tag(stubs: list[Stub]) -> list[Stub]:
            """把本段的焦点控件快照附着到事件上（结算后 _seg_snap 会被重置）。"""
            if snap is not None:
                for s in stubs:
                    s.uia = snap
            if seg_t > 0:
                for s in stubs:
                    s.data.setdefault("_t_ms", seg_t)
            return stubs

        if snap is not None and snap.is_password:
            s = self.normalizer.make_stub("key", src="derived",
                                          data={"key": "<masked>", "count": len(segment),
                                                "password": True, "modifiers": []})
            s.flags["masked"] = True
            return _tag([s])
        v_now = None
        if snap is not None and snap.is_editable:
            tok, cur = self.inspector.focused_element(timeout_ms=300)
            if cur is not None:
                v_now = self.inspector.read_value(tok, timeout_ms=300) if tok is not None else None
            self.inspector.release(tok) if tok is not None else None
        v_prev = self._seg_v_before
        appended: str | None = None
        if v_prev is not None and v_now is not None:
            if v_now.startswith(v_prev):
                appended = v_now[len(v_prev):]
            elif len(v_now) > len(v_prev) and v_now.endswith(typed[-1:] or "\x00"):
                appended = v_now[len(v_prev):]
        if appended is None and snap is not None and snap.is_editable and typed:
            for s in segment:
                s.flags["ime_risk"] = True
            self._reset_seg()
            return _tag(list(segment))
        if appended and appended != typed and any(ord(c) > 127 for c in appended):
            # IME 合成上屏：文本入库、原始键丢弃（consumed_by_text 等价实现）
            clean_text = sanitize_text(appended)[:2048]
            s = self.normalizer.make_stub("text_input", src="derived",
                                          data={"text": clean_text, "source": "ime",
                                                "len_delta": len(clean_text),
                                                "ctrl_type": snap.control_type if snap else ""})
            self._reset_seg()
            return _tag([s])
        self._reset_seg()
        return _tag(list(segment))

    def _reset_seg(self) -> None:
        self._seg_v_before = None
        self._seg_snap = None

    # ── 窗口变化（§3.3 window_change） ───────────────────────────
    def _detect_window_change(self) -> None:
        # 轮询兜底路径（用户 Alt+Tab、点别的窗口等无事件触发的切换）。
        # 点击引发的切换已由 _win_context 写时检测（体检 P1-4），两条路径
        # 共用 _emit_window_change，逻辑不再漂移。
        try:
            fg = self.windows.foreground()
        except Exception:  # noqa: BLE001
            return
        if fg is None:
            return
        prev = self._last_fg
        if prev is not None and (prev.hwnd != fg.hwnd or prev.title != fg.title):
            self._last_fg = fg
            self._emit_window_change(prev, fg)
        else:
            self._last_fg = fg

    # ── 暂停/恢复/丢弃 ───────────────────────────────────────────
    def _handle_pause(self, stub: Stub, t_ns: int) -> None:
        if self.state != "recording":
            return
        self._do_pause(auto=False, t_ns=t_ns, long_hold=bool(stub.data.get("long")))

    def _apply_action(self, action: tuple[str, int, bool]) -> None:
        kind, t_ns, long_hold = action
        if kind == "pause":
            self._do_pause(auto=False, t_ns=t_ns, long_hold=long_hold)
        elif kind == "resume":
            self._do_resume(t_ns)

    def _drain_actions(self) -> None:
        """worker 线程消费控制线程投递的 pause/resume 动作，保证 normalizer
        单线程所有权（§2.2）：这些动作会读写键段缓冲，不能与控制线程并发。"""
        while True:
            try:
                self._apply_action(self._actions.get_nowait())
            except queue.Empty:
                return

    def _do_pause(self, *, auto: bool, t_ns: int, long_hold: bool = False) -> None:
        self._set_state("paused")
        # §3.10 正确顺序：暂停信号到达时，capture 队列里可能还压着「暂停前」刚
        # 输入、尚未喂入 normalizer 的键（worker 每 ~100ms 才排空一批）。必须先把
        # 这些早于回溯窗的积压喂进键段，再冲刷入库，最后才丢弃窗内/暂停期数据。
        # 旧实现在 enter_pause 之后才 bulk-drain 全丢，导致「打字→500ms→暂停」的
        # 合法演示输入被整批吞掉（阶段一 E2E run4 坐实）。
        cutoff = t_ns - PAUSE_BACKDATED_NS
        kept = 0
        try:
            backlog = self.capture.drain(4096)
        except Exception:  # noqa: BLE001
            backlog = []
        for raw in backlog:
            if int(raw.get("t_ns", 0) or 0) <= cutoff:
                kept += 1
                if raw.get("kind") == "move":
                    self.normalizer.track_drag_move(int(raw.get("x", 0)), int(raw.get("y", 0)))
                # 这些是「暂停前」数据：临时解除 paused 让 _on_key 正常入段
                # （区间保证：仍早于窗口的事件本就该记录；paused 短路只护窗口内）
                self.normalizer.paused = False
                for stub in self.normalizer.feed(raw):
                    self._write_stub(stub)
                self.normalizer.paused = True
        _log(f"[pause] backlog_kept={kept} backlog_total={len(backlog)} auto={auto}")
        pre_stubs = self.normalizer.enter_pause()
        for st in pre_stubs:
            self._write_stub(st)
        self._emit_system("pause", {"auto": auto, "long": long_hold})
        overlay.post("pause", paused=True)
        self._auto_paused = auto
        if auto:
            overlay.post("banner", text="已进入敏感窗口，自动暂停（按暂停热键恢复）", stay_s=4)

    def _handle_resume(self, stub: Stub, t_ns: int) -> None:
        self._do_resume(t_ns)

    def _do_resume(self, t_ns: int) -> None:
        if self.state != "paused":
            return
        self._set_state("recording")
        self.normalizer.exit_pause()
        self._emit_system("resume", {})
        self._auto_paused = False
        overlay.post("pause", paused=False)

    def _handle_discard_hotkey(self, t_ns: int) -> None:
        """热键丢弃二次确认（§3.7/§3.10）：3 秒内按两次才生效。"""
        now = time.perf_counter_ns()
        if now - self._discard_pending_mono < 3_000_000_000:
            self._discarded = True
            self._stop_reason = "discard"
            self._emit_system("discard", {"source": "hotkey"})
            self.writer.mark_discarded()
            overlay.post("banner", text="已丢弃本次录制（30 天内可于回收目录找回）",
                         stay_s=self.cfg.record_banner_s)
            self._stop.set()
            return
        self._discard_pending_mono = now
        overlay.post("banner", text="再按一次丢弃热键以确认丢弃本次录制（3 秒内）", stay_s=3)

    # ── 控制文件轮询（ADR-02） ───────────────────────────────────
    def _control_loop(self) -> None:
        last_rev = 0
        while not self._stop.is_set():
            ctrl = self.writer.read_control()
            rev = int(ctrl.get("_rev", 0) or 0)
            if rev and rev != last_rev:
                last_rev = rev
                cmd = str(ctrl.get("cmd", ""))
                sent_ns = self._mono0 + (float(ctrl.get("_sent", time.time() * 1000) or 0) * 1e6
                                         - self._epoch0)
                if cmd == "pause":
                    self._actions.put(("pause", int(sent_ns), False))
                elif cmd == "resume":
                    self._actions.put(("resume", int(sent_ns), False))
                elif cmd == "marker":
                    self._emit_system("marker",
                                      {"label": str(ctrl.get("label", ""))[:120],
                                       "note": str(ctrl.get("note", ""))[:500]}, src="marker")
                elif cmd == "discard":
                    self._discarded = True
                    self._stop_reason = "discard"
                    self._emit_system("discard", {"source": "api"})
                    self.writer.mark_discarded()
                    overlay.post("banner", text="已丢弃本次录制", stay_s=self.cfg.record_banner_s)
                    self._stop.set()
                elif cmd == "stop":
                    self._stop_reason = str(ctrl.get("reason", "api"))
                    self._stop.set()
            self._stop.wait(CONTROL_POLL)

    # ── 心跳/横幅刷新 ────────────────────────────────────────────
    def _heartbeat(self) -> None:
        try:
            qsize = self.capture.queue_size() if hasattr(self.capture, "queue_size") else 0
        except Exception:  # noqa: BLE001
            qsize = 0
        st: dict[str, int] = self.capture.stats() if hasattr(self.capture, "stats") else {}
        self.writer.write_heartbeat(self.state, events=self.writer.last_seq,
                                   drops=st.get("dropped", 0) + self._drops, qsize=qsize)
        dur = self._t_of(time.perf_counter_ns())
        overlay.post("pill", pill_text=(
            f"● 已录 {dur // 60000:02d}:{(dur // 1000) % 60:02d}｜{self.writer.last_seq} 事件"
            + ("｜已暂停" if self.state == "paused" else "")))

    def _emit_system(self, type_: str, data: dict[str, Any], src: str = "system") -> None:
        t_ns = time.perf_counter_ns()
        ev = RecEvent(seq=-1, t=self._t_of(t_ns), ts=self._ts_of(t_ns),
                      type=type_, src=src, data=data)
        self.writer.emit(ev)

    # ── 收尾 ─────────────────────────────────────────────────────
    def _quick_exit(self, reason: str) -> None:
        try:
            self.capture.stop()
        except Exception:  # noqa: BLE001
            pass
        self.writer.mark_discarded()
        overlay.post("banner", text="录制已取消", stay_s=2)
        self.lock.release()
        overlay.stop()

    def _shutdown_final(self, reason: str) -> None:
        if self._discarded:
            # 丢弃是终态：artifact 已由 mark_discarded() 落为 discarded + 回收标记，
            # 绝不能再 finalize（否则覆写 ready、生成清单，等于"丢弃失败"）。
            try:
                self.writer.close()
            except Exception:  # noqa: BLE001
                pass
            overlay.post("banner", text="已丢弃本次录制", stay_s=self.cfg.record_banner_s)
            self.lock.release()
            self._stop.wait(min(1.5, float(self.cfg.record_banner_s)))
            overlay.stop()
            try:
                self.inspector.shutdown()
            except Exception:  # noqa: BLE001
                pass
            sys.exit(0)
        self._emit_system("session_end", {"reason": reason, "counts": self.writer.counters()})
        self.writer.flush()
        res = self.writer.finalize(
            reason=reason,
            counters_extra={"uia_timeouts": self._uia_timeouts,
                            "frame_drops": self._frame_drops,
                            "drops": self._drops,
                            "hook_lag_high": self._hook_lag_high})
        st = res.get("status")
        if self._worker_error:
            # 采集线程崩溃：已录事件不完整，必须标 failed，不能报 ready
            try:
                sp = self.dir / "session.json"
                doc = json.loads(sp.read_text(encoding="utf-8"))
                doc["status"] = "failed"
                doc["error"] = {"code": "RP-7004",
                                "message": f"录制器内部异常：{self._worker_error}"}
                sp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
                st = "failed"
            except Exception:  # noqa: BLE001
                pass
        cnt = res.get("counters", {})
        label = self.rec.label or self.session_id
        if st == "ready":
            overlay.post("banner", text=(
                f"录制已保存：{label}｜事件 {cnt.get('events', 0)}｜帧 {cnt.get('frames', 0)}｜"
                f"{round((cnt.get('bytes', 0) or 0) / 1e6, 1)} MB"
                + ("｜末尾不完整已忽略" if res.get("truncated") else "")),
                stay_s=self.cfg.record_banner_s)
        else:
            overlay.post("banner", text=f"录制失败：{reason}（事件未通过完整性校验，原始数据已保留）",
                         error=True, stay_s=self.cfg.record_banner_s)
        self.lock.release()
        self._stop.wait(self.cfg.record_banner_s + 0.3)
        overlay.stop()
        try:
            self.inspector.shutdown()
        except Exception:  # noqa: BLE001
            pass
        if self._grabber is not None and hasattr(self._grabber, "close"):
            try:
                self._grabber.close()
            except Exception:  # noqa: BLE001
                pass
        sys.exit(0 if st == "ready" else 4)

    def _fail(self, code: str, msg: str) -> None:
        try:
            self.writer._write_session("failed", ended_at=_now_iso(), counters=self.writer.counters(),
                                       error={"code": code, "message": msg})
        except Exception:  # noqa: BLE001
            pass
        overlay.post("banner", text=f"录制失败：{msg}", error=True, stay_s=self.cfg.record_banner_s)
        self._stop.set()


def stub_long(stub: Stub) -> bool:
    try:
        return bool(stub.data.get("long"))
    except Exception:  # noqa: BLE001
        return False


def safe_jpeg(g: Any, rect: tuple[int, int, int, int]) -> bytes | None:
    try:
        if hasattr(g, "grab_jpeg"):
            return bytes(g.grab_jpeg(rect, quality=80))
        return bytes(g.grab(rect))
    except Exception:  # noqa: BLE001
        return None


def safe_jpeg_full(g: Any) -> bytes | None:
    try:
        if hasattr(g, "grab_jpeg"):
            return bytes(g.grab_jpeg(None, quality=80))
        return bytes(g.grab(None))
    except Exception:  # noqa: BLE001
        return None


def _monitor_or_default(x: int, y: int, env: Env) -> int:
    idx = monitor_at_index(x, y, env.monitors)
    return idx or (env.monitors[0].index if env.monitors else 1)


def _virtual(env: Env) -> tuple[int, int, int, int]:
    if env.virtual_screen and any(env.virtual_screen):
        return env.virtual_screen
    if env.monitors:
        m = env.monitors[0].rect
        return (m[0], m[1], m[0] + (m[2] - m[0]), m[1] + (m[3] - m[1]))
    return (0, 0, 1920, 1080)


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def _rect_contains_point(rect: tuple[int, int, int, int] | None,
                         x: int, y: int, pad: int = 2) -> bool:
    """元素矩形是否包含该点（l,t,r,b 语义；矩形无效时视为通过）。"""
    if not rect:
        return True
    l, t, r, b = (int(v) for v in rect)
    if r <= l or b <= t:
        return True
    return (l - pad) <= x <= (r + pad) and (t - pad) <= y <= (b + pad)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="repeat-recorder")
    ap.add_argument("--bundle", required=True, help="session 目录")
    args = ap.parse_args(argv)
    d = Path(args.bundle)
    try:
        rec = Recorder(d)
    except RepeatError as e:
        _log(f"初始化失败: {e}")
        return 3
    except Exception as e:  # noqa: BLE001
        _log(f"初始化异常: {e}")
        return 3
    try:
        rec.run()
    except KeyboardInterrupt:  # pragma: no cover
        rec._stop.set()
        try:
            rec._shutdown_final("interrupt")
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
