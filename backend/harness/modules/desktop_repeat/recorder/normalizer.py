"""归一化与事件合成（§3.2 worker 段 / §3.3 事件口径）。

纯逻辑：输入 pynput 原始事件（platform/win32/capture.py 的 RawInput dict），
输出"事件雏形"（Stub）。窗口上下文 / UIA 快照 / 关键帧由 runner 补齐。

实现口径说明（与设计 §3.3 的等价简化，VENDOR 级偏差全在此声明）：
- 原始 pointer_down/pointer_up 不单独入库——click/double_click/drag 直接在本层
  合成（编译期 S2 对该路径幂等）；
- 双击是追加事件：data.supersedes_eid 指向首次 click 的 eid；runner 写盘时把
  eid 附到 data.eid，编译 S2 据此丢弃被取代的首次 click；
- 键段（连续字符键）进缓冲；结算时机 = 非键事件 / 150ms 防抖（IME 差分采样）/
  静默 1200ms；IME 差分由注入的 key_flusher 决定输出 text_input 还是原始键
  （consumed_by_text 语义 = "被取代的键不入库"，append-only 下等价实现）；
- 滚轮 400ms 窗口累计为一条 scroll；
- 移动不逐条记录：语义事件前发一条"接近摘要"pointer_move（src=derived）；
- 控制热键（pause/resume/discard/marker）产出 system/marker 事件，runner 拦截
  pause/resume/discard 做状态转移（不直接写雏形）；长按 ≥0.8s 的"长暂停"与
  暂停数据效果相同，仅横幅文案差异，本实现以 data.long=true 标注。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

CLICK_MAX_DIST = 4
CLICK_MAX_MS = 600
DBLCLICK_MAX_MS = 500
DRAG_MIN_DIST = 8
DRAG_MAX_POINTS = 16
SCROLL_WINDOW_MS = 400
KEY_SEGMENT_SILENCE_MS = 1200
KEY_IME_DEBOUNCE_MS = 150
MOVE_SAMPLE_MS = 60
LONG_PAUSE_MS = 800


@dataclass(slots=True)
class Stub:
    """事件雏形（runner 补齐 win/uia/frames 后写盘）。"""

    eid: int
    type: str
    src: str = "hook"
    x: int | None = None
    y: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    flags: dict[str, bool] = field(default_factory=dict)
    # 键段结算时附着的焦点控件快照（runner 写入 ev.uia）。必须在结算时随
    # stub 一起带走：_key_flush 会重置 _seg_snap，若仍靠 runner 的实例状态
    # 读取，所有键入步骤都会丢失定位锚点（只能按"当前焦点"盲打）。
    uia: Any | None = None


# key_flusher(原始键段, 原因) -> 最终入库 stub 列表
KeyFlusher = Callable[[list["Stub"], str], list["Stub"]]


def _default_flusher(segment: list[Stub], reason: str) -> list[Stub]:
    return list(segment)


_MOD_KEYS = {"ctrl_l": "ctrl", "ctrl_r": "ctrl", "control_l": "ctrl", "control_r": "ctrl",
             "alt_l": "alt", "alt_r": "alt", "option": "alt",
             "shift_l": "shift", "shift_r": "shift",
             "cmd": "win", "cmd_l": "win", "cmd_r": "win", "win_l": "win", "win_r": "win", "meta": "win"}


def _norm_key(name: str) -> str:
    s = str(name or "")
    if len(s) >= 3 and s[0] == "'" and s[-1] == "'":
        s = s[1:-1]
    return s.lower()


# pynput 在 Windows 把某些组合/功能键以 ASCII 控制字符上报（不是命名键）：
# Ctrl+A..Z → 0x01..0x1A；退格/制表/回车/ESC/删除 → 0x08/0x09/0x0d/0x1b/0x7f。
# 不归一化就会把控制符当正文字符混进键段（回放注入乱码），或吞掉 Ctrl 组合语义。
_WIN_CTRL_TO_LETTER = {chr(c): chr(c + 0x60) for c in range(1, 27)}  # \x01..→a..z
_WIN_CTRL_NAMED = {"\x08": "backspace", "\x09": "tab", "\r": "enter",
                   "\x1b": "esc", "\x7f": "delete"}


def _normalize_control_char(name: str) -> tuple[str, bool]:
    """把控制字符还原为 (键名, 是否隐含 Ctrl)。普通可见字符原样返回 (name, False)。"""
    if len(name) == 1 and (ord(name) < 32 or name == "\x7f"):
        if name in _WIN_CTRL_NAMED:
            return _WIN_CTRL_NAMED[name], False
        if name in _WIN_CTRL_TO_LETTER:
            return _WIN_CTRL_TO_LETTER[name], True
    return name, False


def _key_parts_hit(hotkey: str, parts: set[str]) -> bool:
    spec = {p.strip() for p in str(hotkey).lower().replace(" ", "").split("+") if p.strip()}
    return bool(spec) and spec <= parts


def _compile_hotkeys(hotkeys: dict[str, str]) -> dict[str, tuple[frozenset[str], str]]:
    """{'pause': 'ctrl+alt+p'} → {'p': (frozenset({'ctrl','alt'}), 'pause')}"""
    out: dict[str, tuple[frozenset[str], str]] = {}
    for action, spec in hotkeys.items():
        want = {"toggle": "toggle", "pause": "pause", "discard": "discard", "marker": "marker"}.get(action)
        if not want:
            continue
        parts = [p.strip().lower() for p in str(spec).split("+") if p.strip()]
        if not parts:
            continue
        key = parts[-1]
        mods: set[str] = set()
        for p in parts[:-1]:
            if p in _MOD_KEYS:
                mods.add(_MOD_KEYS[p])
            elif p in ("ctrl", "alt", "shift", "win", "cmd"):
                mods.add({"ctrl": "ctrl", "cmd": "win"}.get(p, p))
        if key.startswith("f"):
            mods = set()
        out[key] = (frozenset(mods), want)
    return out


def _dist(x1: int, y1: int, x2: int, y2: int) -> float:
    return float(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)


def _sample_points(points: list[list[int]]) -> list[list[int]]:
    if len(points) <= DRAG_MAX_POINTS:
        return points
    step = (len(points) - 1) / (DRAG_MAX_POINTS - 1)
    return [points[int(round(i * step))] for i in range(DRAG_MAX_POINTS)]


class Normalizer:
    """有状态归一器。pause 短路在本层生效（§3.10 采集数据从未产生）。"""

    def __init__(self, key_flusher: KeyFlusher | None = None,
                 hotkeys: dict[str, str] | None = None,
                 on_segment_start: Callable[[int], None] | None = None) -> None:
        self._eid = 0
        self._key_flusher = key_flusher or _default_flusher
        self._hotkeys = _compile_hotkeys(hotkeys or {})
        self.on_segment_start = on_segment_start
        self.mask_keys: list[str] = []
        # 鼠标状态
        self._down: dict[str, dict[str, Any]] = {}
        self._last_up: dict[str, dict[str, Any]] = {}
        self._move_from: tuple[int, int] | None = None
        self._move_to: tuple[int, int] | None = None
        self._move_t0 = 0.0
        # 滚轮累计
        self._scroll_dx = 0
        self._scroll_dy = 0
        self._scroll_t = 0.0
        self._scroll_x = 0
        self._scroll_y = 0
        # 键段
        self._segment: list[Stub] = []
        self._held: set[str] = set()
        self._mods: set[str] = set()
        # 热键长按计时（抬起结算）
        self._hotkey_down_perf: str | None = None
        self._hotkey_pending: str = ""
        self._hotkey_down_t: float = 0.0
        # 控制
        self.paused = False
        self._suppress_text_until = 0.0

    # ── 公共 ───────────────────────────────────────────────────────
    def feed(self, raw: dict[str, Any]) -> list[Stub]:
        t_ns = int(raw.get("t_ns") or time.perf_counter_ns())
        kind = raw.get("kind")
        out: list[Stub] = []
        if kind == "move":
            # 拖拽进行中：移动点并入当前按下键的 path 采样（runner 无须再显式转喂）
            if self._down:
                self.track_drag_move(int(raw.get("x", 0)), int(raw.get("y", 0)))
            self._on_move(raw, t_ns)
            return out
        if kind == "scroll":
            # 400ms 窗口内累计（§3.3，以事件时间为基准）：窗口未过期则继续累积
            if not self._scroll_window_open_at(t_ns / 1e6):
                self._flush_scroll(out)
            self._flush_segment(out)
            self._on_scroll(raw, t_ns)
            return out
        if kind == "button":
            self._flush_segment(out)
            self._flush_scroll(out)
            self._on_button(raw, t_ns, out)
            return out
        if kind == "key":
            return self._on_key(raw, t_ns)
        return out

    def tick(self) -> list[Stub]:
        """worker 周期调用（≥5Hz）：键段防抖/静默结算、滚轮收尾。"""
        now = time.perf_counter_ns() / 1e6
        out: list[Stub] = []
        if self._segment:
            seg_t = float(self._segment[-1].data.get("_t_ms", 0) or 0)
            if now - seg_t >= KEY_SEGMENT_SILENCE_MS:
                self._flush_segment(out, reason="silence")
            elif now - seg_t >= KEY_IME_DEBOUNCE_MS:
                self._flush_segment(out, reason="ime")
        if (self._scroll_dx or self._scroll_dy) and now - self._scroll_t >= SCROLL_WINDOW_MS:
            self._flush_scroll(out)
        return out

    def force_flush(self) -> list[Stub]:
        out: list[Stub] = []
        self._flush_segment(out, reason="stop")
        self._flush_scroll(out)
        return out

    def enter_pause(self) -> list[Stub]:
        """进入暂停：先结算暂停前已缓冲的键段/滚轮段（那是合法的演示内容，
        必须入库，§3.10「丢弃」只针对暂停区间内的数据），再清零状态。
        返回待写 stubs（由 worker 线程落盘——与本方法同线程，无竞态）。
        """
        out: list[Stub] = []
        self._flush_segment(out, reason="pause")
        self._flush_scroll(out)
        self.paused = True
        self._segment.clear()
        self._down.clear()
        self._scroll_dx = self._scroll_dy = 0
        self._held.clear()
        return out

    def exit_pause(self) -> None:
        self.paused = False

    def make_stub(self, type_: str, *, src: str = "hook", x: int | None = None,
                  y: int | None = None, data: dict[str, Any] | None = None,
                  flags: dict[str, bool] | None = None) -> Stub:
        self._eid += 1
        return Stub(eid=self._eid, type=type_, src=src, x=x, y=y,
                    data=dict(data or {}), flags=dict(flags or {}))

    # ── 鼠标 ───────────────────────────────────────────────────────
    def _on_move(self, raw: dict[str, Any], t_ns: int) -> None:
        if self.paused:
            return
        t_ms = t_ns / 1e6
        if self._move_from is None:
            self._move_from = (int(raw["x"]), int(raw["y"]))
            self._move_t0 = t_ms
        self._move_to = (int(raw["x"]), int(raw["y"]))

    def track_drag_move(self, x: int, y: int) -> None:
        """按住期间移动点采样进 path（runner 对 move 转喂）。"""
        for d in self._down.values():
            last = d["points"][-1]
            if _dist(x, y, last[0], last[1]) >= 6:
                d["points"].append([x, y])
                d["moved"] = d.get("moved", 0.0) + _dist(x, y, last[0], last[1])

    def _move_summary(self, t_ns: int) -> Stub | None:
        if self._move_from is None or self._move_to is None:
            return None
        now_ms = t_ns / 1e6
        f, to = self._move_from, self._move_to
        self._move_from = self._move_to = None
        if f == to:
            return None
        dur = max(0, int(now_ms - self._move_t0))
        return self.make_stub("pointer_move", src="derived", x=to[0], y=to[1],
                              data={"from": list(f), "to": list(to), "dur_ms": dur,
                                    "samples": max(2, min(16, dur // MOVE_SAMPLE_MS + 1))})

    def _on_button(self, raw: dict[str, Any], t_ns: int, out: list[Stub]) -> None:
        if self.paused:
            return
        btn = str(raw.get("button", "left"))
        x, y = int(raw["x"]), int(raw["y"])
        t_ms = t_ns / 1e6
        if raw.get("pressed"):
            self._down[btn] = {"x": x, "y": y, "t": t_ms, "points": [[x, y]], "moved": 0.0}
            return
        d = self._down.pop(btn, None)
        if d is None:
            return
        ev_type = {"left": "click", "right": "right_click", "middle": "middle_click"}.get(btn, "click")
        dist = _dist(x, y, d["x"], d["y"])
        dur = t_ms - d["t"]
        moved = max(dist, d.get("moved", 0.0))
        sm = self._move_summary(t_ns)
        if sm:
            out.append(sm)
        if moved > DRAG_MIN_DIST:
            out.append(self.make_stub("drag", x=x, y=y,
                                      data={"button": btn, "path": _sample_points(d["points"]),
                                            "dur_ms": int(dur), "from": [d["x"], d["y"]],
                                            "to": [x, y], "modifiers": sorted(self._mods)}))
            self._last_up.pop(btn, None)
            return
        prev = self._last_up.get(btn)
        if (btn == "left" and prev is not None and t_ms - float(prev["t"]) <= DBLCLICK_MAX_MS
                and _dist(x, y, int(prev["x"]), int(prev["y"])) <= CLICK_MAX_DIST):
            out.append(self.make_stub("double_click", x=x, y=y,
                                      data={"button": btn, "supersedes_eid": prev["eid"],
                                            "modifiers": sorted(self._mods)}))
            self._last_up[btn] = {"x": x, "y": y, "t": t_ms, "eid": out[-1].eid}
            return
        data: dict[str, Any] = {"button": btn, "count": 1, "modifiers": sorted(self._mods)}
        if dist <= CLICK_MAX_DIST and dur <= CLICK_MAX_MS:
            data["down_up_ms"] = int(dur)
        elif dist <= CLICK_MAX_DIST:
            data["long_press_ms"] = int(dur)
        else:
            # 小幅位移（4–8px）的 down/up：视作点击（容差）
            data["down_up_ms"] = int(dur)
        out.append(self.make_stub(ev_type, x=x, y=y, data=data))
        self._last_up[btn] = {"x": x, "y": y, "t": t_ms, "eid": out[-1].eid}

    # ── 滚轮 ───────────────────────────────────────────────────────
    def _on_scroll(self, raw: dict[str, Any], t_ns: int) -> None:
        if self.paused:
            return
        if not (self._scroll_dx or self._scroll_dy):
            self._scroll_x = int(raw["x"])
            self._scroll_y = int(raw["y"])
        self._scroll_dx += int(raw.get("dx", 0))
        self._scroll_dy += int(raw.get("dy", 0))
        self._scroll_t = t_ns / 1e6

    def _scroll_window_open(self) -> bool:
        if not (self._scroll_dx or self._scroll_dy):
            return False
        return (time.perf_counter_ns() / 1e6 - self._scroll_t) < SCROLL_WINDOW_MS

    def _scroll_window_open_at(self, now_ms: float) -> bool:
        if not (self._scroll_dx or self._scroll_dy):
            return False
        return (now_ms - self._scroll_t) < SCROLL_WINDOW_MS

    def _scroll_expired(self) -> bool:
        return bool((self._scroll_dx or self._scroll_dy) and not self._scroll_window_open())

    def _flush_scroll(self, out: list[Stub]) -> None:
        if not (self._scroll_dx or self._scroll_dy):
            return
        dy, dx = self._scroll_dy, self._scroll_dx
        self._scroll_dx = self._scroll_dy = 0
        sm = self._move_summary(int(time.perf_counter_ns()))
        if sm:
            out.append(sm)
        out.append(self.make_stub("scroll", x=self._scroll_x, y=self._scroll_y,
                                  data={"dx": dx, "dy": dy, "notches": abs(dy) or abs(dx)}))

    # ── 键盘 ───────────────────────────────────────────────────────
    def _on_key(self, raw: dict[str, Any], t_ns: int) -> list[Stub]:
        out: list[Stub] = []
        raw_name = str(raw.get("name", ""))
        # Windows 钩子把 Ctrl+A..Z 等以 ASCII 控制符上报（\x01..\x1a）：归一化为
        # 语义键 + 隐含 Ctrl，否则控制符会当正文混进键段（回放注入乱码、组合键丢失）。
        name, ctrl_implied = _normalize_control_char(raw_name)
        # 空格键 pynput 上报为 'space'（长度>1 会被 merge 当功能键丢弃 → 正文丢空格）。
        if name == "space":
            name = " "
        down = bool(raw.get("down"))
        low = _norm_key(name)
        mod = _MOD_KEYS.get(low)
        # 控制热键：按下记录时刻与意图，抬起结算（长按 ≥0.8s = 长暂停，仅横幅/统计差异）
        hk = self._hotkeys.get(low)
        if hk is not None:
            need_mods, want = hk
            mods_ok = len(need_mods) == 0 or self._mods >= need_mods
            if down and mods_ok:
                if self._hotkey_down_perf != low:
                    self._hotkey_down_perf = low
                    self._hotkey_pending = want
                    self._hotkey_down_t = t_ns / 1e6
                return out
            if not down and self._hotkey_down_perf == low:
                want2 = self._hotkey_pending or want
                self._hotkey_down_perf = None
                self._hotkey_pending = ""
                long_hold = (t_ns / 1e6 - self._hotkey_down_t) >= LONG_PAUSE_MS
                return self._control_stubs(want2, out, long_hold=long_hold)
            # 非热键意图（如纯按 'r' 未带 ctrl+alt）：不吞事件，落到下面的正文键处理。
            # 旧实现只要尾键命中 _hotkeys 就 return，导致这些字母永远进不了键段
            # （run8 坐实 "repeat"→"eeat"）。
            if down:
                self._held.discard(low)  # 清掉可能被热键分支预置的 held，避免误吞
        if mod:
            if down:
                self._mods.add(mod)
            else:
                self._mods.discard(mod)
            return out
        if self.paused:
            return out
        if not down:
            self._held.discard(low)
            return out
        if low in self._held:
            return out  # 自动重复丢弃（§3.3 flags 简化：不记 repeat）
        self._held.add(low)
        # 控制符还原出的隐含 Ctrl（\x01→ctrl+a）：即便没有独立 ctrl_l 事件也并入修饰集，
        # 使组合键走 hotkey/clipboard 判定，而不是把 'a' 当正文键段字符。
        held_mods = set(self._mods) | ({"ctrl"} if ctrl_implied else set())
        parts = held_mods | {low}
        if "ctrl" in held_mods and low in ("c", "x", "v"):
            op = {"c": "copy", "x": "cut", "v": "paste"}[low]
            self._flush_segment(out)
            out.append(self.make_stub("clipboard", src="derived", data={"op": op}))
            if low == "v" and any(_key_parts_hit(k, parts) for k in self.mask_keys):
                self._suppress_text_until = time.perf_counter_ns() / 1e6 + 2000
                self._held.discard(low)
            return out
        if any(_key_parts_hit(k, parts) for k in self.mask_keys):
            self._suppress_text_until = time.perf_counter_ns() / 1e6 + 2000
            return out
        if time.perf_counter_ns() / 1e6 < self._suppress_text_until and len(low) == 1:
            return out  # masked 组合键后的文本抑制窗
        was_empty = not self._segment
        # 组合键（隐含或显式 Ctrl + 字母）：独立成段——入段前后各划一次边界，
        # 否则组合键会和后续普通键混进同一文本段（run11 坐实：Ctrl+A 的 'a'
        # 混入正文成 "ahello…"，hotkey 步丢失）。
        if ctrl_implied and low.isalpha():
            self._flush_segment(out)
            self._segment.append(self.make_stub("key", data={
                "key": name, "down": True, "vk": int(raw.get("vk", 0) or 0),
                "modifiers": sorted(held_mods), "_t_ms": t_ns / 1e6}))
            self._flush_segment(out)
            return out
        self._segment.append(self.make_stub("key", data={
            "key": name, "down": True, "vk": int(raw.get("vk", 0) or 0),
            "modifiers": sorted(held_mods), "_t_ms": t_ns / 1e6}))
        if was_empty and self.on_segment_start is not None:
            try:
                self.on_segment_start(int(raw.get("focus_hwnd", 0) or 0))
            except Exception:  # noqa: BLE001
                pass
        return out

    def _control_stubs(self, want: str, out: list[Stub], *, long_hold: bool) -> list[Stub]:
        if want == "pause":
            # 热键切换语义：暂停中再按 = 恢复；进入时立即置 paused（本层即真相源）
            if self.paused:
                self.exit_pause()
                out.append(self.make_stub("resume", src="system"))
            else:
                self.enter_pause()
                out.append(self.make_stub("pause", src="system", data={"long": long_hold}))
        elif want == "discard":
            out.append(self.make_stub("discard", src="system"))
        elif want == "marker":
            out.append(self.make_stub("marker", src="marker", data={"label": "", "note": ""}))
        elif want == "toggle":
            # toggle（Ctrl+Alt+R）：暂停中恢复、录制中=停止（runner 处理 stop 分叉）
            if self.paused:
                self.exit_pause()
                out.append(self.make_stub("resume", src="system"))
            else:
                out.append(self.make_stub("toggle", src="system"))
        return out

    def _flush_segment(self, out: list[Stub], reason: str = "boundary") -> None:
        if not self._segment:
            return
        seg = self._segment
        self._segment = []
        try:
            resolved = self._key_flusher(seg, reason)
        except Exception:  # noqa: BLE001 - 差分结算失败退回原始键
            resolved = list(seg)
        out.extend(resolved)
