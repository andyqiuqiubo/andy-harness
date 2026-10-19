"""Win32 UIA 定位层回归测试（不依赖真实桌面）。

锁定阶段一真机验证发现的三类缺陷（均由真实桌面复现，假件单测此前无法覆盖）：
1. ``Control.Exists()`` 关键字参数名在 uiautomation 各版本不一致 → 必须按位传参；
2. uiautomation 的 ``Compare`` 回调以 ``(control, depth)`` 两个位置参数调用；
3. UIA worker 冷启动存在就绪竞态 → ``_submit`` 必须等待就绪而不是立即失败。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from harness.modules.desktop_repeat.models import LocatorSpec
from harness.modules.desktop_repeat.platform.win32.pathmatch import match_path
from harness.modules.desktop_repeat.platform.win32.uia import WinUiInspector


class _Cand:
    """候选控件替身：Exists 只接受位置参数（模拟新版 uiautomation 签名）。"""

    def __init__(self, name: str = "Edit") -> None:
        self.Name = name
        self.ControlTypeName = "EditControl"
        self.AutomationId = "1001"
        self.ClassName = "Edit"
        self.calls: list[tuple[Any, ...]] = []

    def Exists(self, maxSearchSeconds: float = 5, searchIntervalSeconds: float = 0.05,
               *args: Any, **kwargs: Any) -> bool:
        # 位置参数才合法；一旦以 sleepIntervalSeconds= 传参即抛 TypeError
        if kwargs:
            raise TypeError("unexpected keyword: " + ",".join(kwargs))
        self.calls.append((maxSearchSeconds, searchIntervalSeconds))
        return True


class _Root:
    def __init__(self, cand: _Cand, compare_arity: int = 2) -> None:
        self._cand = cand
        self._arity = compare_arity

    def Control(self, **kwargs: Any) -> _Cand:
        cmp = kwargs.get("Compare")
        if cmp is not None:
            # 真实 uiautomation 以 (control, depth) 回调；arity=2 模拟该行为
            args = (self._cand, 0) if self._arity == 2 else (self._cand,)
            if not cmp(*args):
                raise AssertionError("Compare 回调未通过（签名不兼容？）")
        return self._cand


def test_find_class_index_uses_positional_exists_args() -> None:
    cand = _Cand()
    insp = WinUiInspector()
    spec = LocatorSpec(kind="uia", by="class_index",
                       value={"class_name": "Edit", "index": 1}, timeout_ms=1000)
    got = insp._find(_Root(cand), spec, 1.0)  # noqa: SLF001
    assert got is cand
    assert cand.calls, "Exists 未被调用"


def test_find_name_type_compare_accepts_depth_argument() -> None:
    cand = _Cand("Submit")
    cand.ControlTypeName = "ButtonControl"
    insp = WinUiInspector()
    spec = LocatorSpec(kind="uia", by="name_type",
                       value={"name": "Submit", "control_type": "Button"}, timeout_ms=1000)
    # Compare 若只声明一个参数，会因 (control, depth) 两参数调用而 TypeError
    got = insp._find(_Root(cand, compare_arity=2), spec, 1.0)  # noqa: SLF001
    assert got is cand


# ── 原生 FindFirst 快路径（RP-2001「地址栏 timeout 误判」主修） ────
class _NativeRoot:
    """带 Element 句柄的 root：模拟真实 uiautomation Control。

    _native_find 先探测 root.Element，再建 COM 客户端——本假件的 Element
    是非零哨兵，会进入原生分支；随后原生通道在本环境不可用（无真实
    COM/常量异常），必须安全回退到 root.Control(...)，绝不抛错。
    """

    def __init__(self, cand: _Cand) -> None:
        self.Element = object()  # 非 None 哨兵
        self._cand = cand

    def Control(self, **kwargs: Any) -> _Cand:  # noqa: ARG002
        return self._cand


def test_native_find_unavailable_falls_back_to_python_walk() -> None:
    """原生通道不可用时行为与旧实现完全一致（Compare 遍历仍可命中）。"""
    cand = _Cand("地址和搜索栏")
    insp = WinUiInspector()
    # 强制标记原生通道不可用，模拟旧版 uiautomation 缺常量的环境
    insp._native_cache = False  # noqa: SLF001
    spec = LocatorSpec(kind="uia", by="name_type",
                       value={"name": "地址和搜索栏", "control_type": "Edit"},
                       timeout_ms=1000)
    got = insp._find(_NativeRoot(cand), spec, 1.0)  # noqa: SLF001
    assert got is cand


def test_fake_root_without_element_never_touches_com() -> None:
    """假件 root（无 Element 属性）必须直接走 Python 遍历——
    保证纯 Python 单测零真实 COM 依赖（本沙箱 UIA 不可用）。"""
    cand = _Cand()
    insp = WinUiInspector()
    spec = LocatorSpec(kind="uia", by="automation_id", value="1001", timeout_ms=1000)
    got = insp._find(_Root(cand), spec, 1.0)  # noqa: SLF001  旧假件无 .Element
    assert got is cand


def test_match_path_compare_accepts_depth_argument() -> None:
    cand = _Cand("Edit")
    root = _Root(cand, compare_arity=2)
    got = match_path(None, root, "Pane[桌面 1]/Window[Demo]/Edit", timeout_secs=1.0)
    assert got is cand


class _SlowInspector(WinUiInspector):
    """模拟冷启动：worker 0.25s 后就绪，旧实现会在 0s 立即返回 not ready。"""

    def __init__(self, delay: float = 0.25, init_error: BaseException | None = None) -> None:
        super().__init__()
        self._delay = delay
        self._forced_error = init_error

    def _worker_loop(self) -> None:  # type: ignore[override]
        time.sleep(self._delay)
        if self._forced_error is not None:
            self._init_error = self._forced_error
        self._ready = True
        self._ready_evt.set()
        while self._forced_error is None:
            task = self._q.get()
            if task is None:
                return
            task.run()


def test_submit_waits_for_worker_readiness() -> None:
    insp = _SlowInspector(delay=0.25)
    t0 = time.perf_counter()
    ok, res, err = insp._submit(lambda: 42, 1000)  # noqa: SLF001
    elapsed = time.perf_counter() - t0
    assert ok and res == 42 and err is None
    assert elapsed >= 0.2, "未等待 worker 就绪"


def test_submit_reports_worker_init_error() -> None:
    insp = _SlowInspector(delay=0.01, init_error=RuntimeError("COM 初始化失败"))
    ok, _res, err = insp._submit(lambda: 1, 1000)  # noqa: SLF001
    assert not ok and isinstance(err, RuntimeError)
    assert "初始化失败" in str(err)


def test_submit_times_out_when_worker_never_ready() -> None:
    insp = _SlowInspector(delay=5.0)
    t0 = time.perf_counter()
    # 请求预算 200ms → 就绪等待按预算收敛（不无限挂起）
    ok, _res, err = insp._submit(lambda: 1, 200)  # noqa: SLF001
    assert not ok and isinstance(err, TimeoutError)
    assert time.perf_counter() - t0 < 1.5
    insp._ready = True  # 清理：避免 daemon 线程后续行为干扰
    insp._ready_evt.set()
    threading.Event().wait(0)


# ── 钩子层：AltGr 组合下的键名回退 ────────────────────────────────
from harness.modules.desktop_repeat.platform.win32.capture import (  # noqa: E402
    PynputCapture,
    _name_from_vk,
)


class _FakeKey:
    """模拟 pynput 在 Ctrl+Alt+字母（AltGr）下的 KeyCode：char 为 None。"""

    def __init__(self, vk: int) -> None:
        self.name = None
        self.char = None
        self.vk = vk


def test_vk_to_name_mapping() -> None:
    assert _name_from_vk(0x52) == "r"       # R
    assert _name_from_vk(0x41) == "a"       # A
    assert _name_from_vk(0x31) == "1"
    assert _name_from_vk(0x70) == "f1"
    assert _name_from_vk(0x79) == "f10"
    assert _name_from_vk(0xBA) == ";"
    assert _name_from_vk(0x99) == ""        # 未知键不臆造


def test_key_event_uses_vk_when_char_missing() -> None:
    cap = PynputCapture()
    cap._key_event(_FakeKey(0x52), True)  # noqa: SLF001
    raw = cap.drain(1)
    assert raw and raw[0]["name"] == "r"
    assert raw[0]["down"] is True
    assert "focus_hwnd" in raw[0]


def test_find_window_without_pid_never_matches_any_window():
    """回归（真机坐实）：没有 pid 时不得退化成"匹配任意窗口"。

    旧实现的 pid 级匹配在 pid 为空时仍会遍历候选，配合 ".*" 标题正则
    返回枚举到的第一个窗口——于是 app_hint 指向 explorer.exe，作用域却被
    设成 EditPlus/Chrome，紧接着 _scope_ok 以 RP-4002 拒绝（0/15 步就失败）。
    """
    from harness.modules.desktop_repeat.platform import make_windows

    w = make_windows()
    assert w.find_window({"exe": "definitely_not_running_zzz.exe",
                          "window_title_regex": ".*"}) is None
