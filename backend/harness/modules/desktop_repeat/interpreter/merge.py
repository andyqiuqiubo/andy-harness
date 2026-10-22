"""S1–S3：去噪 / 归并 / 窗口绑定（§5.4）。

输入：RecEvent 列表（含控制事件 pause/resume/marker/session_*）。
输出：语义事件列表（dict 形态，带 t_eff 有效时间轴与窗口归属）。

实现说明（与采集层分工）：
- click/double_click/drag/scroll 已由归一化层合成，本层负责：
  * double_click 的 supersedes_eid 取代首次 click（写盘时 eid 附在 data.eid）；
  * 键段合并为 type 候选段（边界=焦点变化/非键事件/静默>1200ms，按已结算段处理）；
  * 组合键合成 hotkey（单键段中含修饰键+字符键时）；
  * 丢弃 pointer_move 摘要（保留给 S5 的间隔推断用）、被取代 click、纯修饰键残留；
- 暂停区间扣除：t_eff = t - Σ已结束暂停时长；pause/resume 边界外事件本就未采
  （§3.10 强保证），此处只做兜底（历史数据/手动编辑的 bundle）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..models import RecEvent

KEY_SILENCE_MS = 1200


@dataclass(slots=True)
class SemEvent:
    """语义事件（编译期中间表示）。"""

    seq: int
    t: int                 # 原始会话毫秒
    t_eff: int             # 有效时间轴毫秒（扣暂停）
    type: str              # click|double_click|right_click|drag|scroll|type|hotkey|key|window_change|marker|launch_hint
    ev: RecEvent
    win: dict[str, Any] = field(default_factory=dict)
    masked: bool = False
    ime_risk: bool = False
    password: bool = False


def pause_intervals(events: list[RecEvent]) -> list[tuple[int, int]]:
    """从 pause/resume 控制事件还原区间。未闭合的 pause → 开区间到最后一个事件。"""
    spans: list[tuple[int, int]] = []
    open_t: int | None = None
    last_t = 0
    for ev in events:
        last_t = max(last_t, ev.t)
        if ev.type == "pause":
            open_t = ev.t
        elif ev.type == "resume" and open_t is not None:
            spans.append((open_t, ev.t))
            open_t = None
    if open_t is not None:
        spans.append((open_t, last_t))
    return spans


def to_effective(t: int, spans: list[tuple[int, int]]) -> int:
    """t 映射到有效时间轴（t_eff）。"""
    shift = 0
    for a, b in spans:
        if b <= t:
            shift += b - a
        elif a < t < b:
            # 落在暂停区间内（理论上无事件）：钳到区间起点
            return a - sum(bb - aa for aa, bb in spans if bb <= a)
    return t - shift


def compile_semantics(events: list[RecEvent]) -> tuple[list[SemEvent], dict[str, Any]]:
    """S1–S3 主入口。返回 (语义事件, 元信息)。"""
    spans = pause_intervals(events)
    superseded: set[int] = set()
    for ev in events:
        if ev.type == "double_click":
            se = ev.data.get("supersedes_eid")
            if isinstance(se, int):
                superseded.add(se)
            # 写盘层把 eid 记在 data.eid 或直接用 seq（本项目 data.seq 同步）
            se2 = ev.data.get("supersedes_seq")
            if isinstance(se2, int):
                superseded.add(se2)

    meta: dict[str, Any] = {"pauses": len(spans), "pause_ms": sum(b - a for a, b in spans)}
    out: list[SemEvent] = []
    key_segment: list[RecEvent] = []
    dropped = 0

    def flush_segment() -> None:
        nonlocal key_segment
        if not key_segment:
            return
        seg = key_segment
        key_segment = []
        _emit_segment(seg, spans, out)

    for ev in events:
        if ev.type in ("session_start", "session_end", "pause", "resume", "discard"):
            continue
        if ev.type == "pointer_move":
            dropped += 1
            continue
        if ev.type == "marker":
            if ev.data.get("warn") == "elevated":
                continue  # 告警 marker 不生成步骤
            out.append(_mk(ev, ev.t, spans, "marker"))
            continue
        if ev.type == "clipboard":
            # 复制/剪切/粘贴合成为 hotkey 步骤（normalizer 对 ctrl+c/x/v 发 clipboard 而非 key）
            flush_segment()
            op = str(ev.data.get("op", ""))
            combo = {"copy": "ctrl+c", "cut": "ctrl+x", "paste": "ctrl+v"}.get(op, "")
            if combo and not ev.flags.get("masked"):
                se = _mk(ev, ev.t, spans, "hotkey")
                se.ev.data = {**ev.data, "combo": combo}
                out.append(se)
            else:
                dropped += 1
            continue
        if ev.type == "double_click":
            flush_segment()
            se = _mk(ev, ev.t, spans, "double_click")
            out.append(se)
            continue
        if ev.type in ("click", "right_click", "middle_click", "drag", "scroll"):
            flush_segment()
            kind = {"click": "click", "right_click": "right_click",
                    "middle_click": "middle_click", "drag": "drag", "scroll": "scroll"}[ev.type]
            out.append(_mk(ev, ev.t, spans, kind))
            # 被双击取代的首次 click：丢弃。取代 id 优先比对归一化 eid（写盘时
            # 附着到 data.eid）；旧 bundle 无 eid 才回退 seq 比对（体检 P0-3——
            # seq 与 eid 是两个编号空间，前置系统事件会让二者错位）。
            ids = {ev.seq}
            if isinstance(ev.data.get("eid"), int):
                ids.add(int(ev.data["eid"]))
            if ev.type == "click" and ids & superseded:
                out.pop()
                dropped += 1
            continue
        if ev.type == "key":
            # 单修饰键/被密码掩码的键段占位
            if ev.data.get("password"):
                flush_segment()
                se = _mk(ev, ev.t, spans, "type")
                se.password = True
                out.append(se)
                continue
            # 静默边界 >1200ms（有效时间轴）：先结算前段
            if key_segment:
                prev_eff = to_effective(key_segment[-1].t, spans)
                if ev.t - prev_eff > KEY_SILENCE_MS:
                    flush_segment()
            key_segment.append(ev)
            continue
        if ev.type == "text_input":
            flush_segment()
            out.append(_mk(ev, ev.t, spans, "type"))
            continue
        if ev.type == "window_change":
            flush_segment()
            se = _mk(ev, ev.t, spans, "window_change")
            out.append(se)
            continue
    flush_segment()

    # S3 窗口绑定：window_change → focus 步骤；仅与"上一条已发出的 focus"去重
    bound: list[SemEvent] = []
    last_focus_key: tuple[str, str] | None = None
    for se in out:
        if se.type == "window_change":
            curr = se.ev.data.get("curr") or {}
            key = (str(curr.get("exe", "")), str(curr.get("title", "")))
            if last_focus_key == key:
                dropped += 1
                continue
            last_focus_key = key
            se.type = "window_focus"
        elif se.type == "window_focus" and se.win:
            last_focus_key = (str(se.win.get("exe", "")), str(se.win.get("title", "")))
        bound.append(se)

    meta["dropped"] = dropped
    return bound, meta


def _win_dict(ev: RecEvent) -> dict[str, Any]:
    w = ev.win
    if w is None:
        return {}
    if isinstance(w, dict):
        return w
    return w.to_dict()  # WindowRef（内存态管线，未经 JSONL roundtrip）


def _mk(ev: RecEvent, t: int, spans: list[tuple[int, int]], type_: str) -> SemEvent:
    return SemEvent(seq=ev.seq, t=ev.t, t_eff=to_effective(ev.t, spans), type=type_, ev=ev,
                    win=_win_dict(ev), masked=bool(ev.flags.get("masked")),
                    ime_risk=bool(ev.flags.get("ime_risk")),
                    password=bool(ev.data.get("password")))


_MOD_NAME_PREFIXES = ("ctrl", "alt", "shift", "win", "cmd", "meta", "option")


def _is_mod_keyname(k: str) -> bool:
    return any(k.lower().startswith(p) for p in _MOD_NAME_PREFIXES)


def _emit_segment(seg: list[RecEvent], spans: list[tuple[int, int]], out: list[SemEvent]) -> None:
    """键段 → type/hotkey 语义事件。

    规则：
    - 段内事件自带 modifiers 快照（normalizer 记录）；单独按修饰键不会产生段事件；
    - 恰含 1 个命名功能键（enter/tab/esc/f*…）且无字符键 → hotkey（可含修饰）；
    - 恰含 1 个字符键且修饰集非空 → hotkey；
    - 其余字符 → type 段；
    - 段里混入的裸修饰键事件（历史 bundle）：并入修饰集、不入文本。
    """
    mods: set[str] = set()
    chars: list[str] = []
    funcs: list[str] = []
    for ev in seg:
        for m in (ev.data.get("modifiers") or []):
            mods.add(str(m))
        k = str(ev.data.get("key", ""))
        if not k:
            continue
        if len(k) == 1:
            chars.append(k)
        else:
            base = k.lower().rstrip("_lr0123456789") if _is_mod_keyname(k) else k.lower()
            if _is_mod_keyname(k):
                # 裸修饰键事件并入 mods（ctrl_l→ctrl）
                mods.add(k.lower().split("_")[0] if k.lower().startswith("ctrl") else
                         ("win" if k.lower() in ("cmd", "cmd_l", "cmd_r", "win", "win_l", "win_r", "meta", "option")
                          else k.lower().split("_")[0]))
            elif base in ("cmd", "win", "meta", "ctrl", "alt", "shift"):
                mods.add(base)
            else:
                funcs.append(k)
    head = seg[0]
    orig_data = dict(head.data)
    combo = None
    if funcs and not chars:
        key = funcs[0]
        combo = "+".join(sorted(mods) + [key]) if mods else key
    elif len(chars) == 1 and mods:
        combo = "+".join(sorted(mods) + [chars[0]])
    if combo is not None:
        # Alt+Tab 的语义就是"切换窗口"：回放时按 Alt+Tab 会切到**任意**上一个窗口，
        # 完全不可控（真机坐实：从上一个窗口切回 EditPlus 被录成 alt+tab，
        # 回放切错窗口，后续步骤全落在错误应用上）。转成 window_focus，
        # 由执行层按窗口标题可靠聚焦。
        se_type = "window_focus" if ("alt" in combo and "tab" in combo) else "hotkey"
        se = SemEvent(seq=head.seq, t=head.t, t_eff=to_effective(head.t, spans),
                      type=se_type, ev=head, win=_win_dict(head),
                      masked=bool(head.flags.get("masked")))
        head.data = {**orig_data, "combo": combo}
        out.append(se)
        return
    if chars:
        text = "".join(chars)
        se = SemEvent(seq=head.seq, t=head.t, t_eff=to_effective(head.t, spans),
                      type="type", ev=head, win=_win_dict(head),
                      masked=bool(head.flags.get("masked")),
                      ime_risk=bool(head.flags.get("ime_risk")))
        head.data = {**orig_data, "text": text, "count": len(text)}
        out.append(se)
        # 段内混入的功能键（enter/tab/esc/方向键…）必须单独成 hotkey：
        # "输入 dify 后按回车"是搜索/表单提交的常见套路，旧实现把它们丢掉，
        # 回放输完文字就停住不动（真机坐实：地址栏输入后无法跳转）。
        for fk in funcs:
            # 每个功能键需要独立的事件副本：多个 hotkey 共用同一个 RecEvent 会
            # 让 combo 互相覆盖（真机 bug：段内两个功能键只剩最后一个）。
            ev2 = replace(head, data={**orig_data, "combo": fk})
            out.append(SemEvent(seq=head.seq, t=head.t, t_eff=to_effective(head.t, spans),
                                type="hotkey", ev=ev2, win=_win_dict(ev2),
                                masked=bool(head.flags.get("masked"))))
        return
    # 纯修饰键段：丢弃
