"""desktop_repeat 执行层组件测试（§12.1：FakeUiBridge + FakeDesktopController 同款思路）。

用纯 Python 假平台件驱动 RepeatRunner 全路径：
- 成功回放（hit 记录、检查点、SSE 事件）；
- 定位失败 → 挂起 → 人工 skip/abort（§5.2 P10/P11/P12）；
- 环境闸门（L2/L3 禁 coord，ADR-05/RP-2006）与显式放行（blind）；
- 干跑零注入（A7）；安全门拦截零注入；
- 断点续跑（T-22）。

注意：生产链尾恒有 coord 兜底（L0 命中是正常降级）；要测"链耗尽"
必须构造无 coord 的链（对应用户在画布删掉坐标兜底的情形）。
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from harness.modules.desktop_repeat.bus import ProgressStore, Reporter
from harness.modules.desktop_repeat.config import RepeatConfig
from harness.modules.desktop_repeat.executor.pacing import NullTakeover
from harness.modules.desktop_repeat.executor.runner import RepeatRunner, graph_sha256
from harness.modules.desktop_repeat.models import (
    Env,
    Hit,
    LocatorSpec,
    Miss,
    MonitorInfo,
    StepDef,
    WindowRef,
)
from harness.modules.desktop_repeat.safety_gate import SafetyGate


# ── 假平台件 ─────────────────────────────────────────────────────
class FakeInspector:
    def __init__(self, hits: dict[str, str]) -> None:
        self.hits = hits
        self.readbacks: dict[str, str] = {}
        self.pattern_ok = {"invoke": True, "set_value": True}

    def available(self) -> bool:
        return True

    def warmup(self) -> None:
        pass

    def element_by_spec(self, spec: LocatorSpec, scope: dict | None, timeout_ms: int):
        key = f"{spec.by}:{spec.value}"
        verdict = self.hits.get(key, "hit")
        if verdict == "hit":
            return Hit(rect=(100, 100, 180, 130), center=(140, 115),
                       element=f"tok:{key}", kind=spec.kind, score=spec.confidence,
                       elapsed_ms=1, diagnostics={"by": spec.by})
        reason = {"miss": "not_found", "disabled": "disabled", "timeout": "timeout"}.get(verdict, "not_found")
        return Miss(kind=spec.kind, reason=reason, elapsed_ms=2)

    def is_editable(self, element: Any, timeout_ms: int = 500) -> bool:
        """默认视为可写控件；用 non_editable 模拟"锚到窗口容器"（Pane）。"""
        return str(element) not in getattr(self, "non_editable", set())

    def read_value(self, element: Any, timeout_ms: int = 500) -> str:
        return self.readbacks.get(str(element), "")

    def set_value(self, element: Any, text: str, timeout_ms: int = 1000) -> bool:
        if not self.pattern_ok.get("set_value", True):
            return False
        self.readbacks[str(element)] = text
        return True

    def invoke(self, element: Any, timeout_ms: int = 1000) -> bool:
        return bool(self.pattern_ok.get("invoke", True))

    def toggle(self, element: Any) -> bool:
        return True

    def select_item(self, element: Any) -> bool:
        return False

    def legacy_do_default(self, element: Any) -> bool:
        return False

    def focus_element(self, element: Any) -> bool:
        return True

    def patterns_of_element(self, token: Any) -> list[str]:
        return ["InvokePattern", "ValuePattern"]

    def snapshot_of_token(self, token: Any, timeout_ms: int = 600):
        return None

    def release(self, token: Any) -> None:
        pass

    def shutdown(self) -> None:
        pass


class FlakyInspector(FakeInspector):
    """第 n 次起由 fail 转 hit（瞬态重试路径）。"""

    def __init__(self, key: str, fail_times: int) -> None:
        super().__init__({key: "timeout"})
        self._key = key
        self._fail = fail_times
        self.n = 0

    def element_by_spec(self, spec, scope, timeout_ms):
        self.n += 1
        if self.n > self._fail:
            self.hits[self._key] = "hit"
        return super().element_by_spec(spec, scope, timeout_ms)


class FakeInjector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def move(self, x, y):
        self.calls.append(("move", (x, y)))
        return 1

    def click(self, x, y, button="left", count=1):
        self.calls.append(("click", (x, y, button, count)))
        return 1

    def press(self, button, down, x=None, y=None):
        self.calls.append(("press", (button, down)))
        return 1

    def scroll(self, x, y, dx, dy):
        self.calls.append(("scroll", (x, y, dx, dy)))
        return 1

    def type_unicode(self, text):
        self.calls.append(("unicode", text))
        return len(text)

    def hotkey(self, keys):
        self.calls.append(("hotkey", tuple(keys)))
        return len(keys)

    def injected_count(self) -> int:
        return len(self.calls)


class FakeWindows:
    def __init__(self, win: WindowRef | None = None) -> None:
        self.win = win or WindowRef(hwnd=42, title="T", exe="app.exe",
                                    rect=(0, 0, 1000, 800), client=(10, 40, 990, 790))

    def foreground(self):
        return self.win

    def find_window(self, hints: dict) -> WindowRef | None:
        return self.win

    def focus(self, win) -> bool:
        return True

    def restore(self, win) -> None:
        pass

    def alive(self, hwnd, timeout_ms=5000) -> bool:
        return True

    def is_enabled(self, hwnd) -> bool:
        return True

    def enumerate(self, pid=None):
        return [self.win]


def _base_env() -> Env:
    return Env(os="Test", virtual_screen=(0, 0, 1920, 1080),
               monitors=[MonitorInfo(index=1, rect=(0, 0, 1920, 1080), work=(0, 0, 1920, 1040),
                                     dpi=96, scale=1.0)])


class FakeEnv:
    def __init__(self, env: Env | None = None) -> None:
        self.env = env or _base_env()

    def snapshot(self) -> Env:
        return self.env

    def graphics_available(self) -> bool:
        return True

    def desktop_name(self) -> str:
        return "Default"

    def is_elevated(self) -> bool:
        return False


def _step(sid: str, action: str, *, by: str = "automation_id", value: Any = "btn",
          params: dict | None = None, risk: str = "write",
          policy: dict | None = None, timing: dict | None = None,
          assertion: dict | None = None, coord: bool = True) -> dict:
    locs = [LocatorSpec(kind="uia", by=by, value=value, confidence=0.95)]
    if coord:
        locs.append(LocatorSpec(kind="coord", pos=(10, 10), scale=1.0,
                                screen=(1920, 1080), confidence=0.25))
    s = StepDef(sid=sid, action=action, label=f"{sid} 步骤",
                app_hint={"exe": "app.exe"}, locators=locs,
                params=params or {}, risk=risk,
                policy=policy or {"on_fail": "suspend_manual", "retries": 1,
                                  "backoff_ms": 50, "idempotent": True},
                timing=timing or {}, assertion=assertion)
    return s.to_dict()


def _graph(steps: list[dict], params_defs: list[dict] | None = None,
           env_base: Env | None = None) -> dict:
    nodes = [{"id": "n0", "type": "start", "data": {"params": params_defs or []}},
             {"id": "n2", "type": "end", "data": {}}]
    edges = []
    prev = "n0"
    for i, sd in enumerate(steps):
        nid = f"g{i}"
        nodes.append({"id": nid, "type": "gui", "data": sd})
        edges.append({"id": f"e{prev}{nid}", "source": prev, "target": nid})
        prev = nid
    edges.append({"id": f"e{prev}n2", "source": prev, "target": "n2"})
    return {"nodes": nodes, "edges": edges,
            "meta": {"env_base": (env_base or _base_env()).to_dict()}}


def _runner(graph: dict, *, inspector=None, injector=None, windows=None, env=None,
            gate=None, store=None, unattended=False, run_root=None,
            resolved=None, flags=None, takeover=None, **kw) -> RepeatRunner:
    cfg = RepeatConfig.load({"retry_backoff_ms": 30, "retries": 1})
    return RepeatRunner(
        workflow_id="wf-test", graph=graph,
        resolved=resolved or {}, flags=flags or {}, cfg=cfg,
        inspector=inspector or FakeInspector({}),
        injector=injector or FakeInjector(),
        windows=windows or FakeWindows(),
        env_provider=env or FakeEnv(),
        gate=gate or SafetyGate(mode="auto", allow_password=True),
        run_id="run-test",
        reporter=Reporter("run-test", store or ProgressStore()),
        emergency_stop=threading.Event(),
        # 假平台件不产生真实注入：用 NullTakeover 隔离全局 OS 输入态，
        # 否则本机上任何真实鼠标/键盘活动都会被误判为人工接管而永久挂起。
        takeover=takeover or NullTakeover(),
        run_root=run_root, unattended=unattended, **kw)


async def _run_until_suspended(r: RepeatRunner, *, timeout: float = 5.0) -> asyncio.Task:
    """启动 run()，等待进入 suspended，返回未完成任务。"""
    task = asyncio.create_task(r.run())
    try:
        await asyncio.wait_for(_wait_status(r, "suspended"), timeout)
    except (TimeoutError, asyncio.InvalidStateError):
        pass
    return task


async def _wait_status(r: RepeatRunner, status: str) -> None:
    while r.rec.status != status:
        if r.rec.status in ("succeeded", "failed", "aborted"):
            return
        await asyncio.sleep(0.01)


# ── 成功路径 ─────────────────────────────────────────────────────
class TestRunnerHappyPath:
    @pytest.mark.asyncio
    async def test_click_and_type_succeed(self, tmp_path: Path):
        g = _graph([
            # s01 不生成 coord 定位器：本用例要验证 InvokePattern 通道，
            # 否则会触发"容器点击改用录制坐标"的校正而改走坐标点击。
            _step("s01", "click", by="automation_id", value="btnSave", coord=False),
            _step("s02", "type", value="txtName", params={"value": "ACME"}),
        ])
        inj = FakeInjector()
        insp = FakeInspector({"automation_id:btnSave": "hit", "automation_id:txtName": "hit"})
        r = _runner(g, injector=inj, inspector=insp, run_root=tmp_path)
        rec = await r.run()
        assert rec.status == "succeeded"
        assert [s.status for s in rec.steps] == ["done", "done"]
        assert rec.steps[0].hit_index == 0 and rec.steps[0].hit_kind == "uia"
        # click 走 InvokePattern：不消耗坐标注入
        assert inj.injected_count() == 0, inj.calls
        # type 走 SetValue 通道并读回一致
        assert insp.readbacks.get("tok:automation_id:txtName") == "ACME"
        cp = json.loads((tmp_path / "runtime" / "checkpoint.json").read_text(encoding="utf-8"))
        assert cp["completed"] == ["s01", "s02"]
        assert cp["graph_sha256"] == graph_sha256(g)

    @pytest.mark.asyncio
    async def test_coord_fallback_when_uia_miss(self):
        """uia miss → coord（L0 闸门放行）成功，标 degraded（§7.4 降级）。"""
        g = _graph([_step("s01", "click", by="automation_id", value="gone")])
        insp = FakeInspector({"automation_id:gone": "miss"})
        inj = FakeInjector()
        r = _runner(g, inspector=insp, injector=inj)
        rec = await r.run()
        assert rec.status == "succeeded"
        sr = rec.steps[0]
        assert sr.status == "degraded" and sr.degraded
        assert sr.hit_index == 1 and sr.hit_kind == "coord"
        assert any(c[0] == "click" for c in inj.calls)

    @pytest.mark.asyncio
    async def test_env_l3_blocks_coord_gate_suspend(self):
        """L3 + 链仅 uia/coord：uia miss、coord 被闸门拒 → RP-2006 挂起（ADR-05）。"""
        cur = Env(virtual_screen=(0, 0, 800, 600),
                  monitors=[MonitorInfo(index=1, rect=(0, 0, 800, 600), scale=1.0)])
        g = _graph([_step("s01", "click", by="automation_id", value="x")])
        insp = FakeInspector({"automation_id:x": "miss"})
        r = _runner(g, inspector=insp, env=FakeEnv(cur))
        task = await _run_until_suspended(r)
        assert r.rec.env_level == "L3"
        assert r.rec.steps[-1].code == "RP-2006"
        r.resume("abort")
        await task
        assert r.rec.status == "aborted"

    @pytest.mark.asyncio
    async def test_blind_coord_allowed_explicitly(self):
        """显式 allow_blind_coord：L3 也放行 coord，blind 标记进审计（§7.2 RP-2006 例外）。"""
        cur = Env(virtual_screen=(0, 0, 800, 600),
                  monitors=[MonitorInfo(index=1, rect=(0, 0, 800, 600), scale=1.0)])
        g = _graph([_step("s01", "click", by="automation_id", value="x")])
        insp = FakeInspector({"automation_id:x": "miss"})
        inj = FakeInjector()
        r = _runner(g, inspector=insp, env=FakeEnv(cur), injector=inj,
                    allow_blind_coord=True)
        rec = await r.run()
        assert rec.status == "succeeded"
        assert rec.blind_used and rec.steps[0].blind


# ── 失败阶梯 ─────────────────────────────────────────────────────
class TestRunnerFailureLadder:
    @pytest.mark.asyncio
    async def test_locate_exhausted_suspend_then_skip(self):
        """无 coord 链（用户删兜底）+ uia miss → RP-2001 挂起 → skip 前进（P10/P12）。"""
        g = _graph([
            _step("s01", "click", value="ghost", coord=False),
            _step("s02", "click", value="ok", coord=False),
        ])
        insp = FakeInspector({"automation_id:ghost": "miss", "automation_id:ok": "hit"})
        r = _runner(g, inspector=insp)
        task = await _run_until_suspended(r)
        assert r.rec.steps[0].code == "RP-2001"
        r.resume("skip")
        rec = await task
        assert rec.status == "succeeded"
        assert rec.steps[0].status == "skipped"
        assert rec.steps[1].status == "done"

    @pytest.mark.asyncio
    async def test_on_fail_abort_finishes_failed(self):
        g = _graph([_step("s01", "click", value="ghost", coord=False,
                          policy={"on_fail": "abort", "retries": 0, "backoff_ms": 10,
                                  "idempotent": True})])
        insp = FakeInspector({"automation_id:ghost": "miss"})
        r = _runner(g, inspector=insp)
        rec = await r.run()
        assert rec.status == "failed"
        assert rec.error_code == "RP-2001"

    @pytest.mark.asyncio
    async def test_transient_timeout_then_hit_via_coord(self):
        """uia 首查超时 → 瞬态重试后 uia 恢复命中（P08/P09 阶梯）。"""
        g = _graph([_step("s01", "click", value="x", coord=False,
                          policy={"on_fail": "suspend_manual", "retries": 2,
                                  "backoff_ms": 10, "idempotent": True})])
        insp = FlakyInspector("automation_id:x", fail_times=1)
        r = _runner(g, inspector=insp)
        rec = await r.run()
        assert rec.status == "succeeded"
        assert rec.steps[0].attempts and rec.steps[0].attempts[0].reason == "timeout"

    @pytest.mark.asyncio
    async def test_safety_deny_no_injection_then_abort(self):
        """安全门拦截（密码+未授权）：拦截即零注入（§9.1），人工 abort 收口。"""
        g = _graph([_step("s01", "type", value="pw", params={"value": "x", "password": True},
                          risk="dangerous")])
        inj = FakeInjector()
        gate = SafetyGate(mode="interactive", approval_provider=None, allow_password=False)
        r = _runner(g, injector=inj, gate=gate)
        task = await _run_until_suspended(r)
        assert inj.injected_count() == 0
        assert r.rec.steps[-1].code in ("RP-4004", "RP-4002")
        r.resume("abort")
        await task

    @pytest.mark.asyncio
    async def test_modal_blocked_suspend(self):
        """连前台窗口都取不到（UAC 安全桌面/无前台）→ 才是真遮挡 → RP-1007 挂起。

        2026-10-20 语义收窄：作用域被禁用**不再**一律挂起——回放时前台常是
        andy-harness 自己的窗口，一刀切会把"切进模态对话框继续操作"钉死
        （真机 EditPlus「另存为」s11）。现在只在取不到前台时判真遮挡。
        """
        g = _graph([_step("s01", "click", value="a")])
        wins = FakeWindows()
        wins.is_enabled = lambda hwnd: False  # type: ignore[method-assign]
        wins.foreground = lambda: None  # type: ignore[method-assign]
        insp = FakeInspector({"automation_id:a": "hit"})
        r = _runner(g, inspector=insp, windows=wins)
        task = await _run_until_suspended(r)
        assert r.rec.steps[-1].code == "RP-1007"
        r.resume("abort")
        await task

    @pytest.mark.asyncio
    async def test_modal_disabled_window_no_longer_suspends(self):
        """作用域被模态禁用但前台可取 → 降级放行，命中即执行（不再挂起）。"""
        g = _graph([_step("s01", "click", value="a")])
        wins = FakeWindows()
        wins.is_enabled = lambda hwnd: False  # type: ignore[method-assign]
        insp = FakeInspector({"automation_id:a": "hit"})
        r = _runner(g, inspector=insp, windows=wins)
        rec = await r.run()
        assert rec.steps[-1].status == "done", \
            f"模态禁用下仍被挂起: {rec.steps[-1].status} / {rec.steps[-1].code}"


# ── 干跑 ─────────────────────────────────────────────────────────
class TestRunnerDryRun:
    @pytest.mark.asyncio
    async def test_dry_zero_injection(self):
        """A7：干跑期间注入事件数 = 0。"""
        g = _graph([
            _step("s01", "click", value="a"),
            _step("s02", "type", value="b", params={"value": "hi"}),
            _step("s03", "manual_confirm", params={"prompt": "输密码"}),
        ])
        insp = FakeInspector({"automation_id:a": "hit", "automation_id:b": "hit"})
        inj = FakeInjector()
        r = _runner(g, inspector=insp, injector=inj)
        rep = await r.dry()
        assert inj.injected_count() == 0
        assert rep.ok and len(rep.steps) == 3
        assert rep.steps[0].hit_index == 0
        assert rep.steps[2].hit_kind == "manual_confirm"

    @pytest.mark.asyncio
    async def test_dry_coord_fallback_visible(self):
        g = _graph([_step("s01", "click", value="ghost")])
        insp = FakeInspector({"automation_id:ghost": "miss"})
        inj = FakeInjector()
        r = _runner(g, inspector=insp, injector=inj)
        rep = await r.dry()
        assert rep.steps[0].hit_index == 1 and rep.steps[0].hit_kind == "coord"
        assert inj.injected_count() == 0


# ── 断点续跑 / 检查点 ────────────────────────────────────────────
class TestRunnerCheckpoint:
    @pytest.mark.asyncio
    async def test_resume_skip_then_completed_steps_advance(self):
        """挂起→skip 后续跑到末步；检查点 completed 单调增长。"""
        g = _graph([
            _step("s01", "click", value="a", coord=False),
            _step("s02", "click", value="ghost", coord=False,
                  policy={"on_fail": "suspend_manual", "retries": 0, "backoff_ms": 10,
                          "idempotent": True}),
            _step("s03", "click", value="c", coord=False),
        ])
        insp = FakeInspector({"automation_id:a": "hit", "automation_id:ghost": "miss",
                              "automation_id:c": "hit"})
        r = _runner(g, inspector=insp)
        task = await _run_until_suspended(r)
        r.resume("skip")
        rec = await task
        assert rec.status == "succeeded"
        assert [s.status for s in rec.steps] == ["done", "skipped", "done"]
        assert rec.checkpoint["completed"][:3] == ["s01", "s02", "s03"]

    @pytest.mark.asyncio
    async def test_checkpoint_seed_skips_completed(self):
        """T-22 语义：给 completed 检查点，恢复时前序步骤不重放。"""
        g = _graph([
            _step("s01", "click", value="a", coord=False),
            _step("s02", "click", value="ghost", coord=False,
                  policy={"on_fail": "abort", "retries": 0, "backoff_ms": 10, "idempotent": True}),
        ])
        insp = FakeInspector({"automation_id:a": "hit", "automation_id:ghost": "miss"})
        inj = FakeInjector()
        cp = {"completed": ["s01"], "current": "s02"}
        r = _runner(g, inspector=insp, injector=inj, checkpoint=cp)
        rec = await r.run()
        assert rec.status == "failed"
        assert len(rec.steps) == 1          # s01 未重放
        assert rec.steps[0].sid == "s02"


# ── 提示节点 / SSE ───────────────────────────────────────────────
class TestRunnerNotices:
    @pytest.mark.asyncio
    async def test_notice_and_terminal_events_present(self):
        """§9.5.2：前置告知 notice + run_started + step + run_done（INV-P4 单终止）。"""
        store = ProgressStore()
        g = _graph([_step("s01", "click", value="a")])
        insp = FakeInspector({"automation_id:a": "hit"})
        r = _runner(g, inspector=insp, store=store)
        await r.run()
        evs = store._buffers.get("run-test", [])
        types = [e["type"] for e in evs]
        assert "notice" in types
        assert "run_started" in types
        assert "run_done" in types
        assert types.count("run_done") + types.count("run_error") == 1  # INV-P4
        assert store.has_terminal("run-test")


# ── 阶段二：异常 / 竞态 / 无人工分支 ─────────────────────────────
class NoAppWindows(FakeWindows):
    """目标应用未运行：任何匹配都返回 None。"""

    def find_window(self, hints: dict):
        return None

    def enumerate(self, pid=None):
        return []


class TestRunnerEdgeBranches:
    @pytest.mark.asyncio
    async def test_unattended_manual_confirm_aborts_instead_of_hanging(self):
        """无人值守遇人工节点：必须立即中止（RP-4004），不得无限等待裁决。"""
        g = _graph([_step("s01", "manual_confirm", params={"prompt": "请输入验证码"})])
        r = _runner(g, unattended=True)
        r.cfg.pre_run_ready_s = 0
        rec = await asyncio.wait_for(r.run(), timeout=5)
        assert rec.status == "aborted"
        assert rec.error_code == "RP-4004"

    @pytest.mark.asyncio
    async def test_resume_decision_before_suspend_is_not_lost(self):
        """裁决竞态（P11/P12）：用户在页面刚点"跳过"、runner 尚未挂起 future。"""
        g = _graph([_step("s01", "manual_confirm"), _step("s02", "click")])
        r = _runner(g, inspector=FakeInspector({"automation_id:btn": "hit"}))
        r.cfg.pre_run_ready_s = 0
        task = asyncio.create_task(r.run())
        r.resume("skip")  # 立即裁决：早于 _await_resume
        rec = await asyncio.wait_for(task, timeout=5)
        assert rec.status == "succeeded", (rec.status, rec.error_code)
        assert rec.steps[0].status == "skipped"
        assert rec.steps[1].status == "done"

    @pytest.mark.asyncio
    async def test_assertion_failure_suspends_then_abort(self):
        g = _graph([_step("s01", "click",
                          assertion={"kind": "value_equals", "value": "期望值"})])
        r = _runner(g, inspector=FakeInspector({"automation_id:btn": "hit"}))
        r.cfg.pre_run_ready_s = 0
        task = await _run_until_suspended(r)
        assert r.rec.status == "suspended"
        assert r.rec.steps[-1].code == "RP-3002"
        r.abort()
        await asyncio.wait_for(task, timeout=5)
        assert r.rec.status == "aborted"

    @pytest.mark.asyncio
    async def test_app_not_running_fails_fast_interactive(self):
        """回归（真机暴露）：目标应用未运行必须立刻失败并释放租约。

        旧实现在交互模式下挂起并无限等待：用户看不到运行面板、没有中止入口，
        run 一直占着输入租约，之后每次回放都被"已有回放进行中"拒绝
        （真机：run-e0e3d32b76 挂在 precheck，0/15 步）。
        """
        g = _graph([_step("s01", "click")])
        r = _runner(g, windows=NoAppWindows())
        r.cfg.pre_run_ready_s = 0
        rec = await asyncio.wait_for(r.run(), timeout=8)
        assert rec.status == "failed"
        assert rec.error_code == "RP-1002"
        assert "目标应用" in (rec.error_message or "")

    @pytest.mark.asyncio
    async def test_app_not_running_fails_unattended(self):
        g = _graph([_step("s01", "click")])
        r = _runner(g, windows=NoAppWindows(), unattended=True)
        r.cfg.pre_run_ready_s = 0
        rec = await asyncio.wait_for(r.run(), timeout=8)
        assert rec.status == "failed"
        assert rec.error_code == "RP-1002"

    @pytest.mark.asyncio
    async def test_marker_step_is_noop_and_succeeds(self):
        g = _graph([_step("s01", "marker")])
        r = _runner(g)
        r.cfg.pre_run_ready_s = 0
        rec = await asyncio.wait_for(r.run(), timeout=5)
        assert rec.status == "succeeded"
        assert rec.steps[0].status == "done"

    @pytest.mark.asyncio
    async def test_takeover_baseline_reset_on_start_and_resume(self):
        """回归（真机截图暴露）：启动回放的点击 / 用户点"重试"都不得被当成
        回放期间的干扰，否则首次 detect 必然误挂起（RP-7002 死循环）。"""
        class RecTakeover:
            def __init__(self):
                self.marks = 0

            def mark_injected(self, pos=None):
                self.marks += 1

            def detect(self):
                return False

        t = RecTakeover()
        g = _graph([_step("s01", "manual_confirm"),
                    _step("s02", "click")])
        r = _runner(g, takeover=t, inspector=FakeInspector({"automation_id:btn": "hit"}))
        r.cfg.pre_run_ready_s = 0
        task = asyncio.create_task(r.run())
        await asyncio.sleep(0.05)
        r.resume("skip")
        rec = await asyncio.wait_for(task, timeout=5)
        assert rec.status == "succeeded"
        assert t.marks >= 2, "启动/裁决未重置接管基线"


# ── 安全门：外部审批提供方的待审决策必须可授权 ──────────────────
class TestSafetyGateApproval:
    def test_provider_pending_decision_is_grantable(self):
        """回归（真机截图暴露）：REST 审批提供方返回 pending 但不带 token，
        旧实现 grant('') 永远失败 → 交互模式"重试"无限循环。"""
        from harness.modules.desktop_repeat.safety_gate import Decision, SafetyGate

        gate = SafetyGate(
            mode="interactive", approval_policy="degraded",
            approval_provider=lambda payload: Decision(False, "等待人工审批", True, ""))
        d = gate.check(run_id="r1", sid="s01", action="click", risk="write",
                       app_hint={}, params={}, scope_ok=True)
        assert d.pending_approval is True
        assert d.approval_token, "待审决策未分配可授权 token"
        assert gate.grant(d.approval_token) is True
        # 授权后同一 run 的写动作放行
        d2 = gate.check(run_id="r1", sid="s02", action="click", risk="write",
                        app_hint={}, params={}, scope_ok=True)
        assert d2.allow is True

    @pytest.mark.asyncio
    async def test_retry_reuses_step_row(self):
        """回归（真机截图暴露）：重试同一步骤不得在时间线追加重复行。"""
        from harness.modules.desktop_repeat.safety_gate import Decision, SafetyGate

        # 本用例验证"重试复用步骤行"，需要进入审批：用 first_write 策略，
        # 默认的 degraded 策略下 UIA 首级命中会直接放行。
        gate = SafetyGate(mode="interactive", approval_policy="first_write",
                          approval_provider=lambda p: Decision(False, "等待人工审批", True, ""))
        g = _graph([_step("s01", "click")])
        r = _runner(g, gate=gate, inspector=FakeInspector({"automation_id:btn": "hit"}))
        r.cfg.pre_run_ready_s = 0
        task = await _run_until_suspended(r)
        assert r.rec.status == "suspended"
        r.resume("retry")
        rec = await asyncio.wait_for(task, timeout=5)
        assert rec.status == "succeeded"
        sids = [s.sid for s in rec.steps]
        assert sids == ["s01"], f"步骤行重复: {sids}"
        assert rec.stats()["done"] == 1


@pytest.mark.asyncio
async def test_suspend_timeout_auto_aborts_and_releases():
    """回归（真实运行暴露）：挂起不能无限等待。

    旧实现 `await fut` 无超时：用户关掉页面后 run 永久 suspended、输入租约
    永不释放，之后每次回放都被"已有回放进行中"拒绝。超时必须收口为 RP-4005。
    """
    g = _graph([_step("s01", "manual_confirm")])
    r = _runner(g)
    old_ready, old_to = r.cfg.pre_run_ready_s, r.cfg.suspend_timeout_s
    r.cfg.pre_run_ready_s = 0
    r.cfg.suspend_timeout_s = 0.2
    try:
        rec = await asyncio.wait_for(r.run(), timeout=6)
    finally:
        r.cfg.pre_run_ready_s = old_ready
        r.cfg.suspend_timeout_s = old_to
    assert rec.status == "aborted"
    assert rec.error_code == "RP-4005"


@pytest.mark.asyncio
async def test_window_focus_resets_takeover_baseline():
    """回归（真实页面截图暴露）：window_focus 会误触发人工介入判定。

    Windows 前台锁让 SetForegroundWindow 失败时，focus() 退化为
    keybd_event(VK_MENU)（模拟 Alt 键解锁）。这会刷新系统"最后输入时间"但不属于
    人操作；不重置接管基线，紧接着的 detect() 必定返回 True → 第一步就挂起，
    用户看到"检测到人工介入，已暂停"（真机坐实）。
    """
    class RecTakeover:
        def __init__(self) -> None:
            self.marks = 0

        def mark_injected(self, pos=None) -> None:
            self.marks += 1

        def detect(self) -> bool:
            return False

    t = RecTakeover()
    g = _graph([_step("s01", "window_focus")])
    r = _runner(g, takeover=t)
    r.cfg.pre_run_ready_s = 0
    rec = await asyncio.wait_for(r.run(), timeout=5)
    assert rec.status == "succeeded", (rec.status, rec.error_code)
    assert t.marks >= 2, "window_focus 后未重置接管基线"


@pytest.mark.asyncio
async def test_run_budget_uses_minutes_not_seconds():
    """回归（真实用户运行暴露）：cfg.run_timeout_min 是分钟，RunBudget 收秒。

    旧实现把 30 直接传进去 → 预算只有 30 秒，任何稍长的流程必报
    RP-3003（真机：两次运行 duration_ms 恰为 30000/31000）。
    """
    import time

    g = _graph([_step("s01", "marker")])
    r = _runner(g)
    left = r._run_budget.deadline - time.perf_counter()
    assert left > 29 * 60, f"运行预算被当成秒（剩余 {left:.1f}s）"


def test_takeover_ignores_pointer_jitter(monkeypatch):
    """手搭在鼠标上的轻微抖动不得判成人工介入（真机：点完"重试"立刻 RP-7002）。"""
    import time

    from harness.modules.desktop_repeat.executor import pacing as P
    from harness.modules.desktop_repeat.platform.win32 import env as E

    t = P.HumanTakeover(None)
    t._last_inject_perf = time.perf_counter() - 1.0   # 我方 1s 前注入
    t._last_pos = (500, 400)
    monkeypatch.setattr(E, "idle_seconds", lambda: 0.5)      # 系统 0.5s 前有输入
    monkeypatch.setattr(P, "_cursor_pos", lambda: (503, 402))  # 3px 抖动
    assert t.detect() is False

    monkeypatch.setattr(P, "_cursor_pos", lambda: (640, 520))  # 明显移动
    assert t.detect() is True


@pytest.mark.asyncio
async def test_type_with_weak_anchor_falls_back_to_focus_input():
    """回归（真实百度流程）：键入步骤锚在 Chrome 窗口容器上，页面标题一变
    定位链必然全丢。键入本质是"向当前焦点发键盘"，应降级为直接输入，
    而不是 RP-2001 让整轮回放失败（真机：百度首页→结果页，s07 type miss）。
    """
    from harness.modules.desktop_repeat.models import LocatorSpec

    step = StepDef(sid="s01", action="type", label="输入搜索词",
                   app_hint={"exe": "app.exe"},
                   locators=[LocatorSpec(kind="uia", by="name_type",
                                         value={"name": "百度一下，你就知道",
                                                "control_type": "Pane"},
                                         confidence=0.9)],
                   params={"mode": "auto", "value": "zhangxueyou"},
                   risk="write",
                   policy={"on_fail": "suspend_manual", "retries": 0,
                           "backoff_ms": 0, "idempotent": False})
    g = _graph([step.to_dict()])
    inj = FakeInjector()

    class AlwaysMiss(FakeInspector):
        def element_by_spec(self, spec, scope, timeout_ms):
            return Miss(kind=spec.kind, reason="not_found", elapsed_ms=2)

    r = _runner(g, injector=inj, inspector=AlwaysMiss({}))
    r.cfg.pre_run_ready_s = 0
    rec = await asyncio.wait_for(r.run(), timeout=5)
    assert rec.status == "succeeded", (rec.status, rec.error_code, rec.steps[-1].message)
    assert ("unicode", "zhangxueyou") in inj.calls, "未降级为当前焦点输入"
    assert not any(c[0] == "click" for c in inj.calls), "弱锚点不应点击任何位置"


@pytest.mark.asyncio
async def test_type_with_strong_anchor_only_timeout_falls_back():
    """回归（真实 s04 地址栏）：强锚点（Edit）但所有链都是 timeout（UIA 慢、
    未跑完），存在性没被证伪。键入本质是"向当前焦点发键盘"，应降级直接输入，
    而不是 RP-2001 挂起整轮（真机：Chrome 深树 2s 遍历不完 → 地址栏明明在）。
    """
    from harness.modules.desktop_repeat.models import LocatorSpec

    step = StepDef(sid="s04", action="type", label="type → Edit『地址和搜索栏』",
                   app_hint={"exe": "app.exe"},
                   locators=[
                       LocatorSpec(kind="uia", by="name_type",
                                   value={"name": "地址和搜索栏", "control_type": "Edit"},
                                   confidence=0.82),
                       LocatorSpec(kind="uia", by="structure_path",
                                   value="Pane[百度 - Chrome]/ToolBar/Pane/Group/Edit[地址和搜索栏]",
                                   confidence=0.62),
                   ],
                   params={"mode": "auto", "value": "dify"},
                   risk="write",
                   policy={"on_fail": "suspend_manual", "retries": 0,
                           "backoff_ms": 0, "idempotent": False})
    g = _graph([step.to_dict()])
    inj = FakeInjector()

    class AlwaysTimeout(FakeInspector):
        def element_by_spec(self, spec, scope, timeout_ms):
            return Miss(kind=spec.kind, reason="timeout", elapsed_ms=2300)

    r = _runner(g, injector=inj, inspector=AlwaysTimeout({}))
    r.cfg.pre_run_ready_s = 0
    rec = await asyncio.wait_for(r.run(), timeout=5)
    assert rec.status == "succeeded", (rec.status, rec.error_code, rec.steps[-1].message)
    assert ("unicode", "dify") in inj.calls, "全 timeout 未降级为当前焦点输入"


@pytest.mark.asyncio
async def test_type_strong_anchor_confirmed_absent_does_not_fall_back():
    """强锚点（Edit）被**确证不存在**（not_found）时不得降级——元素确实不在，
    向焦点乱打会把文字送进错误控件，仍走正常失败阶梯（挂起）。
    """
    from harness.modules.desktop_repeat.models import LocatorSpec

    step = StepDef(sid="s01", action="type", label="type → Edit",
                   app_hint={"exe": "app.exe"},
                   locators=[LocatorSpec(kind="uia", by="name_type",
                                         value={"name": "收件人", "control_type": "Edit"},
                                         confidence=0.9)],
                   params={"mode": "auto", "value": "abc"},
                   risk="write",
                   policy={"on_fail": "suspend_manual", "retries": 0,
                           "backoff_ms": 0, "idempotent": False})
    g = _graph([step.to_dict()])
    inj = FakeInjector()

    class NotFound(FakeInspector):
        def element_by_spec(self, spec, scope, timeout_ms):
            return Miss(kind=spec.kind, reason="not_found", elapsed_ms=2)

    r = _runner(g, injector=inj, inspector=NotFound({}))
    r.cfg.pre_run_ready_s = 0
    task = asyncio.create_task(r.run())
    try:
        await asyncio.wait_for(_wait_status(r, "suspended"), timeout=5)
    except (TimeoutError, asyncio.InvalidStateError):
        pass
    assert r.rec.status == "suspended", "确证不存在的强锚点不应挂起（逻辑回归）"
    assert not any(c[0] == "unicode" for c in inj.calls), "确证不存在仍降级输入=乱打"
    r._finish_aborted("RP-7001", "测试清理")
    try:
        await asyncio.wait_for(task, timeout=2)
    except (TimeoutError, asyncio.InvalidStateError):
        task.cancel()


@pytest.mark.asyncio
async def test_type_on_container_anchor_does_not_click():
    """锚点命中窗口容器（Pane，is_editable=False）时，绝不能点击/聚焦容器。

    旧实现会走剪贴板通道 focus_element(容器) + Ctrl+V，把焦点从真正的输入框
    挪走（Chrome 实录：type 锚到 Pane，回放点页面中心后文字丢失）。
    """
    g = _graph([_step("s01", "type", by="automation_id", value="pane",
                      params={"value": "abc"}, coord=False)])
    inj = FakeInjector()
    insp = FakeInspector({"automation_id:pane": "hit"})
    insp.non_editable = {"tok:automation_id:pane"}
    r = _runner(g, injector=inj, inspector=insp)
    r.cfg.pre_run_ready_s = 0
    rec = await asyncio.wait_for(r.run(), timeout=5)
    assert rec.status == "succeeded", (rec.status, rec.error_code)
    assert ("unicode", "abc") in inj.calls, "容器锚点未降级为当前焦点输入"
    assert not any(c[0] == "click" for c in inj.calls), "容器锚点不应点击"


@pytest.mark.asyncio
async def test_scope_switches_between_same_exe_windows():
    """回归（真实百度流程）：同 exe 的多个窗口必须按**标题**隔离。

    旧实现只比 exe：进入 Andy-Harness 窗口后再也不会切到百度窗口，后续
    click/type 全在错误窗口里"命中"，最终 succeeded 但百度毫无反应。
    """
    import re as _re

    from harness.modules.desktop_repeat.models import WindowRef

    class MultiWindows(FakeWindows):
        def __init__(self) -> None:
            super().__init__()
            self.wins = [
                WindowRef(hwnd=1, title="Andy-Harness - Google Chrome", exe="app.exe"),
                WindowRef(hwnd=2, title="百度一下，你就知道 - Google Chrome", exe="app.exe"),
            ]
            self.focused: list[int] = []

        def find_window(self, hints):
            pat = hints.get("window_title_regex")
            for w in self.wins:
                if hints.get("exe") and w.exe != hints["exe"]:
                    continue
                if pat and not _re.search(pat, w.title):
                    continue
                return w
            return None

        def focus(self, win):
            self.focused.append(win.hwnd)
            return True

    def mk(sid: str, title: str) -> dict:
        return StepDef(sid=sid, action="click", label=sid,
                       app_hint={"exe": "app.exe",
                                 "window_title_regex": f".*{title}.*"},
                       locators=[LocatorSpec(kind="uia", by="automation_id", value="btn")],
                       risk="write").to_dict()

    w = MultiWindows()
    g = _graph([mk("s01", "Andy-Harness"), mk("s02", "百度一下")])
    r = _runner(g, windows=w)
    r.cfg.pre_run_ready_s = 0
    rec = await asyncio.wait_for(r.run(), timeout=5)
    assert rec.status == "succeeded", (rec.status, rec.error_code)
    assert w.focused == [1, 2], f"未按标题切换窗口: {w.focused}"
    assert r._scope is not None and r._scope.hwnd == 2


@pytest.mark.asyncio
async def test_human_takeover_detection_disabled_by_default():
    """默认不做"人工介入自动暂停"：GetLastInputInfo 对任何指针移动都刷新，
    用户手搭在鼠标上就会反复 RP-7002（真机多次坐实）。"""
    class AlwaysDetect:
        def mark_injected(self, pos=None) -> None:
            pass

        def detect(self) -> bool:
            return True

    g = _graph([_step("s01", "click"), _step("s02", "click")])
    r = _runner(g, takeover=AlwaysDetect())
    r.cfg.pre_run_ready_s = 0
    rec = await asyncio.wait_for(r.run(), timeout=5)
    assert rec.status == "succeeded", (rec.status, rec.error_code)
    assert r.cfg.human_takeover_detect is False


class TestApprovalPolicy:
    """审批时机：默认只对"降级定位的写操作"要求确认，不再每轮首次写操作必打断。"""

    def test_trusted_uia_hit_skips_approval(self):
        from harness.modules.desktop_repeat.safety_gate import Decision, SafetyGate

        asked: list[dict] = []

        def _provider(payload):
            asked.append(payload)
            return Decision(False, "", True, "")

        gate = SafetyGate(mode="interactive", approval_provider=_provider)
        d = gate.check(run_id="r1", sid="s01", action="click", risk="write", app_hint={},
                       params={}, scope_ok=True, hit_kind="uia", hit_index=0, degraded=False)
        assert d.allow is True
        assert asked == [], "可信 UIA 命中不应触发审批"

    def test_degraded_hit_requires_approval_with_specific_reason(self):
        from harness.modules.desktop_repeat.safety_gate import Decision, SafetyGate

        gate = SafetyGate(mode="interactive", approval_policy="degraded",
                          approval_provider=lambda p: Decision(False, "", True, ""))
        d = gate.check(run_id="r2", sid="s02", action="click", risk="write", app_hint={},
                       params={}, scope_ok=True, hit_kind="image", hit_index=2,
                       degraded=True, step_index=1)
        assert d.pending_approval is True
        assert "第 2 步" in d.reason and "图像匹配" in d.reason
        assert "不再询问" in d.reason

    def test_grant_covers_rest_of_run(self):
        from harness.modules.desktop_repeat.safety_gate import Decision, SafetyGate

        gate = SafetyGate(mode="interactive", approval_policy="degraded",
                          approval_provider=lambda p: Decision(False, "", True, ""))
        d = gate.check(run_id="r3", sid="s02", action="click", risk="write", app_hint={},
                       params={}, scope_ok=True, hit_kind="image", hit_index=2,
                       degraded=True, step_index=1)
        assert gate.grant(d.approval_token) is True
        d2 = gate.check(run_id="r3", sid="s03", action="click", risk="write", app_hint={},
                        params={}, scope_ok=True, hit_kind="coord", hit_index=3,
                        degraded=True, step_index=2)
        assert d2.allow is True, "授权后本轮剩余步骤应免审"

    @pytest.mark.asyncio
    async def test_interactive_run_with_uia_hits_never_suspends(self):
        from harness.modules.desktop_repeat.safety_gate import Decision, SafetyGate

        gate = SafetyGate(mode="interactive",
                          approval_provider=lambda p: Decision(False, "", True, ""))
        g = _graph([_step("s01", "click"),
                    _step("s02", "type", by="automation_id", value="txt",
                          params={"value": "abc"})])
        r = _runner(g, gate=gate)
        r.cfg.pre_run_ready_s = 0
        rec = await asyncio.wait_for(r.run(), timeout=5)
        assert rec.status == "succeeded", (rec.status, rec.error_code)
        assert all(s.status == "done" for s in rec.steps)


    def test_default_policy_never_asks(self):
        """默认策略 never：回放中途不弹任何确认（含高风险动作），审计留痕。"""
        from harness.modules.desktop_repeat.safety_gate import Decision, SafetyGate

        asked: list[dict] = []

        def _provider(payload):
            asked.append(payload)
            return Decision(False, "", True, "")

        gate = SafetyGate(mode="interactive", approval_provider=_provider)
        for risk, kind in (("write", "image"), ("dangerous", "")):
            d = gate.check(run_id="rn", sid="s01", action="click", risk=risk,
                           app_hint={}, params={}, scope_ok=True,
                           hit_kind=kind, hit_index=2, degraded=bool(kind))
            assert d.allow is True, f"{risk} 仍然要求确认"
        assert asked == [], "默认策略下不应调用审批提供方"


@pytest.mark.asyncio
async def test_type_readback_mismatch_does_not_abort_run():
    """回归（真实 Chrome 地址栏暴露）：读回不一致 ≠ 输入失败。

    Chrome omnibox 的 SetValue 常被拒，实际输入靠粘贴/unicode 完成，而
    ValuePattern 只读导致读回不可靠。旧实现直接 RP-5003 挂起整轮——
    真机现象是"地址栏明明已经输入 dify，却报内容不一致并停止"。
    """
    from harness.modules.desktop_repeat.executor.actions import ActionResult  # noqa: F401

    class MismatchInspector(FakeInspector):
        def is_editable(self, element, timeout_ms=500) -> bool:
            return True

        def set_value(self, element, text, timeout_ms=1000) -> bool:
            return True

        def read_value(self, element, timeout_ms=500):
            return "地址和搜索栏"   # 模拟"读回的是控件名而不是值"

    g = _graph([_step("s01", "type", by="automation_id", value="omnibox",
                      params={"value": "dify"})])
    inj = FakeInjector()
    r = _runner(g, injector=inj, inspector=MismatchInspector({"automation_id:omnibox": "hit"}))
    r.cfg.pre_run_ready_s = 0
    rec = await asyncio.wait_for(r.run(), timeout=5)
    assert rec.status == "succeeded", (rec.status, rec.error_code, rec.steps[-1].message)
    assert rec.steps[0].degraded is True, "读回不一致应标记为降级"
    assert "读回" in (rec.steps[0].message or ""), rec.steps[0].message
    # 通道 1（SetValue）已生效：不应再产生任何坐标点击
    assert not any(c[0] == "click" for c in inj.calls), inj.calls


def test_window_focus_skips_when_target_window_missing():
    """回归（真机坐实）：window_focus 找不到目标窗口时应跳过，而不是失败。

    录制起点常落在桌面/资源管理器，回放时该窗口已关闭；旧实现返回
    RP-5001 让整个流程停在第 1 步之前。
    """
    from harness.modules.desktop_repeat.executor.actions import ActionContext, act_focus
    from harness.modules.desktop_repeat.models import StepDef

    ctx = ActionContext(inspector=None, injector=None, windows=None, grabber=None,
                        budget=None, dry_run=False)
    step = StepDef(sid="s01", action="window_focus", evidence={})
    res = act_focus(ctx, step, None)
    assert res.ok is True
    assert res.channel == "skip"


def test_container_click_uses_recorded_coord():
    """回归（真机坐实）：UIA 命中容器时要用录制坐标，而不是元素中心。

    工具栏里的「新建」按钮在 UIA 中通常只暴露整个工具栏为一个 Pane；按元素
    中心点击会落到工具栏中间的其他按钮上（真机：EditPlus 新建文档，
    回放点了工具栏中心，页面根本没有新建）。
    """
    from harness.modules.desktop_repeat.models import Hit, LocatorSpec, StepDef

    step = StepDef(sid="s01", action="click", app_hint={"exe": "app.exe"},
                   locators=[LocatorSpec(kind="uia", by="name_type",
                                         value={"name": "标准", "control_type": "Pane"}),
                             LocatorSpec(kind="coord", pos=(27, 76), scale=1.0,
                                         screen=(1920, 1080))],
                   risk="write")
    r = _runner(_graph([step.to_dict()]))
    hit = Hit(rect=(0, 60, 800, 90), center=(400, 75), element="tok:x", kind="uia")
    r._correct_hit_with_recorded_coord(step, hit)  # noqa: SLF001
    assert hit.center == (27, 76), "未改用录制坐标"
    assert hit.kind == "coord"
    assert hit.diagnostics.get("coord_corrected") is True


def test_click_skips_uia_invoke_when_coord_corrected():
    """坐标校正后不得再 invoke 容器：invoke 工具栏不会触发内部按钮。"""
    from harness.modules.desktop_repeat.executor.actions import ActionContext, act_click
    from harness.modules.desktop_repeat.models import Hit, StepDef

    class Inv(FakeInspector):
        def __init__(self) -> None:
            super().__init__({"name_type:标准": "hit"})
            self.invoked = 0

        def invoke(self, element, timeout_ms=1000) -> bool:
            self.invoked += 1
            return True

    insp = Inv()
    inj = FakeInjector()
    ctx = ActionContext(inspector=insp, injector=inj, windows=FakeWindows(),
                        grabber=None, budget=None, dry_run=False)
    hit = Hit(rect=(0, 60, 800, 90), center=(27, 76), element="tok:x", kind="coord",
              diagnostics={"coord_corrected": True})
    res = act_click(ctx, StepDef(sid="s01", action="click"), hit)
    assert insp.invoked == 0, "坐标校正后仍调用了 UIA invoke"
    assert res.channel in ("coord", "pattern_fallback")
    assert ("click", (27, 76, "left", 1)) in inj.calls, inj.calls


def test_coord_used_when_uia_hit_is_wrong_element():
    """回归（真机坐实）：UIA 抓到别的元素时，录制坐标才是唯一可信位置。

    EditPlus 点「另存为 → 保存」按钮：模态对话框弹出后 ElementFromPoint 返回的
    是背后的文档区（Pane『po』），若按该元素中心点击等于点文档，保存永远不会
    发生。此时必须直接用录制坐标 (1293,747)。
    """
    from harness.modules.desktop_repeat.models import Hit, LocatorSpec, StepDef

    step = StepDef(sid="s09", action="click", app_hint={"exe": "editplus.exe"},
                   locators=[LocatorSpec(kind="uia", by="name_type",
                                         value={"name": "po", "control_type": "Pane"}),
                             LocatorSpec(kind="coord", pos=(1293, 747), scale=1.0,
                                         screen=(1920, 1080))],
                   risk="write")
    r = _runner(_graph([step.to_dict()]))
    # UIA 命中文档区：rect 完全不含保存按钮坐标
    hit = Hit(rect=(0, 100, 600, 500), center=(300, 300), element="tok:x", kind="uia")
    r._correct_hit_with_recorded_coord(step, hit)  # noqa: SLF001
    assert hit.center == (1293, 747), "UIA 误命中时未改用录制坐标"
    assert hit.kind == "coord"
