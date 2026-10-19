"""desktop_repeat 测试共享假平台件（FakeUiBridge 风格）。

纯 Python 替身：不触碰真实钩子/UIA/注入（真机行为在 e2e/回归层验证）。
executor 与服务层测试共用，避免重复定义漂移。
"""

from __future__ import annotations

import threading
from typing import Any

from harness.modules.desktop_repeat.bus import ProgressStore, Reporter
from harness.modules.desktop_repeat.config import RepeatConfig
from harness.modules.desktop_repeat.executor.runner import RepeatRunner
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


class FakeInspector:
    """按 (by,value) 裁决 hit/miss；hit 返回 token，type 动作读回走 SetValue 通道。"""

    def __init__(self, hits: dict[str, str]) -> None:
        self.hits = hits
        self.readbacks: dict[str, str] = {}
        self.pattern_ok = {"invoke": True, "set_value": True}
        self.elements: dict[str, Any] = {}

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
        """默认视为可写控件；需要模拟"锚到窗口容器"时把元素放进 non_editable。"""
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
    """第 n 次之后由 timeout 转 hit（瞬态重试路径）。"""

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
        self.enabled = True

    def foreground(self):
        return self.win

    def find_window(self, hints: dict) -> WindowRef | None:
        # 宽容版：恒返回当前窗口（exe 一致即可）。作用域按标题隔离、exe 兜底
        # 拒写等真实语义由 test_desktop_repeat_{executor,audit} 的专用假件覆盖，
        # 不在共享假件里掺入，避免各套件互相牵制。
        return self.win

    def focus(self, win) -> bool:
        return True

    def restore(self, win) -> None:
        pass

    def alive(self, hwnd, timeout_ms=5000) -> bool:
        return True

    def is_enabled(self, hwnd) -> bool:
        return self.enabled

    def enumerate(self, pid=None):
        return [self.win]


def base_env() -> Env:
    return Env(os="Test", virtual_screen=(0, 0, 1920, 1080),
               monitors=[MonitorInfo(index=1, rect=(0, 0, 1920, 1080), work=(0, 0, 1920, 1040),
                                     dpi=96, scale=1.0)])


class FakeEnv:
    def __init__(self, env: Env | None = None) -> None:
        self.env = env or base_env()

    def snapshot(self) -> Env:
        return self.env

    def graphics_available(self) -> bool:
        return True

    def desktop_name(self) -> str:
        return "Default"

    def is_elevated(self) -> bool:
        return False


class FakeLease:
    def __init__(self) -> None:
        self.held = False

    def acquire(self):
        if self.held:
            return False, 1
        self.held = True
        return True, 0

    def release(self) -> None:
        self.held = False


class StepDB:
    """repeat_runs / step_hits / audit 的内存替身（repository 契约）。"""

    def __init__(self) -> None:
        self.runs: dict[str, dict] = {}
        self.steps: list[dict] = []
        self.audit: list[dict] = []

    def insert_run(self, run_id, workflow_id, doc, *, steps_total):
        self.runs[run_id] = {**doc, "status": "running", "total_steps": steps_total, "steps": []}

    def finish_run(self, run_id, rec):
        d = rec.to_dict() if hasattr(rec, "to_dict") else dict(rec)
        self.runs[run_id] = {**self.runs.get(run_id, {}), **d}

    def record_step(self, run_id, sr):
        d = sr.to_dict() if hasattr(sr, "to_dict") else dict(sr)
        self.steps.append({"run_id": run_id, **d})

    def get_run(self, run_id):
        r = self.runs.get(run_id)
        return dict(r) if r else None

    def list_runs(self, workflow_id=None, limit=50):
        return list(self.runs.values())[:limit]

    def insert_audit(self, rec):
        self.audit.append(rec)

    def insert_approval(self, run_id, payload):
        self.audit.append({"kind": "approval", "run_id": run_id, **payload})


def make_step(sid: str, action: str, *, by: str = "automation_id", value: Any = "btn",
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
                                  "backoff_ms": 30, "idempotent": True},
                timing=timing or {}, assertion=assertion)
    return s.to_dict()


def make_graph(steps: list[dict], params_defs: list[dict] | None = None,
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
            "meta": {"env_base": (env_base or base_env()).to_dict()}}


def make_runner(graph: dict, *, inspector=None, injector=None, windows=None, env=None,
                gate=None, store=None, unattended=False, run_root=None,
                resolved=None, flags=None, **kw) -> RepeatRunner:
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
        run_root=run_root, unattended=unattended, **kw)


async def wait_status(r: RepeatRunner, status: str, *, timeout: float = 8.0) -> None:
    import asyncio
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while r.rec.status != status and loop.time() < end:
        if r.rec.status in ("succeeded", "failed", "aborted"):
            return
        await asyncio.sleep(0.01)


async def run_until_suspended(r: RepeatRunner, *, timeout: float = 8.0) -> Any:
    """启动 run()，等待 suspended/终态，返回未完成任务。

    关键：resume() 竞态安全——先挂起再裁决；测试侧用 await 前轮询状态。
    """
    import asyncio
    task = asyncio.create_task(r.run())
    await wait_status(r, "suspended", timeout=timeout)
    return task


__all__ = [
    "FakeInspector", "FlakyInspector", "FakeInjector", "FakeWindows", "FakeEnv",
    "FakeLease", "StepDB", "base_env", "make_step", "make_graph", "make_runner",
    "wait_status", "run_until_suspended",
]
