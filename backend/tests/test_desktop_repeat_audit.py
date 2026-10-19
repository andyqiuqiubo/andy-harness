"""andy-repeat 全方位体检新增用例（2026-10-19）。

这些用例针对既有 154 项假件单测**覆盖不到**的真实执行路径缺陷。断言写成
"应当成立的正确行为"——在修复前应当失败（用于坐实缺陷），修复后转绿。

覆盖：
- A1 RunRecord.to_dict 必须带 vars/checkpoint/attempts_total，否则 finish_run
  把 attempts_total 落 0、checkpoint 落 {}、vars_json 覆盖成 {}（历史详情丢数据）。
- A2 失败阶梯的"放宽重试"（self._relaxed）不得跨步骤泄漏：某步触发 relaxed 后，
  后续步骤的 UIA name 必须回到精确匹配（编辑距离 0），否则会点错近名控件。
- B1 双击去重：编译期必须丢弃被 double_click 取代的首次 click。真实录制流里
  事件 seq 与归一化 eid 会因前置系统事件（session_start 之外的 marker/告警）错位，
  去重不能只靠 seq==supersedes_eid 的巧合。
- B2 前端 i18n：RepeatStudioView 用到的每个 repeat.* 键，zh/en 两份词典都必须存在。
"""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path

import pytest

from harness.modules.desktop_repeat.bus import ProgressStore, Reporter
from harness.modules.desktop_repeat.config import RepeatConfig
from harness.modules.desktop_repeat.executor.runner import RepeatRunner
from harness.modules.desktop_repeat.models import (
    Attempt,
    Hit,
    LocatorSpec,
    Miss,
    RunRecord,
    StepDef,
    StepRun,
    WindowRef,
)
from harness.modules.desktop_repeat.repository import RepeatRepository
from harness.modules.desktop_repeat.safety_gate import SafetyGate


# ── A1：RunRecord 序列化完整 + 真库 finish_run 落库 ─────────────────
class TestRunRecordPersistenceFidelity:
    def test_to_dict_carries_vars_checkpoint_attempts(self):
        rec = RunRecord(run_id="R1", workflow_id="wf1", status="succeeded")
        rec.vars = {"cust": "ACME", "token": "***"}
        rec.checkpoint = {"index": 2, "completed": ["s01", "s02"]}
        rec.attempts_total = 3
        rec.steps = [StepRun(sid="s01", status="done", attempts=[Attempt(kind="uia", result="hit")])]
        d = rec.to_dict()
        # 修复前：这三个键根本不在 to_dict 里 → finish_run 读默认值
        assert d.get("vars") == {"cust": "ACME", "token": "***"}
        assert d.get("checkpoint") == {"index": 2, "completed": ["s01", "s02"]}
        assert d.get("attempts_total") == 3

    def test_finish_run_with_real_record_keeps_stats(self, tmp_path: Path):
        from harness.infra.database import Database

        repo = RepeatRepository(Database(db_path=str(tmp_path / "r.db")))
        repo.setup()
        repo.insert_run("R1", "wf1", {
            "status": "running", "vars": {"cust": "ACME"},
            "graph_sha256": "ab" * 32, "started_at": "t0",
        }, steps_total=1)

        rec = RunRecord(run_id="R1", workflow_id="wf1", status="succeeded",
                        total_steps=1, attempts_total=4)
        rec.vars = {"cust": "ACME"}
        rec.checkpoint = {"index": 1}
        rec.steps = [StepRun(sid="s01", status="done", hit_index=0, hit_kind="uia")]
        rec.finished_at = "t1"
        repo.finish_run("R1", rec)

        got = repo.get_run("R1")
        # 修复前：finish_run 从 to_dict 取不到这三个字段 → 被写回默认值
        assert got["attempts_total"] == 4, "重试次数被静默清零"
        assert got["vars"] == {"cust": "ACME"}, "参数（脱敏）被覆盖成空"
        assert got["checkpoint"] == {"index": 1}, "断点数据丢失"


# ── A2：_relaxed 不得跨步骤泄漏 ────────────────────────────────────
class _RelaxLoggingInspector:
    """记录每次 element_by_spec 收到的 (value, relaxed)；第 s01 前 fail_times 次判 miss。"""

    def __init__(self, miss_value: str, fail_times: int) -> None:
        self.miss_value = miss_value
        self.fail_times = fail_times
        self.n_miss = 0
        self.seen: list[tuple[str, bool]] = []

    def available(self) -> bool:
        return True

    def warmup(self) -> None:
        pass

    def element_by_spec(self, spec, scope, timeout_ms):
        # name_type 的 value 是 dict；按"名字是否含标记字母"判定，
        # 同时记录每次调用时该 spec 对象的 relaxed 状态（泄漏检测核心）。
        val = str(spec.value)
        tag = "A" if "'A'" in val or '"A"' in val else "B"
        self.seen.append((tag, bool(getattr(spec, "relaxed", False))))
        if tag == self.miss_value and self.n_miss < self.fail_times:
            self.n_miss += 1
            return Miss(kind="uia", reason="not_found", elapsed_ms=1)
        return Hit(rect=(100, 100, 180, 130), center=(140, 115),
                   element=f"tok:{val}", kind=spec.kind, score=0.9,
                   elapsed_ms=1, diagnostics={"by": spec.by})

    def is_editable(self, element, timeout_ms: int = 500) -> bool:
        return True

    def read_value(self, element, timeout_ms: int = 500):
        return None

    def set_value(self, element, text, timeout_ms: int = 1000) -> bool:
        return True

    def invoke(self, element, timeout_ms: int = 1000) -> bool:
        return True

    def toggle(self, element) -> bool:
        return True

    def select_item(self, element) -> bool:
        return False

    def legacy_do_default(self, element) -> bool:
        return False

    def focus_element(self, element) -> bool:
        return True

    def patterns_of_element(self, token) -> list[str]:
        return ["InvokePattern"]

    def snapshot_of_token(self, token, timeout_ms: int = 600):
        return None

    def release(self, token) -> None:
        pass

    def shutdown(self) -> None:
        pass


class _FakeInj:
    def click(self, x, y, button="left", count=1):
        return 1

    def move(self, x, y):
        return 1

    def press(self, button, down, x=None, y=None):
        return 1

    def scroll(self, x, y, dx, dy):
        return 1

    def type_unicode(self, text):
        return len(text) or 1

    def hotkey(self, keys):
        return len(keys)


class _FakeWinA:
    def __init__(self):
        self.win = WindowRef(hwnd=1, title="App", exe="app.exe",
                             rect=(0, 0, 1000, 800), client=(0, 0, 1000, 800))

    def foreground(self):
        return self.win

    def find_window(self, hints):
        return self.win

    def focus(self, win):
        return True

    def restore(self, win):
        pass

    def alive(self, hwnd, timeout_ms=5000):
        return True

    def is_enabled(self, hwnd):
        return True

    def enumerate(self, pid=None):
        return [self.win]


class _FakeEnvA:
    def snapshot(self):
        from harness.modules.desktop_repeat.models import Env, MonitorInfo
        return Env(os="T", virtual_screen=(0, 0, 1920, 1080),
                   monitors=[MonitorInfo(index=1, rect=(0, 0, 1920, 1080), scale=1.0)])

    def graphics_available(self):
        return True

    def desktop_name(self):
        return "Default"

    def is_elevated(self):
        return False


def _step(sid: str, value: str) -> dict:
    s = StepDef(sid=sid, action="click", label=sid, app_hint={"exe": "app.exe"},
                locators=[LocatorSpec(kind="uia", by="name_type",
                                      value={"name": value, "control_type": "Button"},
                                      confidence=0.9)],
                params={}, risk="write",
                policy={"on_fail": "suspend_manual", "retries": 2, "backoff_ms": 10})
    return s.to_dict()


def _graph(steps: list[dict]) -> dict:
    from tests.repeat_fakes import base_env
    nodes = [{"id": "n0", "type": "start", "data": {"params": []}},
             {"id": "nE", "type": "end", "data": {}}]
    edges = []
    prev = "n0"
    for i, sd in enumerate(steps):
        nid = f"g{i}"
        nodes.append({"id": nid, "type": "gui", "data": sd})
        edges.append({"id": f"e{prev}{nid}", "source": prev, "target": nid})
        prev = nid
    edges.append({"id": f"e{prev}nE", "source": prev, "target": "nE"})
    return {"nodes": nodes, "edges": edges,
            "meta": {"env_base": base_env().to_dict()}}


@pytest.mark.asyncio
async def test_relaxed_flag_does_not_leac_to_next_step():
    insp = _RelaxLoggingInspector(miss_value="A", fail_times=1)
    # s01 定位值 "A" 先 miss 一次 → 触发放宽重试（self._relaxed=True）→ 第二次命中
    # s02 定位值 "B" 应回到精确匹配（relaxed=False）
    graph = _graph([_step("s01", "A"), _step("s02", "B")])
    cfg = RepeatConfig.load({"retries": 2, "retry_backoff_ms": 5, "human_takeover_detect": False})
    r = RepeatRunner(
        workflow_id="wf", graph=graph, resolved={}, flags={}, cfg=cfg,
        inspector=insp, injector=_FakeInj(), windows=_FakeWinA(), env_provider=_FakeEnvA(),
        gate=SafetyGate(mode="auto"), run_id="run-a2",
        reporter=Reporter("run-a2", ProgressStore()), emergency_stop=threading.Event(),
        takeover=__import__("harness.modules.desktop_repeat.executor.pacing",
                            fromlist=["NullTakeover"]).NullTakeover())
    await r.run()
    b_calls = [relaxed for (val, relaxed) in insp.seen if val == "B"]
    assert b_calls, "s02 未被解析"
    # 修复前：s02 也被标 relaxed=True（泄漏）
    assert not any(b_calls), "放宽匹配跨步骤泄漏：后续步骤的 name 必须精确匹配"


# ── B1：双击去重不得依赖 seq==eid 巧合 ─────────────────────────────
def test_double_click_dedup_with_offset_events():
    from harness.modules.desktop_repeat.interpreter.pipeline import compile_session
    from harness.modules.desktop_repeat.models import Pos, RecEvent, UiaSnapshot

    win = WindowRef(hwnd=1, title="App", exe="app.exe", rect=(0, 0, 1200, 900),
                    client=(10, 40, 1190, 860))
    snap = UiaSnapshot(control_type="Button", name="保存", automation_id="bSave",
                       path="Window/App/Button[1]", depth=2, rect=(700, 360, 780, 390))

    def click(seq: int, t: int, eid: int):
        return RecEvent(seq=seq, t=t, ts=1760000000000 + t, type="click",
                        pos=Pos(x=740, y=375, rel=(0.5, 0.5)), win=win, uia=snap,
                        data={"button": "left", "eid": eid})

    def dbl(seq: int, t: int, eid: int, sup_eid: int):
        return RecEvent(seq=seq, t=t, ts=1760000000000 + t, type="double_click",
                        pos=Pos(x=740, y=375, rel=(0.5, 0.5)), win=win, uia=snap,
                        data={"button": "left", "eid": eid, "supersedes_eid": sup_eid})

    # 模拟真实错位：session_start(0) + 一个 marker(1) 让后续 seq 比 eid 偏移，
    # 首次 click seq=2 / eid=1；double_click supersedes_eid=1。
    events = [
        RecEvent(seq=0, t=0, ts=1760000000000, type="session_start", src="system", data={}),
        RecEvent(seq=1, t=10, ts=1760000000010, type="marker", src="marker",
                 data={"label": "step", "note": ""}),
        click(seq=2, t=100, eid=1),
        dbl(seq=3, t=400, eid=2, sup_eid=1),
        RecEvent(seq=4, t=500, ts=1760000000500, type="session_end", src="system", data={}),
    ]
    res = compile_session(events, session_id="s-offset")
    types = [s.action for s in res.steps if s.action not in ("marker",)]
    # 修复前：click 因 seq(2)!=supersedes_eid(1) 未被丢弃 → 多出一次 click
    assert types == ["double_click"], f"双击取代失效，产生多余步骤: {types}"


# ── A3：作用域标题"假确认"（P0-4）——exe 兜底命中错误窗口必须拒写 ─────
@pytest.mark.asyncio
async def test_exe_fallback_wrong_window_blocks_write():
    """find_window 四级放宽落到 exe 兜底、返回标题不匹配的窗口时，
    写操作必须以 RP-4002 拒绝，而不是"假确认"放行（错误窗口里点错还报成功）。"""
    import re

    class FallbackWindows(_FakeWinA):
        """忠实模拟真实 find_window：标题正则不中 → exe 兜底返回另一窗口。"""

        def __init__(self) -> None:
            super().__init__()
            self.other = WindowRef(hwnd=99, title="无关窗口", exe="app.exe",
                                   rect=(0, 0, 1000, 800), client=(0, 0, 1000, 800))

        def find_window(self, hints):
            pat = str(hints.get("window_title_regex", "") or "")
            if pat:
                try:
                    if re.search(pat, self.win.title):
                        return self.win
                except re.error:
                    pass
                return self.other  # 兜底：同 exe 的无关窗口
            return self.win

        def focus(self, win):
            return True

    s = StepDef(sid="s01", action="click", label="s01",
                app_hint={"exe": "app.exe", "window_title_regex": ".*目标窗口.*"},
                locators=[LocatorSpec(kind="coord", pos=(10, 10), confidence=0.25)],
                risk="write",
                policy={"on_fail": "suspend_manual", "retries": 0, "backoff_ms": 1})
    graph = _graph([s.to_dict()])
    cfg = RepeatConfig.load({"retries": 0, "coord_gate": False})
    r = RepeatRunner(
        workflow_id="wf", graph=graph, resolved={}, flags={}, cfg=cfg,
        inspector=_RelaxLoggingInspector("X", 0), injector=_FakeInj(),
        windows=FallbackWindows(), env_provider=_FakeEnvA(),
        gate=SafetyGate(mode="interactive", approval_policy="never"),
        run_id="run-a3", reporter=Reporter("run-a3", ProgressStore()),
        emergency_stop=threading.Event(),
        takeover=__import__("harness.modules.desktop_repeat.executor.pacing",
                            fromlist=["NullTakeover"]).NullTakeover())
    await r.run()
    # 修复前：_scope_title_ok 恒 True → 放行 → succeeded（假成功）
    assert r._scope_title_ok is False, "exe 兜底命中错误窗口却被确认"
    assert r.rec.steps[0].code == "RP-4002", f"未按作用域契约拒绝: {r.rec.steps[0].code}"


# ── A4：热键定位链耗尽必须降级为焦点注入（真机 s09 因果倒置死锁）────────
class _AlwaysMissInspector(_RelaxLoggingInspector):
    """模拟"锚点被绑到按键按下之后才出现的页面"：UIA 定位链永远搜不到。"""

    def __init__(self) -> None:
        super().__init__(miss_value="__never__", fail_times=0)
        self.calls = 0

    def element_by_spec(self, spec, scope, timeout_ms):
        self.calls += 1
        return Miss(kind="uia", reason="not_found", elapsed_ms=2)


class _RecordingInj(_FakeInj):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def hotkey(self, keys):
        self.calls.append(list(keys))
        return len(keys)


def _hotkey_step() -> dict:
    """真机 run-092d827a9a 的 s09 形状：hotkey(enter)，锚点是 Dify 根 Pane。"""
    s = StepDef(sid="s09", action="hotkey", label="回车", app_hint={"exe": "app.exe"},
                locators=[LocatorSpec(kind="uia", by="name_type",
                                      value={"name": "工作室 - Dify - Google Chrome",
                                             "control_type": "Pane"},
                                      confidence=0.4)],
                params={"combo": "enter"}, risk="write",
                policy={"on_fail": "suspend_manual", "retries": 1, "backoff_ms": 1})
    return s.to_dict()


def _mk_runner(graph: dict, insp, inj, run_id: str, cfg_over: dict | None = None,
               windows=None):
    from harness.modules.desktop_repeat.executor.pacing import NullTakeover
    cfg = RepeatConfig.load({"retries": 1, "retry_backoff_ms": 1,
                             "human_takeover_detect": False, **(cfg_over or {})})
    return RepeatRunner(
        workflow_id="wf", graph=graph, resolved={}, flags={}, cfg=cfg,
        inspector=insp, injector=inj, windows=windows or _FakeWinA(),
        env_provider=_FakeEnvA(),
        gate=SafetyGate(mode="auto"), run_id=run_id,
        reporter=Reporter(run_id, ProgressStore()), emergency_stop=threading.Event(),
        takeover=NullTakeover())


@pytest.mark.asyncio
async def test_hotkey_locator_exhausted_falls_back_to_focus_injection():
    """热键不依赖元素锚点（act_hotkey 完全不用 hit），定位链只是个"会失败的门闩"。

    真机 run-092d827a9a s09：录制层把回车绑到"该回车按下之后才出现的 Dify 页面"
    上，于是出现因果倒置——必须先定位到页面才允许按回车，而页面要等回车才存在。
    修复前：阶梯判 RP-2001 → on_fail=suspend_manual → 回放永远停在 s09 等人工。
    """
    insp, inj = _AlwaysMissInspector(), _RecordingInj()
    r = _mk_runner(_graph([_hotkey_step()]), insp, inj, "run-a4")
    try:
        # 修复前这里不是失败而是**无限挂起**（await 人工介入），真机表现为
        # "回放等待人工 · s09"停住不动。用超时栅栏把挂死变成可断言的失败，
        # 免得回归时把整个测试套件拖住。
        rec = await asyncio.wait_for(r.run(), timeout=10)
    except TimeoutError:  # noqa: UP041
        pytest.fail("热键定位链耗尽后回放无限挂起（真机 s09 现象）：未降级为焦点注入")
    sr = rec.steps[0]
    # 修复前：inj.calls 为空、rec.status="suspended"、sr.code="RP-2001"
    assert inj.calls == [["enter"]], "热键未注入：定位链耗尽即死锁"
    assert sr.status == "done", f"步骤未成功: {sr.status} / {sr.code}"
    assert sr.hit_kind == "focus", f"未走焦点注入降级: {sr.hit_kind}"
    assert rec.status == "succeeded", f"整轮未通过: {rec.status}"


@pytest.mark.asyncio
async def test_dry_run_hotkey_reports_focus_not_miss():
    """干跑必须与真实回放口径一致：热键锚点全丢时判 focus，不能判 miss。

    否则 Studio 预览会显示"此步骤不可用"，用户还没回放就被劝退。
    """
    insp, inj = _AlwaysMissInspector(), _RecordingInj()
    r = _mk_runner(_graph([_hotkey_step()]), insp, inj, "run-a4-dry")
    rep = await r.dry()
    st = next(s for s in rep.steps if s.sid == "s09")
    assert st.hit_kind == "focus", f"干跑热键判为 miss: {st.hit_kind} / {st.message}"
    assert st.hit_index >= 0 and rep.ok, "干跑判定流程不可用"
    assert inj.calls == [], "干跑不得真正注入"


# ── A5：模态对话框不得把回放锁死（真机 EditPlus「另存为」s11）────────────
class _ModalWindows(_FakeWinA):
    """可编排的窗口假件：主窗口 + 同进程模态对话框，枚举/启用态/前台可控。

    find_window 复用**真实** Win32 实现（只替换枚举与状态查询），保证测的是
    产品算法而不是测试里重写的简化版。
    """

    def __init__(self) -> None:
        from harness.modules.desktop_repeat.platform.win32.windows import WinWindowManager
        super().__init__()
        self.main = WindowRef(hwnd=1, title="EditPlus - [新的文档 7 *]",
                              exe="editplus.exe", cls="AfxFrameOrView140su",
                              rect=(0, 0, 1600, 900), client=(0, 0, 1600, 900))
        self.dlg = WindowRef(hwnd=99, title="另存为", exe="editplus.exe",
                             cls="#32770", rect=(536, 188, 1496, 788),
                             client=(536, 188, 1496, 788))
        self.win = self.main
        self.visible: list = [self.main]
        self.enabled = {1: True, 99: True}
        self.fg = self.main
        self._real_find = WinWindowManager.find_window
        self.enum_calls = 0
        self.dialog_after_enums = 0  # >0：第 N 次枚举后才"弹出"对话框

    def foreground(self):
        return self.fg

    def is_enabled(self, hwnd):
        return self.enabled.get(hwnd, True)

    def enumerate(self, pid=None):
        self.enum_calls += 1
        if self.dialog_after_enums and self.enum_calls >= self.dialog_after_enums:
            if self.dlg not in self.visible:
                self.visible.append(self.dlg)
        return list(self.visible)

    def find_window(self, hints):
        return self._real_find(self, hints)

    def focus(self, win):
        # 真实 Win32：被模态框禁用的窗口 SetForegroundWindow 必然失败
        if not self.enabled.get(getattr(win, "hwnd", 0), True):
            return False
        self.fg = win
        return True


def _hint_step(sid: str, action: str, hint: dict, locators=None, risk="read") -> dict:
    s = StepDef(sid=sid, action=action, label=sid, app_hint=hint,
                locators=locators or [], params={}, risk=risk,
                policy={"on_fail": "suspend_manual", "retries": 0, "backoff_ms": 1})
    return s.to_dict()


@pytest.mark.asyncio
async def test_modal_dialog_does_not_block_window_focus():
    """作用域窗口被模态框禁用时，window_focus 必须放行并把作用域切到模态面。

    真机：EditPlus 点保存 → 「另存为」弹出、主窗口被 Disable → s11 window_focus
    被 RP-1007 拒绝，"切进对话框"这一步本身被遮挡判定拦死，整轮挂起。
    """
    win = _ModalWindows()
    # 真机形状：对话框刚弹出、尚未进入可枚举列表（点击→可见有几百毫秒延迟），
    # 但前台已经切到它，且主窗口已被系统 Disable。
    win.visible = [win.main]
    win.enabled[1] = False       # 主窗口被模态框禁用
    win.fg = win.dlg             # 前台已是「另存为」
    step = _hint_step("s11", "window_focus",
                      {"exe": "editplus.exe", "window_title_regex": ".*另存为.*"})
    r = _mk_runner(_graph([step]), _RelaxLoggingInspector("Z", 0), _FakeInj(),
                   "run-a5", cfg_over={"window_appear_wait_ms": 400}, windows=win)
    r._scope = win.main
    try:
        rec = await asyncio.wait_for(r.run(), timeout=10)
    except TimeoutError:  # noqa: UP041
        pytest.fail("window_focus 在模态遮挡下挂起（未放行）")
    sr = rec.steps[0]
    # 修复前：RP-1007（被禁用的旧作用域 → 一刀切判遮挡）
    assert sr.code != "RP-1007", f"模态遮挡把切作用域步骤锁死: {sr.code}"
    assert sr.status == "done", f"步骤未成功: {sr.status} / {sr.code}"
    assert r._scope is not None and r._scope.hwnd == 99, "作用域未接管到模态对话框"


@pytest.mark.asyncio
async def test_click_in_dialog_with_drifted_title_not_rejected():
    """类名精确命中（cls 来自真实目标窗口）时，标题漂移不得按 RP-4002 拒写。

    真机：EditPlus「另存为」对话框标题会在「另存为」↔「EditPlus」之间漂移，
    s13 的 app_hint 是 cls=#32770 + title=.*EditPlus.*。仅按标题校验会把
    "正确的对话框"判成错误窗口，写操作被拒 → 保存永远点不到。
    """
    win = _ModalWindows()
    win.visible = [win.main, win.dlg]
    win.fg = win.dlg
    locs = [LocatorSpec(kind="uia", by="name_type",
                        value={"name": "保存(S)", "control_type": "Button"},
                        confidence=0.8)]
    step = _hint_step("s13", "click",
                      {"exe": "editplus.exe", "cls": "#32770",
                       "window_title_regex": ".*EditPlus.*"},
                      locators=locs, risk="write")
    r = _mk_runner(_graph([step]), _RelaxLoggingInspector("Z", 0), _RecordingInj(),
                   "run-a5b", cfg_over={"window_appear_wait_ms": 400}, windows=win)
    try:
        rec = await asyncio.wait_for(r.run(), timeout=10)
    except TimeoutError:  # noqa: UP041
        pytest.fail("对话框内点击挂起")
    sr = rec.steps[0]
    # 修复前：标题不符 → _scope_title_ok=False → 写操作 RP-4002
    assert sr.code != "RP-4002", f"标题漂移被误判为错误窗口: {sr.code} / {sr.message}"
    assert r._scope is not None and r._scope.hwnd == 99, "未定位到对话框"
    assert sr.status == "done", f"点击保存未成功: {sr.status} / {sr.code}"


@pytest.mark.asyncio
async def test_modal_blocked_degrades_when_foreground_is_another_process():
    """前台是别的进程（回放时 andy-harness 自己的窗口常在前台）时不得挂起。

    真机：用户点了「回放」按钮 → 前台是 harness 窗口 → 同进程接管失败 →
    旧实现直接 RP-1007 挂起整轮。正确行为是降级放行，让定位链在作用域子树
    （UIA 里对话框是主窗口的子节点）实打实地搜；同时禁用盲坐标兜底。
    """
    win = _ModalWindows()
    # 对话框尚未进入枚举列表（刚弹出），前台又是别的进程 → 接管不了
    win.visible = [win.main]
    win.enabled[1] = False      # 主窗口被模态框禁用
    other = WindowRef(hwnd=777, title="Andy-Harness", exe="harness.exe",
                      cls="Chrome_WidgetWin_1", rect=(0, 0, 1920, 1080),
                      client=(0, 0, 1920, 1080))
    win.fg = other              # 前台是别的进程
    locs = [LocatorSpec(kind="uia", by="name_type",
                        value={"name": "保存(S)", "control_type": "Button"},
                        confidence=0.8)]
    step = _hint_step("s13", "click",
                      {"exe": "editplus.exe", "cls": "#32770",
                       "window_title_regex": ".*EditPlus.*"},
                      locators=locs, risk="write")
    r = _mk_runner(_graph([step]), _RelaxLoggingInspector("Z", 0), _RecordingInj(),
                   "run-a5d", cfg_over={"window_appear_wait_ms": 400}, windows=win)
    r._scope = win.main
    try:
        rec = await asyncio.wait_for(r.run(), timeout=10)
    except TimeoutError:  # noqa: UP041
        pytest.fail("跨进程前台时仍被模态遮挡挂起")
    sr = rec.steps[0]
    assert sr.code != "RP-1007", f"被模态遮挡判死: {sr.code}"
    assert sr.status == "done", f"步骤未成功: {sr.status} / {sr.code}"
    assert r._modal_degraded is True, "降级期间应标记（禁用盲坐标兜底）"
    # 关键：不得把别的进程窗口当成操作面接管（会把点击带进 harness 窗口）
    assert r._scope is not None and r._scope.hwnd == 1, \
        f"错误接管了跨进程前台窗口: {getattr(r._scope, 'title', None)}"


@pytest.mark.asyncio
async def test_window_focus_skips_when_target_disabled_by_modal():
    """目标窗口被模态框禁用时，window_focus 应 skip 而不是 RP-5001 挂起。"""
    win = _ModalWindows()
    win.visible = [win.main]
    win.enabled[1] = False
    # 前台是别的进程 → 同进程接管失败 → 作用域停留在被禁用的主窗口，
    # 此时 act_focus 必须 skip（旧实现抛 RP-5001 把整轮钉死在这一步）。
    win.fg = WindowRef(hwnd=777, title="Andy-Harness", exe="harness.exe",
                       cls="Chrome_WidgetWin_1", rect=(0, 0, 1920, 1080),
                       client=(0, 0, 1920, 1080))
    step = _hint_step("s11", "window_focus",
                      {"exe": "editplus.exe", "window_title_regex": ".*EditPlus.*"})
    r = _mk_runner(_graph([step]), _RelaxLoggingInspector("Z", 0), _FakeInj(),
                   "run-a5e", cfg_over={"window_appear_wait_ms": 300}, windows=win)
    r._scope = win.main
    try:
        rec = await asyncio.wait_for(r.run(), timeout=10)
    except TimeoutError:  # noqa: UP041
        pytest.fail("window_focus 在目标被禁用时挂起（应 skip）")
    assert rec.steps[0].status == "done", f"{rec.steps[0].status} / {rec.steps[0].code}"


@pytest.mark.asyncio
async def test_window_focus_waits_for_dialog_to_appear():
    """目标对话框尚未可枚举时，window_focus 应做有界等待而不是沿用旧作用域。

    点击到窗口出现有几百毫秒延迟（真机 seq16 点击 → seq17 窗口变化 ≈823ms），
    立即枚举必扑空；旧实现直接沿用上一个作用域，后续步骤全落在错误窗口。
    """
    win = _ModalWindows()
    win.dialog_after_enums = 3   # 前两次枚举看不到对话框（模拟弹出延迟）
    win.fg = win.dlg
    step = _hint_step("s11", "window_focus",
                      {"exe": "editplus.exe", "window_title_regex": ".*另存为.*"})
    r = _mk_runner(_graph([step]), _RelaxLoggingInspector("Z", 0), _FakeInj(),
                   "run-a5c", cfg_over={"window_appear_wait_ms": 2500}, windows=win)
    r._scope = win.main
    try:
        await asyncio.wait_for(r.run(), timeout=10)
    except TimeoutError:  # noqa: UP041
        pytest.fail("等待窗口出现时挂起")
    # 修复前：扑空后沿用主窗口（hwnd=1）
    assert r._scope is not None and r._scope.hwnd == 99, \
        f"未等到对话框出现，作用域仍是 {getattr(r._scope, 'title', None)}"


# ── B1b：P1-3 跨进程接管（CLI 独立进程发现活动录制会话）────────────
def test_discover_live_session_finds_fresh_draft(tmp_path: Path):
    import json
    import time as _t

    import harness.modules.desktop_repeat.service as svc_mod
    from harness.modules.desktop_repeat.config import RepeatConfig
    from harness.modules.desktop_repeat.store.bundle import new_session_id

    root = tmp_path / "sessions"
    root.mkdir()
    db_path = tmp_path / "t.db"
    from harness.infra.database import Database
    svc = svc_mod.DesktopRepeatService(Database(db_path=str(db_path)), root,
                                       RepeatConfig.load())
    # 模拟别的进程正在录：draft + 心跳新鲜的 bundle
    sid = new_session_id()
    sdir = root / sid
    (sdir / "runtime").mkdir(parents=True)
    (sdir / "session.json").write_text(json.dumps(
        {"schema": 1, "session_id": sid, "status": "draft", "label": "外部进程录制",
         "counters": {}, "integrity": {}, "error": None}), encoding="utf-8")
    (sdir / "runtime" / "heartbeat.json").write_text(json.dumps(
        {"pid": 99999, "state": "recording", "events": 3, "drops": 0, "qsize": 0,
         "ts": int(_t.time() * 1000)}), encoding="utf-8")
    # 陈旧心跳（失联）与已封版的不得被认领
    stale = new_session_id()
    sdir2 = root / stale
    (sdir2 / "runtime").mkdir(parents=True)
    (sdir2 / "session.json").write_text(json.dumps(
        {"schema": 1, "session_id": stale, "status": "draft", "label": "陈旧",
         "counters": {}, "integrity": {}, "error": None}), encoding="utf-8")
    (sdir2 / "runtime" / "heartbeat.json").write_text(json.dumps(
        {"pid": 1, "state": "recording", "events": 9, "drops": 0, "qsize": 0,
         "ts": int(_t.time() * 1000) - 60_000}), encoding="utf-8")

    assert svc._discover_live_session() == sid, "应接管心跳新鲜的 draft 会话"

    # 全部终态后不再认领
    (sdir / "session.json").write_text(json.dumps(
        {"schema": 1, "session_id": sid, "status": "ready", "label": "",
         "counters": {}, "integrity": {}, "error": None}), encoding="utf-8")
    assert svc._discover_live_session() is None, "无活跃会话时必须返回 None"


# ── B2：前端 i18n repeat.* 键 zh/en 必须齐全 ───────────────────────
def test_repeat_i18n_keys_present_in_both_locales():
    src = Path(__file__).resolve().parents[2] / "frontend" / "src"
    view = (src / "views" / "RepeatStudioView.vue").read_text(encoding="utf-8")
    used = set(re.findall(r"'(repeat\.[A-Za-z0-9]+)'", view))
    assert used, "未解析到 repeat.* 键"

    def locale_keys(fname: str) -> set[str]:
        text = (src / "i18n" / fname).read_text(encoding="utf-8")
        return set(re.findall(r"'([a-zA-Z]+\.[A-Za-z0-9]+)':", text))

    for fname in ("zh.ts", "en.ts"):
        have = locale_keys(fname)
        missing = sorted(k for k in used if k not in have)
        assert not missing, f"{fname} 缺少 RepeatStudioView 使用的键: {missing}"
