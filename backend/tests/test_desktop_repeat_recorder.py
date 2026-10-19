"""recorder 子进程写盘管线测试（L1：假平台件驱动 _write_stub → 封版 → 校验）。

不装真实全局钩子、不起 UI 主循环、不起 worker 线程——直接喂原始事件 dict 给
normalizer 并同步调 _write_stub，确定性验证 §3.2 采集流水线核心不变式：
- 点击 → win/uia/frames/anchor 产物齐全；
- 双击取代关系（data.supersedes_eid）；
- 暂停区间零采集（INV-R2/R3）；
- 封版 → manifest verify 通过、seq 连续（INV-R1）。

热键合成（pause/resume/长按）与 IME 归并的判定已由 test_desktop_repeat_core
覆盖（同一 normalizer._control_stubs / _key_flush 代码路径），此处不重复。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import harness.modules.desktop_repeat.recorder.runner as rr
from harness.modules.desktop_repeat.config import RepeatConfig
from harness.modules.desktop_repeat.models import Env, MonitorInfo, UiaSnapshot, WindowRef
from harness.modules.desktop_repeat.store.bundle import BundleReader, new_session_id


def _png(color: str = "white", size: tuple[int, int] = (160, 120)) -> bytes:
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(color: str = "white", size: tuple[int, int] = (160, 120)) -> bytes:
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=80)
    return buf.getvalue()


class FakeCapture:
    def __init__(self) -> None:
        self.started = False

    def available(self) -> bool:
        return True

    def start(self, on_event: object | None = None) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def stats(self) -> dict[str, int]:
        return {"dropped": 0, "slow": 0, "qsize": 0}

    def queue_size(self) -> int:
        return 0

    def drain(self, max_items: int = 0) -> list[dict]:
        return []


class FakeInspector:
    def __init__(self) -> None:
        self.snap = UiaSnapshot(control_type="Button", name="保存", automation_id="btnSave",
                                path="Window[测试应用主窗口]/Button[1]", depth=2,
                                patterns=["InvokePattern"], rect=(700, 360, 780, 390))

    def available(self) -> bool:
        return True

    def warmup(self) -> None:
        pass

    def element_token_from_point(self, x: int, y: int, timeout_ms: int):
        return 7, self.snap

    def focused_element(self, timeout_ms: int = 400):
        s = UiaSnapshot(control_type="Edit", name="客户名称", automation_id="txtCust",
                        is_editable=True, patterns=["ValuePattern"],
                        rect=(300, 200, 600, 230))
        return 8, s

    def read_value(self, element: object, timeout_ms: int = 500) -> str:
        return ""

    def release(self, token: int) -> None:
        pass

    def shutdown(self) -> None:
        pass


class FakeWindowsMgr:
    def __init__(self) -> None:
        self.win = WindowRef(hwnd=42, title="测试应用主窗口", cls="Frame", pid=1, exe="app.exe",
                             rect=(0, 0, 1200, 900), client=(10, 40, 1190, 860))

    def foreground(self) -> WindowRef:
        return self.win

    def alive(self, hwnd: int, timeout_ms: int = 5000) -> bool:
        return True


class FakeEnvProv:
    def snapshot(self) -> Env:
        return Env(os="Test", virtual_screen=(0, 0, 1920, 1080),
                   monitors=[MonitorInfo(index=1, rect=(0, 0, 1920, 1080), scale=1.0)])

    def apply_dpi_awareness(self) -> None:
        pass

    def desktop_name(self) -> str:
        return "Default"


class FakeGrab:
    def __init__(self) -> None:
        self._i = 0

    def grab(self, rect: object = None) -> bytes:
        self._i += 1
        return _png("white" if self._i % 2 else "black")

    def grab_jpeg(self, rect: object = None, quality: int = 80) -> bytes:
        return _jpeg()

    def grab_gray(self, rect: object = None):
        raise RuntimeError("未用到 numpy 路径")

    def close(self) -> None:
        pass


@pytest.fixture()
def recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sid = new_session_id()
    sdir = tmp_path / sid
    (sdir / "runtime").mkdir(parents=True)
    cfg = RepeatConfig.load({"frame_tier": "standard", "countdown_s": 0})
    boot = {
        "session_id": sid,
        "config": cfg.to_public_dict() | {"queue_capacity": 256, "hotkeys": cfg.hotkeys},
        "record": {"label": "录制管线测试"},
        "env": FakeEnvProv().snapshot().to_dict(),
    }
    (sdir / "runtime" / "bootstrap.json").write_text(json.dumps(boot, ensure_ascii=False),
                                                     encoding="utf-8")
    monkeypatch.setattr(rr, "make_capture", FakeCapture)
    monkeypatch.setattr(rr, "make_inspector", FakeInspector)
    monkeypatch.setattr(rr, "make_windows", FakeWindowsMgr)
    monkeypatch.setattr(rr, "make_grabber", FakeGrab)
    r = rr.Recorder(sdir)
    r.cfg.record_banner_s = 0
    r._mono0 = time.perf_counter_ns()
    r._epoch0 = time.time_ns()
    return r, sdir, sid


def _feed(r: rr.Recorder, raw: dict) -> None:
    for stub in r.normalizer.feed(raw):
        r._write_stub(stub)


def _click(r: rr.Recorder, x: int, y: int, t_ns: int, button: str = "left") -> None:
    _feed(r, {"kind": "button", "x": x, "y": y, "button": button, "pressed": True, "t_ns": t_ns})
    _feed(r, {"kind": "button", "x": x, "y": y, "button": button, "pressed": False,
              "t_ns": t_ns + 80_000_000})


def test_click_probes_and_finalizes(recorder):
    r, sdir, sid = recorder
    r._set_state("recording")
    r._emit_system("session_start", {"env": r.env.to_dict(), "config": {}})

    t = int(time.perf_counter_ns())
    _click(r, 740, 375, t)

    # _shutdown_final 内部自会补 session_end + 封版，调用方不再重复写
    with pytest.raises(SystemExit) as se:
        r._shutdown_final("api")
    assert se.value.code == 0

    reader = BundleReader(sdir)
    doc = reader.session()
    assert doc["status"] == "ready"
    assert doc["label"] == "录制管线测试"
    events, truncated = reader.events()
    assert truncated is False
    types = [e.type for e in events]
    assert types == ["session_start", "click", "session_end"]  # 末条由收尾产出（唯一）
    assert types.count("session_end") == 1
    ev = events[1]
    assert ev.seq == 1 and events[0].seq == 0 and events[2].seq == 2  # INV-R1
    assert ev.data["button"] == "left"
    assert ev.win is not None and ev.win.exe == "app.exe"
    assert ev.uia is not None and ev.uia.automation_id == "btnSave"
    assert ev.pos is not None and ev.pos.x == 740 and ev.pos.y == 375
    assert ev.pos.rel is not None  # 客户区比例已计算（供 relative 定位）
    frames = ev.data.get("frames") or []
    assert len(frames) >= 2 and all(str(f).startswith("frames/") for f in frames)
    # 锚点（元素 rect ≥ 40×24 满足条件）
    assert str(ev.data.get("anchor_prov", "")).startswith("anchors/")
    reader.verify()  # manifest 通过


def test_double_click_supersedes_and_pause_zero_events(recorder):
    r, sdir, sid = recorder
    r._set_state("recording")
    base = int(time.perf_counter_ns())
    _click(r, 100, 100, base)               # first click
    _click(r, 100, 100, base + 300_000_000)  # 300ms later → double_click 取代
    # 暂停：进入 paused 后事件短路（INV-R2/R3：recording 之外无输入事件、paused 仅控制事件）
    r._do_pause(auto=False, t_ns=base + 1_000_000_000)
    _click(r, 999, 999, base + 2_000_000_000)   # 暂停中点击 → 不入库
    r._do_resume(base + 3_000_000_000)
    _click(r, 50, 50, base + 4_000_000_000)     # 恢复后可入库
    r.writer.flush()

    from harness.modules.desktop_repeat.store.jsonl import read_events
    events, _ = read_events(sdir / "events.jsonl")
    types = [e.type for e in events]
    assert "double_click" in types
    # 首次 click 仍入库（取代发生在编译期）；暂停区间内（999,999）无事件
    clicks = [e for e in events if e.type == "click"]
    assert len(clicks) == 2
    assert all(e.pos is not None and (e.pos.x, e.pos.y) in [(100, 100), (50, 50)] for e in clicks)
    # pause/resume 控制事件成对且区间内无任何输入事件
    assert "pause" in types and "resume" in types
    ip, ir = types.index("pause"), types.index("resume")
    assert not [t for t in types[ip + 1:ir] if t in ("click", "key", "drag", "scroll")]


def test_pre_pause_segment_is_flushed_not_dropped(recorder):
    """回归（阶段一 E2E 真实缺陷）：暂停前已缓冲的键段必须先结算入库，
    enter_pause 旧实现直接 clear() 会吞掉「点击→立即输入→马上暂停」的演示内容。"""
    r, sdir, sid = recorder
    r._set_state("recording")
    base = int(time.perf_counter_ns())
    _click(r, 100, 100, base)
    for ch in "abc":
        st = r.normalizer.make_stub("key", data={"key": ch, "down": True,
                                                 "modifiers": [], "_t_ms": base / 1e6})
        r._write_stub(st)  # 键段进 normalizer 缓冲（静默窗未到，不落盘）
    # 马上暂停（键段仍在缓冲内）
    r._do_pause(auto=False, t_ns=base + 50_000_000)
    r._do_resume(base + 100_000_000)
    r.writer.flush()
    from harness.modules.desktop_repeat.store.jsonl import read_events
    events, _ = read_events(sdir / "events.jsonl")
    types = [e.type for e in events]
    keys = [e for e in events if e.type == "key"]
    # a/b/c 三个键必须落盘（暂停前演示内容不丢）
    assert len(keys) == 3, f"暂停前键段被吞: types={types}"
    assert "".join(e.data.get("key", "") for e in keys) == "abc"
    ip, ir = types.index("pause"), types.index("resume")
    # pause 标记在键之后、恢复之前
    assert max(i for i, t in enumerate(types) if t == "key") < ip
    # pause 与 resume 之间不得有输入事件（区间零采集仍成立）
    assert not [t for t in types[ip + 1:ir] if t in ("click", "key", "drag", "scroll")]
    # seq 连续（INV-R1）
    assert [e.seq for e in events] == list(range(len(events)))


def test_keyboard_segment_written(recorder):
    r, sdir, sid = recorder
    r._set_state("recording")
    t = int(time.perf_counter_ns())
    for ch in "abc":
        r._write_stub(r.normalizer.make_stub("key", data={"key": ch, "down": True, "modifiers": [],
                                                          "_t_ms": t / 1e6}))
        t += 30_000_000
    r.writer.flush()
    from harness.modules.desktop_repeat.store.jsonl import read_events
    events, _ = read_events(sdir / "events.jsonl")
    keys = [e for e in events if e.type == "key"]
    assert "".join(e.data.get("key", "") for e in keys) == "abc"


class QueueCapture:
    """可注入原始事件队列的 capture：drain() 取走积压，供 §3.10 backlog 分流测试。"""

    def __init__(self) -> None:
        self._q: list[dict] = []

    def available(self) -> bool:
        return True

    def start(self, on_event: object | None = None) -> None:
        pass

    def stop(self) -> None:
        pass

    def stats(self) -> dict[str, int]:
        return {"dropped": 0, "slow": 0, "qsize": len(self._q)}

    def queue_size(self) -> int:
        return len(self._q)

    def put(self, raw: dict) -> None:
        self._q.append(raw)

    def drain(self, max_items: int = 4096) -> list[dict]:
        out, self._q = self._q[:max_items], self._q[max_items:]
        return out


def test_pause_keeps_backlog_before_backdate_window(recorder):
    """回归（阶段一 E2E run4 真实丢键缺陷）：暂停信号到达时，capture 队列里还压着
    「暂停前」尚未喂入的键。旧实现 enter_pause 之后 bulk-drain 全丢 → 整批演示输入
    消失。修复：按 t_ns 时间戳分流——早于 150ms 回溯窗的先喂入入段再冲刷保留，
    窗内/之后的（可能是热键抖动）才丢弃。"""
    from harness.modules.desktop_repeat.recorder.runner import PAUSE_BACKDATED_NS

    r, sdir, sid = recorder
    r._set_state("recording")
    qcap = QueueCapture()
    r.capture = qcap
    base = int(time.perf_counter_ns())

    def key_down(ch: str, t_ns: int) -> dict:
        return {"kind": "key", "name": ch, "char": ch, "down": True,
                "vk": ord(ch.upper()), "mods": [], "t_ns": t_ns}

    # 暂停前 500ms 打的 "hel"（早于回溯窗 → 必须保留）
    qcap.put(key_down("h", base))
    qcap.put(key_down("e", base + 10_000_000))
    qcap.put(key_down("l", base + 20_000_000))
    # 距暂停仅半个回溯窗（落在 (pause_t-PAUSE_BACKDATED_NS, pause_t] 内 → 抖动，丢弃）
    pause_t = base + 500_000_000
    assert PAUSE_BACKDATED_NS < 500_000_000, "回溯窗比前积压键还早，用例前提不成立"
    jitter_t = pause_t - int(PAUSE_BACKDATED_NS / 2)
    qcap.put(key_down("X", jitter_t))

    r._do_pause(auto=False, t_ns=pause_t)
    r._do_resume(pause_t + 50_000_000)
    r.writer.flush()

    from harness.modules.desktop_repeat.store.jsonl import read_events
    events, _ = read_events(sdir / "events.jsonl")
    keys = [e for e in events if e.type == "key"]
    joined = "".join(e.data.get("key", "") for e in keys)
    # h/e/l 必须保留；窗内抖动 X 必须丢弃
    assert "h" in joined and "e" in joined and "l" in joined, f"暂停前积压键被吞: keys={joined!r}"
    assert "X" not in joined, f"回溯窗内抖动键未被丢弃: keys={joined!r}"
    types = [e.type for e in events]
    ip = types.index("pause")
    # 保留的键必须排在 pause 标记之前（区间零采集）
    key_idx = [i for i, t in enumerate(types) if t == "key"]
    assert all(i < ip for i in key_idx)
    # seq 连续（INV-R1）
    assert [e.seq for e in events] == list(range(len(events)))


def test_key_segment_anchors_focus_control_by_handle(recorder):
    """回归（真机 E2E 缺陷）：键段快照必须按**输入发生时**的焦点句柄采集。

    旧实现在 worker 线程延迟查询当前焦点：后帧采集阻塞 400ms 期间若用户已点下一个
    控件，键入步骤会锚到错误控件（实测锚到按钮，回放把文本写进按钮）。
    """
    r, sdir, sid = recorder
    r._set_state("recording")
    edit_snap = UiaSnapshot(control_type="Edit", automation_id="txtCust",
                            path="Window[Demo]/Edit", depth=2,
                            patterns=["ValuePattern"], is_editable=True,
                            rect=(300, 200, 600, 230))
    btn_snap = UiaSnapshot(control_type="Button", name="Submit", automation_id="btnSubmit",
                           path="Window[Demo]/Button", depth=2, rect=(300, 260, 400, 290))
    seen: dict[str, int] = {}

    def _from_handle(hwnd: int, timeout_ms: int = 400):
        seen["hwnd"] = int(hwnd)
        return 11, edit_snap

    r.inspector.element_token_from_handle = _from_handle  # type: ignore[attr-defined]
    r.inspector.focused_element = lambda timeout_ms=400: (12, btn_snap)  # type: ignore[assignment]

    t = int(time.perf_counter_ns())
    for ch in "abc":
        r.normalizer.feed({"kind": "key", "name": ch, "char": ch, "down": True,
                           "vk": ord(ch.upper()), "mods": [], "focus_hwnd": 4242,
                           "t_ns": t})
        t += 30_000_000
    for stub in r.normalizer.force_flush():
        r._write_stub(stub)
    r.writer.flush()

    from harness.modules.desktop_repeat.store.jsonl import read_events
    events, _ = read_events(sdir / "events.jsonl")
    keys = [e for e in events if e.type == "key"]
    assert "".join(e.data.get("key", "") for e in keys) == "abc"
    assert seen.get("hwnd") == 4242, "未按焦点句柄采集快照"
    assert all(e.uia is not None for e in keys), "键事件丢失控件锚点"
    assert all(e.uia.automation_id == "txtCust" for e in keys), "键事件锚到了错误控件"


def test_key_segment_without_handle_falls_back_to_focused(recorder):
    r, sdir, sid = recorder
    r._set_state("recording")
    snap = UiaSnapshot(control_type="Edit", automation_id="txtFallback", is_editable=True,
                       patterns=["ValuePattern"], path="Window[Demo]/Edit", depth=2)
    r.inspector.focused_element = lambda timeout_ms=400: (21, snap)  # type: ignore[assignment]
    t = int(time.perf_counter_ns())
    r.normalizer.feed({"kind": "key", "name": "x", "char": "x", "down": True,
                       "vk": 88, "mods": [], "t_ns": t})
    for stub in r.normalizer.force_flush():
        r._write_stub(stub)
    r.writer.flush()
    from harness.modules.desktop_repeat.store.jsonl import read_events
    events, _ = read_events(sdir / "events.jsonl")
    keys = [e for e in events if e.type == "key"]
    assert keys and keys[0].uia is not None
    assert keys[0].uia.automation_id == "txtFallback"


def test_key_segment_falls_back_to_focused_for_single_hwnd_app(recorder):
    """回归（真实 Chrome 录制暴露）：Chrome/Electron 是单 HWND，控件句柄只能
    定位到顶层窗口根 Pane。旧实现直接把它当锚点，导致键入步骤锚到
    "Pane[... Google Chrome]"，回放时文字落不进搜索框。
    """
    r, sdir, sid = recorder
    r._set_state("recording")
    win_root = UiaSnapshot(control_type="Pane", name="百度一下，你就知道 - Google Chrome",
                           path="Pane[桌面 1]/Pane[百度一下，你就知道 - Google Chrome]",
                           depth=2, rect=(0, 0, 1920, 1080))
    edit = UiaSnapshot(control_type="Edit", name="百度搜索", automation_id="",
                       path="Pane[桌面 1]/Pane[百度一下]/Document/Edit", depth=6,
                       patterns=["ValuePattern"], is_editable=True,
                       rect=(600, 300, 900, 340))

    r.inspector.element_token_from_handle = lambda hwnd, timeout_ms=400: (31, win_root)
    r.inspector.focused_element = lambda timeout_ms=400: (32, edit)
    r.inspector.read_value = lambda tok, timeout_ms=300: ""  # type: ignore[assignment]

    t = int(time.perf_counter_ns())
    for ch in "ab":
        r.normalizer.feed({"kind": "key", "name": ch, "char": ch, "down": True,
                           "vk": ord(ch.upper()), "mods": [], "focus_hwnd": 777,
                           "t_ns": t})
        t += 30_000_000
    for stub in r.normalizer.force_flush():
        r._write_stub(stub)
    r.writer.flush()

    from harness.modules.desktop_repeat.store.jsonl import read_events
    events, _ = read_events(sdir / "events.jsonl")
    keys = [e for e in events if e.type == "key"]
    assert keys, "未记录键事件"
    assert all(e.uia is not None for e in keys)
    assert all(e.uia.control_type == "Edit" for e in keys), "未回退到 UIA 焦点控件"
    assert all(e.uia.rect == (600, 300, 900, 340) for e in keys)


def test_key_segment_keeps_handle_snapshot_when_focus_outside_window(recorder):
    """焦点控件矩形不在句柄窗口内 → 视为 worker 滞后抓到的别的控件，保留原快照。"""
    r, sdir, sid = recorder
    r._set_state("recording")
    win_root = UiaSnapshot(control_type="Pane", name="Chrome",
                           path="Pane[桌面 1]/Pane[Chrome]", depth=2,
                           rect=(0, 0, 800, 600))
    other = UiaSnapshot(control_type="Edit", name="别的窗口输入框",
                        path="Pane[桌面 1]/Window[Other]/Edit", depth=3,
                        patterns=["ValuePattern"], is_editable=True,
                        rect=(1200, 800, 1500, 840))
    r.inspector.element_token_from_handle = lambda hwnd, timeout_ms=400: (41, win_root)
    r.inspector.focused_element = lambda timeout_ms=400: (42, other)
    r.inspector.read_value = lambda tok, timeout_ms=300: ""  # type: ignore[assignment]

    t = int(time.perf_counter_ns())
    r.normalizer.feed({"kind": "key", "name": "z", "char": "z", "down": True,
                       "vk": 90, "mods": [], "focus_hwnd": 888, "t_ns": t})
    for stub in r.normalizer.force_flush():
        r._write_stub(stub)
    r.writer.flush()

    from harness.modules.desktop_repeat.store.jsonl import read_events
    events, _ = read_events(sdir / "events.jsonl")
    keys = [e for e in events if e.type == "key"]
    assert keys and keys[0].uia is not None
    assert keys[0].uia.name == "Chrome", "越界焦点控件被误用作锚点"


def test_click_snapshot_discarded_when_rect_excludes_point(recorder):
    """回归（真实百度录制）：UIA 快照与点击坐标不一致时必须丢弃。

    Chrome 切标签后 UIA 树滞后几百毫秒，ElementFromPoint 会返回**上一个页面**
    的控件。真机实录：点击百度搜索框 (635,576)，快照却是 Andy-Harness 的
    聊天框（rect 完全不含该点），回放时在百度窗口里找不到或误命中同名控件。
    """
    r, sdir, sid = recorder
    r._set_state("recording")
    stale = UiaSnapshot(control_type="Edit", name="chat-textarea",
                        automation_id="chat-textarea",
                        path="Pane[桌面 1]/Pane[Andy-Harness]/Edit", depth=6,
                        patterns=["ValuePattern"], rect=(100, 700, 400, 760))
    r.inspector.element_token_from_point = lambda x, y, timeout_ms=300: (51, stale)

    t = int(time.perf_counter_ns())
    _click(r, 635, 576, t)   # 该点不在 stale.rect 内
    r.writer.flush()

    from harness.modules.desktop_repeat.store.jsonl import read_events
    events, _ = read_events(sdir / "events.jsonl")
    clicks = [e for e in events if e.type == "click"]
    assert clicks, "未记录点击"
    assert clicks[0].uia is None, "与坐标不一致的脏快照未被丢弃"
    assert clicks[0].flags.get("uia_stale") is True


def test_click_triggered_window_switch_emits_window_change(recorder):
    """回归（体检 P1-4）：点击引发的窗口切换必须录到 window_change。

    旧实现 _win_context 写事件前抢先更新 _last_fg，随后轮询检测时
    prev==curr → 永不发事件；只有用户 Alt+Tab/点别的窗口才录得到。
    结果"点开对话框→在对话框操作"的流程缺窗口切换锚点与全屏留证帧。
    """
    from harness.modules.desktop_repeat.models import WindowRef

    r, sdir, sid = recorder
    r._set_state("recording")
    w1 = r.windows.win
    w2 = WindowRef(hwnd=43, title="另存为", cls="#32770", pid=1, exe="app.exe",
                   rect=(100, 100, 700, 500), client=(110, 140, 690, 490))
    seq: list[WindowRef] = [w1, w2]

    def fg() -> WindowRef:
        return seq.pop(0) if seq else w2

    r.windows.foreground = fg  # type: ignore[method-assign]

    base = int(time.perf_counter_ns())
    _click(r, 740, 375, base)          # 点击时前台还是 w1
    _click(r, 200, 200, base + 600_000_000)  # 切换后点在 w2 里
    r.writer.flush()

    from harness.modules.desktop_repeat.store.jsonl import read_events
    events, _ = read_events(sdir / "events.jsonl")
    types = [e.type for e in events]
    assert "window_change" in types, f"点击引发的窗口切换未记录: {types}"
    wc = next(e for e in events if e.type == "window_change")
    assert wc.data["curr"]["title"] == "另存为"
    assert wc.data["prev"]["title"] == "测试应用主窗口"
    # seq 连续（INV-R1）
    assert [e.seq for e in events] == list(range(len(events)))


def test_key_events_keep_press_time_and_press_window(recorder):
    """回归（真机 run-092d827a9a s09 根因）：键事件必须用「按下时刻」与「按下时刻的窗口上下文」。

    旧实现两处都取"结算写盘时刻"：键段要等 150ms 防抖 / 1200ms 静默才结算，
    地址栏输完按回车触发导航，页面标题在键段结算**之前**就变了 →
      a) 回车的 t 晚于它自己引发的 window_change（t=15867 > window_change t=15758），
         编译期回车被排到"导航之后"；
      b) 回车的 win/uia 取写盘时刻的前景窗口 → 锚到导航后的页面根 Pane。
    回放时出现"先定位到该页面才允许按键，而该页面要等按键才存在"的因果倒置死锁
    （表现：回车始终没按下、界面停在原地、挂起等人工）。
    """
    from harness.modules.desktop_repeat.models import WindowRef

    r, sdir, sid = recorder
    r._set_state("recording")
    r._emit_system("session_start", {"env": r.env.to_dict(), "config": {}})
    t0 = int(time.perf_counter_ns())

    # 1) 先点一下地址栏：确立"按下时刻"的窗口上下文（点击类事件仍走 _win_context）
    _click(r, 740, 375, t0)
    assert r._last_fg is not None and r._last_fg.title == "测试应用主窗口"

    # 2) 键入 dify + 回车（按下时刻都远早于"导航完成"）
    for i, ch in enumerate("dify"):
        _feed(r, {"kind": "key", "name": ch, "down": True, "vk": 0,
                  "t_ns": t0 + 200_000_000 + i * 90_000_000, "focus_hwnd": 42})
    enter_ns = t0 + 200_000_000 + 4 * 90_000_000
    _feed(r, {"kind": "key", "name": "enter", "down": True, "vk": 13,
              "t_ns": enter_ns, "focus_hwnd": 42})

    # 3) 回车触发导航：键段尚未结算，前景窗口标题已变成目标页面
    r.windows.win = WindowRef(hwnd=42, title="工作室 - Dify - Google Chrome",
                              cls="Chrome_WidgetWin_1", pid=1, exe="chrome.exe",
                              rect=(0, 0, 1200, 900), client=(0, 40, 1190, 860))

    # 4) 键段结算（此刻已是"导航之后"）
    for stub in r.normalizer.force_flush():
        r._write_stub(stub)
    # 轮询检测到窗口变化 → 必须排在按键之后
    r._detect_window_change()

    with pytest.raises(SystemExit):
        r._shutdown_final("api")
    events, _ = BundleReader(sdir).events()
    keys = [e for e in events if e.type == "key"]
    assert [e.data.get("key") for e in keys] == ["d", "i", "f", "y", "enter"]

    enter = keys[-1]
    # 修复前：win 是导航后的页面 → 回车被绑到"回车之后才出现的页面"
    assert enter.win.title == "测试应用主窗口", f"回车锚到了导航后的窗口: {enter.win.title}"
    # 修复前：t 用写盘时刻，比真实按下时刻晚
    want_ms = int((enter_ns - r._mono0) // 1e6)
    assert abs(enter.t - want_ms) <= 5, f"回车时间戳不是按下时刻: {enter.t} != {want_ms}"
    # 因果顺序：window_change（这次导航）必须排在回车之后
    wc = [e for e in events if e.type == "window_change"]
    assert wc, "回车引发的窗口切换未记录"
    assert wc[0].seq > enter.seq, "window_change 被排到了引发它的回车之前（因果倒置）"


def test_worker_crash_sets_error_and_stops(recorder):
    """回归（真机）：采集线程异常不得静默。

    旧实现是裸线程：NameError 直接杀死 worker，队列不再消费，用户继续打字
    却一条事件都写不进去，session 仍报 ready，直到解析时才报"事件流不含
    可执行操作"（真实会话 r-20261019-152517-33fbbb 只剩 3 条系统事件）。
    """
    r, sdir, sid = recorder

    def _boom() -> None:
        raise RuntimeError("boom")

    r._worker_loop_inner = _boom  # type: ignore[method-assign]
    r._worker_loop()
    assert "RuntimeError" in r._worker_error
    assert r._stop.is_set(), "worker 崩溃后未停止录制"
