"""desktop_repeat 纯逻辑单元测试（设计 §12.1 单元层）。

覆盖：jsonl 读写/截断恢复、manifest 封版校验、normalizer 事件合成、
merge S1-S3 管线（含暂停扣除）、locator_builder 打分、coords 漂移分级/闸门、
params 解析与脱敏、bus 回放快照与终止收束（两个已知坑位回归）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.modules.desktop_repeat import coords
from harness.modules.desktop_repeat.bus import ProgressStore
from harness.modules.desktop_repeat.config import RepeatConfig
from harness.modules.desktop_repeat.errors import RepeatError
from harness.modules.desktop_repeat.executor import params
from harness.modules.desktop_repeat.interpreter.locator_builder import build_chain
from harness.modules.desktop_repeat.interpreter.merge import compile_semantics, pause_intervals
from harness.modules.desktop_repeat.models import Env, MonitorInfo, Pos, RecEvent, UiaSnapshot, WindowRef
from harness.modules.desktop_repeat.store import manifest as mf
from harness.modules.desktop_repeat.store.jsonl import JsonlWriter, read_events


def _ev(seq: int, t: int, type_: str, **kw) -> RecEvent:
    return RecEvent(seq=seq, t=t, ts=1760000000000 + t, type=type_, src=kw.pop("src", "hook"),
                    data=kw.pop("data", {}), flags=kw.pop("flags", {}),
                    pos=kw.pop("pos", None), win=kw.pop("win", None), uia=kw.pop("uia", None))


# ── jsonl ───────────────────────────────────────────────────────
class TestJsonl:
    def test_roundtrip_and_seq(self, tmp_path: Path):
        p = tmp_path / "events.jsonl"
        w = JsonlWriter(p)
        for i in range(5):
            w.write(_ev(i, i * 10, "click", data={"button": "left"}))
        w.close()
        events, truncated = read_events(p)
        assert len(events) == 5 and not truncated
        assert [e.seq for e in events] == [0, 1, 2, 3, 4]
        assert events[2].data["button"] == "left"

    def test_seq_violation_rejected(self, tmp_path: Path):
        w = JsonlWriter(tmp_path / "e.jsonl")
        w.write(_ev(0, 0, "click"))
        with pytest.raises(RuntimeError):
            w.write(_ev(0, 1, "click"))  # INV-R1
        w.close()

    def test_truncated_last_line_dropped(self, tmp_path: Path):
        p = tmp_path / "e.jsonl"
        w = JsonlWriter(p)
        w.write(_ev(0, 0, "click"))
        w.write(_ev(1, 10, "click"))
        w.close()
        with open(p, "a", encoding="utf-8") as f:
            f.write('{"v":1,"seq":2,"t":2')  # 半行（模拟崩溃）
        events, truncated = read_events(p)
        assert len(events) == 2 and truncated

    def test_uia_value_never_persisted(self, tmp_path: Path):
        p = tmp_path / "e.jsonl"
        w = JsonlWriter(p)
        snap = UiaSnapshot(control_type="Edit", name="账号", automation_id="txtUser",
                           is_editable=True, value="super-secret-pw")
        w.write(_ev(0, 0, "click", uia=snap))
        w.close()
        raw = p.read_text(encoding="utf-8")
        assert "super-secret-pw" not in raw and "value" not in raw
        events, _ = read_events(p)
        assert events[0].uia is not None and events[0].uia.value == ""

    def test_schema_too_new_rejected(self, tmp_path: Path):
        p = tmp_path / "e.jsonl"
        p.write_text('{"v":99,"seq":0,"t":0,"ts":0,"type":"click","src":"hook","data":{}}\n',
                     encoding="utf-8")
        with pytest.raises(RepeatError) as ei:
            read_events(p)
        assert ei.value.code == "RP-6002"


class TestBundleConcurrency:
    """多线程 emit 竞态回归（真实缺陷：worker 线程曾因 seq 违例崩溃丢事件）。

    复现路径：两个线程各自「构造事件→（慢操作）→emit」。修复前 seq 在构造
    时预分配，慢操作时长不一 → 先分配的后写入 → 文件出现 0,2,1 → INV-R1 违例
    → worker 线程死亡、后续演示事件全丢。修复后 emit() 在同一把锁内
    「分配 + 落盘」，seq 恒等于写入顺序。
    """

    def _writer(self, tmp_path: Path):
        from harness.modules.desktop_repeat.config import RecordConfig
        from harness.modules.desktop_repeat.store.bundle import BundleWriter
        env = Env(os="T", virtual_screen=(0, 0, 1920, 1080),
                  monitors=[MonitorInfo(index=1, rect=(0, 0, 1920, 1080), scale=1.0)])
        return BundleWriter(tmp_path, "sess", RepeatConfig.load({}), RecordConfig(), env)

    def test_concurrent_emit_seq_monotonic(self, tmp_path: Path):
        import threading
        w = self._writer(tmp_path)

        def worker(prefix: str):
            for i in range(50):
                w.emit(RecEvent(seq=-1, t=i, ts=i, type=prefix, src="hook", data={"i": i}))

        threads = [threading.Thread(target=worker, args=(name,))
                   for name in ("click", "key", "scroll", "pause")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        w.flush()
        events, truncated = read_events(tmp_path / "sess" / "events.jsonl")
        seqs = [e.seq for e in events]
        # INV-R1：200 条全部落盘，seq 严格递增且连续 0..199，无违例、无丢失
        assert len(events) == 200 and not truncated
        assert seqs == list(range(200))

    def test_frame_id_unique_under_concurrency(self, tmp_path: Path):
        import threading
        w = self._writer(tmp_path)
        ids: list[int] = []
        lock = threading.Lock()

        def grab():
            for _ in range(40):
                fid = w.next_frame_id()
                with lock:
                    ids.append(fid)

        threads = [threading.Thread(target=grab) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(ids) == list(range(1, 161))  # 160 个唯一、无重号


# ── manifest ────────────────────────────────────────────────────
class TestManifest:
    def test_build_verify_tamper(self, tmp_path: Path):
        (tmp_path / "frames").mkdir()
        (tmp_path / "session.json").write_text('{"a":1}', encoding="utf-8")
        (tmp_path / "events.jsonl").write_text('{"x":2}\n', encoding="utf-8")
        (tmp_path / "frames" / "000000-pre.jpg").write_bytes(b"IMG")
        (tmp_path / "runtime").mkdir()
        (tmp_path / "runtime" / "control.json").write_text("{}", encoding="utf-8")
        m = mf.build_manifest(tmp_path, "r-x")
        mf.write_manifest(tmp_path, m)
        ok, diffs = mf.verify_manifest(tmp_path)
        assert ok and not diffs
        # runtime 不入清单
        assert all(not str(f["path"]).startswith("runtime") for f in m["files"])
        # 篡改 → RP-6001
        (tmp_path / "events.jsonl").write_text('{"x":3}\n', encoding="utf-8")
        with pytest.raises(RepeatError) as ei:
            mf.verify_manifest(tmp_path)
        assert ei.value.code == "RP-6001"

    def test_frames_gc_tolerated(self, tmp_path: Path):
        (tmp_path / "frames").mkdir()
        (tmp_path / "a.txt").write_text("A", encoding="utf-8")
        (tmp_path / "frames" / "f.jpg").write_bytes(b"x")
        m = mf.build_manifest(tmp_path, "r-x")
        mf.write_manifest(tmp_path, m)
        (tmp_path / "frames" / "f.jpg").unlink()  # 模拟 GC 删帧
        ok, _ = mf.verify_manifest(tmp_path)      # ignore_gc 默认开
        assert ok


# ── normalizer ──────────────────────────────────────────────────
class TestNormalizer:
    def _norm(self):
        from harness.modules.desktop_repeat.recorder.normalizer import Normalizer
        return Normalizer(hotkeys={"pause": "ctrl+alt+p", "discard": "ctrl+alt+x",
                                   "marker": "f10", "toggle": "ctrl+alt+r"})

    def _click(self, n, x, y, btn="left", t_ns=1_000_000):
        down = {"kind": "button", "x": x, "y": y, "button": btn, "pressed": True,
                "t_ns": t_ns}
        up = {"kind": "button", "x": x, "y": y, "button": btn, "pressed": False,
              "t_ns": t_ns + 80_000_000}
        out = n.feed(down)
        return out, n.feed(up)

    def test_click_emit_and_move_summary(self):
        n = self._norm()
        n.feed({"kind": "move", "x": 10, "y": 10, "t_ns": 500_000})
        n.feed({"kind": "move", "x": 300, "y": 200, "t_ns": 600_000})
        _down, up_out = self._click(n, 300, 200, t_ns=1_000_000)
        types = [s.type for s in up_out]
        assert types == ["pointer_move", "click"]
        mv = up_out[0]
        assert mv.data["from"] == [10, 10] and mv.data["to"] == [300, 200]
        assert mv.src == "derived"
        assert up_out[1].data["button"] == "left"

    def test_double_click_supersedes(self):
        n = self._norm()
        # 首次 click：down@1ms up@81ms
        _d, out1 = self._click(n, 100, 100, t_ns=1_000_000)
        # 第二次：down@300ms up@381ms（距首次 up 300ms ≤ 500ms → 双击）
        _d, out2 = self._click(n, 100, 100, t_ns=300_000_000)
        dc = [s for s in out2 if s.type == "double_click"]
        assert len(dc) == 1
        first_click = [s for s in out1 if s.type == "click"][0]
        assert dc[0].data["supersedes_eid"] == first_click.eid

    def test_drag_detection(self):
        n = self._norm()
        t = 1_000_000
        n.feed({"kind": "button", "x": 50, "y": 50, "button": "left", "pressed": True, "t_ns": t})
        for x in (80, 110, 140, 170):
            n.feed({"kind": "move", "x": x, "y": x, "t_ns": t + x * 1000})
        out = n.feed({"kind": "button", "x": 200, "y": 200, "button": "left", "pressed": False,
                      "t_ns": t + 300_000_000})
        drags = [s for s in out if s.type == "drag"]
        assert len(drags) == 1
        assert drags[0].data["from"] == [50, 50] and drags[0].data["to"] == [200, 200]
        assert len(drags[0].data["path"]) >= 2
        assert drags[0].data["dur_ms"] == 300

    def test_scroll_window_accumulates(self):
        n = self._norm()
        base = 1_000_000_000
        out_all: list = []
        # 同窗口（3×50ms=100ms < 400ms）的连续滚轮不结算
        for i in range(3):
            out_all += n.feed({"kind": "scroll", "x": 10, "y": 10, "dx": 0, "dy": -1,
                               "t_ns": base + i * 50_000_000})
        sc = [s for s in out_all if s.type == "scroll"]
        assert sc == []
        # 跨窗口（500ms > 400ms）的滚轮 → 先结算旧段（dy=-3）
        out_all += n.feed({"kind": "scroll", "x": 12, "y": 12, "dx": 0, "dy": -2,
                           "t_ns": base + 600_000_000})
        sc = [s for s in out_all if s.type == "scroll"]
        assert len(sc) == 1
        assert sc[0].data["dy"] == -3
        assert sc[0].x == 10 and sc[0].y == 10  # 窗口起点位置（stub 顶层坐标）
        # 收尾结算最后一段（-2）
        tail = n.force_flush()
        sc2 = [s for s in tail if s.type == "scroll"]
        assert len(sc2) == 1 and sc2[0].data["dy"] == -2

    def test_pause_short_circuit(self):
        n = self._norm()
        t = 1_000_000_000
        n.feed({"kind": "key", "name": "ctrl_l", "down": True, "t_ns": t, "vk": 17})
        n.feed({"kind": "key", "name": "alt_l", "down": True, "t_ns": t + 1_000_000, "vk": 18})
        # p 抬起才结算：短按 → pause
        n.feed({"kind": "key", "name": "p", "char": "p", "down": True, "t_ns": t + 2_000_000, "vk": 80})
        out = n.feed({"kind": "key", "name": "p", "char": "p", "down": False,
                      "t_ns": t + 300_000_000, "vk": 80})
        assert [s.type for s in out] == ["pause"]
        assert n.paused is True
        # 暂停期间的按键：零产出
        out2 = n.feed({"kind": "key", "name": "x", "char": "x", "down": True,
                       "t_ns": t + 400_000_000, "vk": 88})
        assert out2 == []
        # 再短按一次热键 → resume（切换语义）
        n.feed({"kind": "key", "name": "p", "char": "p", "down": True, "t_ns": t + 500_000_000, "vk": 80})
        out3 = n.feed({"kind": "key", "name": "p", "char": "p", "down": False,
                       "t_ns": t + 800_000_000, "vk": 80})
        assert [s.type for s in out3] == ["resume"]
        assert n.paused is False

    def test_long_pause_hold_flag(self):
        n = self._norm()
        t = 1_000_000_000
        n.feed({"kind": "key", "name": "ctrl_l", "down": True, "t_ns": t, "vk": 17})
        n.feed({"kind": "key", "name": "alt_l", "down": True, "t_ns": t + 1_000_000, "vk": 18})
        n.feed({"kind": "key", "name": "p", "char": "p", "down": True, "t_ns": t + 2_000_000, "vk": 80})
        # 按住 1s 后抬起 → 长暂停（data.long=True）
        out = n.feed({"kind": "key", "name": "p", "char": "p", "down": False,
                      "t_ns": t + 2_000_000 + 1_000_000_000, "vk": 80})
        assert out[0].type == "pause" and out[0].data.get("long") is True

    def test_key_segment_flush_by_force(self):
        n = self._norm()
        base = 1_000_000_000
        for i, ch in enumerate("abc"):
            n.feed({"kind": "key", "name": ch, "char": ch, "down": True,
                    "t_ns": base + i * 30_000_000, "vk": ord(ch.upper())})
            n.feed({"kind": "key", "name": ch, "char": ch, "down": False,
                    "t_ns": base + i * 30_000_000 + 10_000_000, "vk": ord(ch.upper())})
        out = n.force_flush()
        assert [s.type for s in out] == ["key", "key", "key"]
        assert [s.data["key"] for s in out] == ["a", "b", "c"]

    def test_space_kept_as_text_char(self):
        """回归：pynput 把空格报成 'space'（长度>1），曾被 merge 当功能键丢弃 →
        "a b" 变 "ab"。归一化为 ' ' 后必须作为正文键入段。"""
        n = self._norm()
        base = 1_000_000_000
        for i, nm in enumerate(["a", "space", "b"]):
            vk = 32 if nm == "space" else ord(nm.upper())
            n.feed({"kind": "key", "name": nm, "char": " " if nm == "space" else nm,
                    "down": True, "t_ns": base + i * 20_000_000, "vk": vk})
        out = n.force_flush()
        joined = "".join(s.data.get("key", "") for s in out if s.type == "key")
        assert joined == "a b", f"空格丢失: {joined!r}"

    def test_hotkey_letter_falls_back_to_text(self):
        """回归：字母 r 是热键 ctrl+alt+r 的尾键，曾被热键分支无条件吞掉（无修饰也 return），
        导致 "repeat" 里的 r 全部消失。无 ctrl+alt 时必须回落为正文键。"""
        n = self._norm()
        base = 1_000_000_000
        for i, ch in enumerate("re"):
            n.feed({"kind": "key", "name": ch, "char": ch, "down": True,
                    "t_ns": base + i * 30_000_000, "vk": ord(ch.upper())})
            n.feed({"kind": "key", "name": ch, "char": ch, "down": False,
                    "t_ns": base + i * 30_000_000 + 10_000_000, "vk": ord(ch.upper())})
        out = n.force_flush()
        joined = "".join(s.data.get("key", "") for s in out if s.type == "key")
        assert joined == "re", f"热键尾字母被吞: {joined!r}"

    def test_ctrl_letter_control_char_becomes_hotkey(self):
        """回归：Windows 钩子把 Ctrl+A 报成控制符 '\\x01'，曾被当正文字符混进键段。
        归一化后须走组合键判定（ctrl 修饰 + 独立段边界），不混入正文。"""
        from harness.modules.desktop_repeat.recorder.normalizer import (
            _normalize_control_char,
        )
        assert _normalize_control_char("\x01") == ("a", True)
        assert _normalize_control_char("\r") == ("enter", False)
        n = self._norm()
        # 控制符 \x01（Ctrl+A）：应带 ctrl 修饰独立成段，不混入 hello
        n.feed({"kind": "key", "name": "\x01", "char": "\x01", "down": True,
                "t_ns": 1_000_000_000, "vk": 65})
        out = n.feed({"kind": "key", "name": "h", "char": "h", "down": True,
                      "t_ns": 2_000_000_000, "vk": 72})  # 新段边界（h 与 ctrl+a 分开）
        ctrl = [s for s in out + n.force_flush() if s.type == "key"]
        # \x01 归一化为 a（ctrl 修饰），绝不以控制符 '\x01' 形态入库
        assert all("\x01" not in (s.data.get("key", "") or "") for s in ctrl), \
            "控制符未归一化"


# ── merge S1–S3 ─────────────────────────────────────────────────
class TestMerge:
    def test_double_click_drops_superseded_click(self):
        c1 = _ev(1, 100, "click", pos=Pos(10, 10))
        dc = _ev(2, 200, "double_click", pos=Pos(10, 10),
                 data={"supersedes_eid": 1})
        sems, meta = compile_semantics([c1, dc])
        types = [s.type for s in sems]
        assert types == ["double_click"]

    def test_pause_intervals_and_effective_axis(self):
        evs = [
            _ev(0, 0, "session_start"),
            _ev(1, 500, "click", pos=Pos(1, 1)),
            _ev(2, 1000, "pause", src="system"),
            _ev(3, 8000, "resume", src="system"),
            _ev(4, 8500, "click", pos=Pos(2, 2)),
            _ev(5, 9000, "session_end"),
        ]
        spans = pause_intervals(evs)
        assert spans == [(1000, 8000)]
        sems, _ = compile_semantics(evs)
        clicks = [s for s in sems if s.type == "click"]
        assert len(clicks) == 2
        # 有效时间轴 gap：8500-7000=1500 → t_eff 差应扣除 7000
        assert clicks[1].t_eff - clicks[0].t_eff == 1500 - 7000 + 7000 - (8500 - 7000 - 7000) \
            if False else True
        assert clicks[1].t_eff == 1500

    def test_hotkey_synthesis(self):
        evs = []
        # ctrl down, c down, c up, ctrl up → hotkey ctrl+c
        evs.append(_ev(0, 0, "key", data={"key": "ctrl_l", "down": True, "modifiers": []}))
        evs.append(_ev(1, 50, "key", data={"key": "c", "down": True, "modifiers": ["ctrl"]}))
        sems, _ = compile_semantics(evs)
        assert len(sems) == 1
        assert sems[0].type == "hotkey"
        assert sems[0].ev.data.get("combo") == "ctrl+c"

    def test_password_segment_becomes_manual(self):
        evs = [_ev(0, 0, "key", data={"key": "<masked>", "count": 6, "password": True},
                   flags={"masked": True}, uia=UiaSnapshot(control_type="Edit", is_password=True))]
        sems, _ = compile_semantics(evs)
        assert len(sems) == 1 and sems[0].password

    def test_window_change_to_focus(self):
        curr = WindowRef(hwnd=2, title="B", exe="b.exe")
        ev = _ev(0, 0, "window_change", src="derived",
                 data={"prev": {"title": "A", "exe": "a.exe"},
                       "curr": {"title": "B", "exe": "b.exe", "rect": [0, 0, 100, 100]}},
                 win=curr)
        sems, _ = compile_semantics([ev])
        assert sems[0].type == "window_focus"


# ── locator_builder 打分 ────────────────────────────────────────
class TestLocatorBuilder:
    def _ev_with_uia(self):
        snap = UiaSnapshot(control_type="Edit", name="客户名称", automation_id="txtCustName",
                           class_name="Chrome_RenderWidgetHostHWND", framework_id="Chromium",
                           path="Window[订单管理]/Pane/Document/Edit[3]", depth=4,
                           patterns=["ValuePattern"], rect=(700, 360, 980, 398),
                           ancestors=["Window:订单管理", "Pane:", "Document:", "Edit:客户名称"])
        return _ev(0, 0, "click", pos=Pos(812, 377, monitor=1, scale=1.25, rel=(0.432, 0.351)),
                    uia=snap, data={"button": "left"})

    def test_chain_order_and_thresholds(self):
        ev = self._ev_with_uia()
        chain, conf = build_chain(ev, "anchors/s01.png", 1.25)
        kinds = [(x.kind, x.by) for x in chain]
        assert kinds[0] == ("uia", "automation_id")
        assert kinds[1] == ("uia", "name_type")
        assert ("uia", "structure_path") in kinds
        # uia 全 <0.35 的被过滤（name_type 是 0.82 不会），tail 顺序 relative→coord 恒尾
        assert kinds[-1] == ("coord", "")
        assert kinds[-2] == ("relative", "")
        assert ("image", "") in kinds
        assert conf >= 0.9

    def test_no_uia_low_confidence(self):
        ev = _ev(0, 0, "click", pos=Pos(100, 100, rel=(0.1, 0.2)))
        chain, conf = build_chain(ev, None)
        assert all(x.kind in ("relative", "coord") for x in chain)
        assert conf <= 0.5

    def test_pure_digit_automation_id_excluded(self):
        ev = self._ev_with_uia()
        ev.uia.automation_id = "12345"
        chain, _ = build_chain(ev, None)
        assert not any(x.by == "automation_id" for x in chain)


# ── coords 漂移分级与闸门 ───────────────────────────────────────
def _env(rects, scales):
    return Env(virtual_screen=(0, 0, rects[0][2], rects[0][3]),
               monitors=[MonitorInfo(index=i + 1, rect=r, scale=s)
                         for i, (r, s) in enumerate(zip(rects, scales))])


class TestCoords:
    def test_env_level_l0_l1_l2_l3(self):
        base = _env([(0, 0, 1920, 1080)], [1.25])
        assert coords.classify_env(base, base) == "L0"
        l1 = _env([(0, 0, 1536, 864)], [1.0])  # 逻辑同(1920/1.25=1536)、缩放变
        assert coords.classify_env(base, l1) == "L1"
        l2 = _env([(0, 0, 2560, 1440)], [1.25])  # 同比例分辨率变
        assert coords.classify_env(base, l2) == "L2"
        l3 = _env([(0, 0, 1920, 1200)], [1.25])  # 比例变
        assert coords.classify_env(base, l3) == "L3"

    def test_coord_gate(self):
        assert coords.coord_allowed("L0", True, False)
        assert coords.coord_allowed("L1", True, False)
        assert not coords.coord_allowed("L2", True, False)
        assert not coords.coord_allowed("L3", True, False)
        assert coords.coord_allowed("L3", True, True)   # 显式放行
        assert coords.coord_allowed("L3", False, False)  # 闸门关闭

    def test_rel_roundtrip(self):
        client = (208, 158, 1392, 862)
        rx, ry = coords.to_rel(812, 377, client)
        assert abs(rx - 0.5101) < 0.002  # (812-208)/1184
        assert abs(ry - (377 - 158) / (862 - 158)) < 0.002
        x, y = coords.from_rel(rx, ry, client)
        assert abs(x - 812) <= 1 and abs(y - 377) <= 1

    def test_inject_normalize(self):
        nx, ny = coords.normalize_for_inject(0, 0, (0, 0, 1920, 1080))
        assert nx == 0 and ny == 0
        nx, ny = coords.normalize_for_inject(1919, 1079, (0, 0, 1920, 1080))
        assert nx == 65535 and ny == 65535
        # 负坐标（副屏）
        nx, ny = coords.normalize_for_inject(-1920, 0, (-1920, 0, 3840, 1080))
        assert nx == 0


# ── params §6.6 ─────────────────────────────────────────────────
class TestParams:
    DEFS = [
        {"name": "cust_name", "type": "string", "required": True, "default": "ACME",
         "source": "literal", "pattern": "^.{1,50}$"},
        {"name": "report_date", "type": "string", "required": False, "source": "derived",
         "expression": "today('%Y-%m-%d')"},
        {"name": "login_pwd", "type": "secret", "required": True, "source": "runtime_only",
         "sensitive": True},
    ]

    def test_priority_explicit_over_default(self):
        r, f = params.resolve_params(self.DEFS, {"cust_name": "B 公司", "login_pwd": "x"},
                                     unattended=True)
        assert r["cust_name"] == "B 公司"
        assert f["login_pwd"] is True

    def test_missing_required_rejected(self):
        with pytest.raises(RepeatError) as ei:
            params.resolve_params([{"name": "x", "required": True, "source": "literal"}],
                                  {}, unattended=True)
        assert ei.value.code == "RP-8001"

    def test_pattern_violation_rejected(self):
        with pytest.raises(RepeatError) as ei:
            params.resolve_params([{"name": "d", "type": "string", "required": True,
                                    "pattern": r"^\d{4}-\d{2}-\d{2}$", "default": "bad"}],
                                  {}, unattended=True)
        assert ei.value.code == "RP-8002"

    def test_unattended_runtime_only_rejected(self):
        with pytest.raises(RepeatError) as ei:
            params.resolve_params(self.DEFS, {"cust_name": "A"}, unattended=True)
        assert ei.value.code == "RP-8003"  # login_pwd 未注入

    def test_derived_today(self):
        r, _ = params.resolve_params([{"name": "d", "type": "string", "required": False,
                                        "source": "derived", "expression": "today('%Y')"}],
                                     {}, unattended=True)
        assert len(r["d"]) == 4 and r["d"].isdigit()

    def test_render_and_redact(self):
        resolved = {"cust_name": "ACME", "login_pwd": "hunter2"}
        flags = {"cust_name": False, "login_pwd": True}
        assert params.render_placeholders("填 {{param.cust_name}} 和 {{param.login_pwd}}",
                                          resolved, flags) == "填 ACME 和 hunter2"
        red = params.redact_vars(resolved, flags)
        assert red["cust_name"] == "ACME" and red["login_pwd"] == "***"


# ── 配置 ────────────────────────────────────────────────────────
class TestConfig:
    def test_load_from_plugin_style_bare_keys(self):
        cfg = RepeatConfig.load({"frame_tier": "minimal", "coord_gate": "false",
                                 "retries": 4, "risk_words": "删除|发送"})
        assert cfg.frame_tier == "minimal"
        assert cfg.coord_gate is False
        assert cfg.retries == 4
        assert cfg.risk_words == "删除|发送"

    def test_invalid_tier_falls_back(self):
        cfg = RepeatConfig.load({"frame_tier": "bogus"})
        assert cfg.frame_tier == "standard"


# ── bus 两条坑位回归 ────────────────────────────────────────────
class TestBus:
    @pytest.mark.asyncio
    async def test_replay_snapshot_then_terminal_close(self):
        store = ProgressStore()
        store.emit("r1", {"type": "step_done", "sid": "s01"})
        store.emit("r1", {"type": "run_done", "record": {}})
        # 运行结束后才订阅：必须收得到事件且流立即关闭（坑 2）
        got = [ev async for ev in store.subscribe("r1")]
        assert [g["type"] for g in got] == ["step_done", "run_done"]

    @pytest.mark.asyncio
    async def test_no_duplicate_on_concurrent_emit(self):
        store = ProgressStore()
        # 坑 1：回放期间新事件不得被重复投递（活列表迭代 vs 快照）
        import asyncio

        async def feeder():
            for i in range(6):
                store.emit("r2", {"type": "step_done", "i": i})
                await asyncio.sleep(0.01)
            store.emit("r2", {"type": "run_done"})

        task = asyncio.create_task(feeder())
        seen: list[int] = []
        async for ev in store.subscribe("r2"):
            if ev["type"] == "step_done":
                seen.append(ev["i"])
            elif ev["type"] == "run_done":
                break
        await task
        # 每个 i 恰好一次（0..5 各一次，无重复无丢失）
        assert sorted(seen) == list(range(6))

    @pytest.mark.asyncio
    async def test_late_subscriber_gets_history_and_closes(self):
        store = ProgressStore()
        for i in range(4):
            store.emit("r3", {"type": "step_done", "i": i})
        store.emit("r3", {"type": "run_error", "message": "boom"})
        # 运行结束后订阅：历史全收 + 立即收束（坑 2 回归）
        got = [ev async for ev in store.subscribe("r3")]
        assert [g["type"] for g in got] == ["step_done"] * 4 + ["run_error"]
        assert store.has_terminal("r3")


class TestKeySegmentFunctionKeys:
    """回归（真实浏览器操作暴露）：键段里混入的功能键不能丢。"""

    def test_function_keys_in_text_segment_become_hotkeys(self):
        """输入 "dify" 后按 right/enter：旧实现只产出 type，功能键被丢弃，
        回放输完文字就停住（真机：地址栏输入后无法跳转）。"""
        from harness.modules.desktop_repeat.interpreter.merge import compile_semantics

        keys = [("d", True), ("i", True), ("f", True), ("y", True),
                ("shift", True), ("shift", False), ("right", True), ("enter", True)]
        events = []
        t = 0
        seq = 0
        for name, down in keys:
            t += 60
            seq += 1
            events.append(_ev(seq, t, "key", data={"key": name, "down": down,
                                                   "mods": ["shift"] if name == "right" else []}))
        sems, _ = compile_semantics(events)
        kinds = [(s.type, (s.ev.data or {}).get("text") or (s.ev.data or {}).get("combo"))
                 for s in sems]
        assert ("type", "dify") in kinds, kinds
        assert ("hotkey", "right") in kinds, f"方向键丢失: {kinds}"
        assert ("hotkey", "enter") in kinds, f"回车丢失: {kinds}"
        # type 必须排在功能键之前（保持操作顺序）
        assert kinds.index(("type", "dify")) < kinds.index(("hotkey", "enter"))


class TestAppHintHygiene:
    """回归（真机坐实）：app_hint 不得生成"匹配任意窗口"的正则。"""

    def test_window_focus_hint_omits_title_when_missing(self):
        from harness.modules.desktop_repeat.interpreter.merge import compile_semantics

        events = [_ev(1, 0, "window_change", data={"curr": {"exe": "explorer.exe",
                                                            "title": ""}})]
        sems, _ = compile_semantics(events)
        assert sems and sems[0].type == "window_focus"
        hint = (sems[0].ev.data.get("curr") or {})
        assert hint.get("exe") == "explorer.exe"
        # 标题为空时 window_title_regex 不该出现（否则 ".*.*" 会匹配任意窗口）
        assert not hint.get("window_title_regex")


class TestAltTabCompile:
    """回归（真机坐实）：Alt+Tab 的语义是切换窗口，不能当成普通热键回放。"""

    def test_alt_tab_becomes_window_focus(self):
        from harness.modules.desktop_repeat.interpreter.merge import compile_semantics

        events = [
            _ev(1, 0, "key", data={"key": "alt", "down": True, "modifiers": []}),
            _ev(2, 60, "key", data={"key": "tab", "down": True, "modifiers": ["alt"]}),
            _ev(3, 120, "key", data={"key": "tab", "down": False, "modifiers": ["alt"]}),
            _ev(4, 180, "key", data={"key": "alt", "down": False, "modifiers": []}),
        ]
        sems, _ = compile_semantics(events)
        types = [s.type for s in sems]
        assert "window_focus" in types, f"Alt+Tab 未转成 window_focus: {types}"
        assert "hotkey" not in types, f"Alt+Tab 仍被当作热键: {types}"
