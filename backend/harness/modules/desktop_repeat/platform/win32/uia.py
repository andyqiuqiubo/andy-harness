"""UIA 惰性查询（自研，构建于系统 UIA API 之上）。

设计约束落地（§3.5 / ADR-08）：
- COM 调用收敛到**单一 worker 线程**（UIA 套间亲和）；对外元素引用一律是整数
  token，绝不把 COM 指针跨线程传递；
- 每次调用硬超时：worker 执行、调用方限时等待；超时即放弃（迟到结果里的 token
  由注册表清扫回收），**永不阻塞事件流**；
- 快照只取最小属性集 + 祖先链 + 可用 Pattern；is_password 时 value 恒为空；
- Control(...) 工厂返回的是"待搜索条件对象"，必须经 .Exists() 真正查找。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from ...models import Hit, LocatorSpec, Miss, UiaSnapshot
from ..base import UiInspector

try:
    import uiautomation as auto
    _UIA_OK = True
except Exception:  # noqa: BLE001
    auto = None  # type: ignore[assignment]
    _UIA_OK = False

_PATTERN_NAMES = (
    "InvokePattern", "ValuePattern", "TogglePattern", "SelectionItemPattern",
    "ScrollPattern", "ScrollItemPattern", "TextPattern", "ExpandCollapsePattern",
    "RangeValuePattern", "LegacyIAccessiblePattern", "TransformPattern",
)
_MAX_ANCESTOR_DEPTH = 8
_REGISTRY_SWEEP_AT = 256  # token 注册表清扫阈值（超时竞态遗留的兜底回收）
_READY_WAIT_MAX_S = 5.0   # worker 冷启动等待上限（UIA COM 首次初始化可达数秒）

logger = logging.getLogger("harness.repeat.uia")


class _Task:
    __slots__ = ("fn", "result", "error", "done", "abandoned")

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.result: Any = None
        self.error: BaseException | None = None
        self.done = threading.Event()
        self.abandoned = False

    def run(self) -> None:
        try:
            self.result = self.fn()
        except BaseException as e:  # noqa: BLE001
            self.error = e
        finally:
            self.done.set()


def _short_type(ctrl_ctype: str) -> str:
    """'EditControl' → 'Edit'（与设计 §3.5 control_type 词汇一致）。"""
    s = str(ctrl_ctype or "")
    if s.endswith("Control"):
        s = s[: -len("Control")]
    return s


class WinUiInspector(UiInspector):
    """单 worker 线程 + token 注册表的 UIA 门面。"""

    def __init__(self, max_pending: int = 3) -> None:
        self._q: queue.Queue[_Task | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._controls: dict[int, Any] = {}
        self._token_seq = 0
        self._max_pending = max_pending
        self._started = False
        self._ready = False
        self._ready_evt = threading.Event()
        self._init_error: BaseException | None = None
        self._start_lock = threading.Lock()
        # 原生 IUIAutomation 通道缓存：None=未初始化，(IUIAutomation, core)=可用，
        # False=不可用（常量缺失/COM 异常），此后只走 Python 遍历。
        self._native_cache: Any = None

    # ── 生命周期 ───────────────────────────────────────────────────
    def available(self) -> bool:
        return _UIA_OK

    def _ensure_started(self) -> None:
        if self._started or not _UIA_OK:
            return
        with self._start_lock:
            if self._started:
                return
            self._started = True
            self._thread = threading.Thread(target=self._worker_loop, name="repeat-uia", daemon=True)
            self._thread.start()
        # 不在此阻塞：就绪等待下沉到 _submit（按请求预算等待），
        # 避免"冷启动未就绪 → 首个 UIA 定位直接失败、静默降级到图像/坐标"。

    def _worker_loop(self) -> None:
        if not _UIA_OK:
            return
        try:
            auto.InitializeUIAutomationInCurrentThread()
        except Exception as e:  # noqa: BLE001
            self._init_error = e
            logger.warning("UIA 线程初始化失败（定位链将整体降级）: %s", e)
        finally:
            self._ready = True
            self._ready_evt.set()
        while True:
            task = self._q.get()
            if task is None:
                break
            task.run()
            if task.abandoned:
                self._release_result_tokens(task.result)
            if len(self._controls) > _REGISTRY_SWEEP_AT:
                # 竞态泄漏兜底：只保留最近 64 个（活动 token 均为近期产物）
                keep = sorted(self._controls)[-64:]
                for k in list(self._controls):
                    if k not in set(keep):
                        self._controls.pop(k, None)
        try:
            auto.UninitializeUIAutomationInCurrentThread()
        except Exception:  # noqa: BLE001
            pass

    def _release_result_tokens(self, result: Any) -> None:
        if isinstance(result, int):
            self._controls.pop(result, None)
        elif isinstance(result, tuple):
            for item in result:
                if isinstance(item, int):
                    self._controls.pop(item, None)

    def _submit(self, fn: Callable[[], Any], timeout_ms: int) -> tuple[bool, Any, BaseException | None]:
        if not _UIA_OK:
            return False, None, RuntimeError("uiautomation 未安装（pip install 'harness[repeat]'）")
        self._ensure_started()
        # 冷启动等待：worker 首次 COM 初始化可能耗时 1–3s。按请求预算等待（上限 5s），
        # 而不是立即返回 not ready——否则首个定位必然失败并静默降级（实测缺陷）。
        wait_s = min(_READY_WAIT_MAX_S, max(0.05, timeout_ms / 1000.0))
        if not self._ready_evt.wait(wait_s):
            return False, None, TimeoutError("uia worker 启动超时")
        if self._init_error is not None:
            return False, None, RuntimeError(f"uia worker 初始化失败: {self._init_error}")
        if self._q.qsize() >= self._max_pending:
            return False, None, TimeoutError("uia backlog")
        task = _Task(fn)
        self._q.put(task)
        if task.done.wait(timeout=max(0.001, timeout_ms / 1000.0)):
            if task.error is not None:
                return False, None, task.error
            return True, task.result, None
        task.abandoned = True
        return False, None, TimeoutError("uia timeout")

    # ── token 注册表（仅 worker 线程读写） ─────────────────────────
    def _register(self, ctrl: Any) -> int:
        self._token_seq += 1
        self._controls[self._token_seq] = ctrl
        return self._token_seq

    def _get(self, token: Any) -> Any:
        try:
            return self._controls.get(int(token))
        except (TypeError, ValueError):
            return None

    def release(self, token: int) -> None:
        try:
            self._controls.pop(int(token), None)
        except (TypeError, ValueError):
            pass

    # ── 快照抽取 ───────────────────────────────────────────────────
    def _rect_of(self, ctrl: Any) -> tuple[int, int, int, int]:
        try:
            r = ctrl.BoundingRectangle
            return (int(r.left), int(r.top), int(r.right), int(r.bottom))
        except Exception:  # noqa: BLE001
            return (0, 0, 0, 0)

    def _patterns_of_ctrl(self, ctrl: Any) -> list[str]:
        out: list[str] = []
        for name in _PATTERN_NAMES:
            pid = getattr(auto.PatternId, name, None)
            if pid is None:
                continue
            try:
                if ctrl.GetPattern(pid) is not None:
                    out.append(name)
            except Exception:  # noqa: BLE001
                continue
        return out

    def _ancestry(self, ctrl: Any) -> tuple[list[str], int, str]:
        """回溯祖先，返回 (每层 'Type:Name', 层数, 自顶向下 path)。"""
        names: list[str] = []
        cur = ctrl
        depth = 0
        while cur is not None and depth < _MAX_ANCESTOR_DEPTH + 1:
            try:
                ctype = _short_type(str(cur.ControlTypeName or ""))
                nm = str(cur.Name or "")
            except Exception:  # noqa: BLE001
                break
            names.append(f"{ctype}:{nm}")
            try:
                parent = cur.GetParentControl()
            except Exception:  # noqa: BLE001
                parent = None
            if parent is None:
                break
            cur = parent
            depth += 1
        path_parts: list[str] = []
        for n in reversed(names):
            ctype, _, nm = n.partition(":")
            path_parts.append(f"{ctype}[{nm}]" if nm else ctype)
        return names, len(names) - 1, "/".join(path_parts)

    def _read_value(self, ctrl: Any) -> str:
        try:
            if bool(ctrl.IsPassword):
                return ""
        except Exception:  # noqa: BLE001
            return ""
        vp = getattr(auto.PatternId, "ValuePattern", None)
        try:
            pat = ctrl.GetPattern(vp) if vp is not None else None
            if pat is not None:
                return str(pat.Value or "")[:4096]
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _read_value_opt(self, ctrl: Any) -> str | None:
        """读控件当前值；没有可读 ValuePattern 时返回 None（**不回退控件 Name**）。

        旧实现读不到值就回退 Name，Chrome 地址栏这类"有 ValuePattern 但 SetValue
        被拒"的控件会读回名称，输入校验随即误判"内容不一致"（真机坐实：地址栏
        输入 dify 实际已生效，却报 RP-5003）。
        """
        vp = getattr(auto.PatternId, "ValuePattern", None)
        try:
            pat = ctrl.GetPattern(vp) if vp is not None else None
            if pat is not None:
                return str(pat.Value or "")[:4096]
        except Exception:  # noqa: BLE001
            pass
        return None

    def _snapshot(self, ctrl: Any) -> UiaSnapshot:
        patterns = self._patterns_of_ctrl(ctrl)
        ancestors, depth, path = self._ancestry(ctrl)
        try:
            ctype = _short_type(str(ctrl.ControlTypeName or ""))
        except Exception:  # noqa: BLE001
            ctype = ""
        try:
            is_pw = bool(ctrl.IsPassword)
        except Exception:  # noqa: BLE001
            is_pw = False
        is_editable = any(p in ("ValuePattern", "TextPattern", "RangeValuePattern") for p in patterns)
        value = ""
        if is_editable and not is_pw:
            value = self._read_value(ctrl)
        return UiaSnapshot(
            control_type=ctype,
            name=str(getattr(ctrl, "Name", "") or ""),
            automation_id=str(getattr(ctrl, "AutomationId", "") or ""),
            class_name=str(getattr(ctrl, "ClassName", "") or ""),
            framework_id=str(getattr(ctrl, "FrameworkId", "") or ""),
            path=path, depth=depth,
            is_password=is_pw,
            is_enabled=bool(getattr(ctrl, "IsEnabled", True)),
            is_offscreen=bool(getattr(ctrl, "IsOffscreen", False)),
            patterns=patterns,
            rect=self._rect_of(ctrl),
            ancestors=ancestors,
            is_editable=is_editable,
            value=value,
        )

    # ── 协议实现 ───────────────────────────────────────────────────
    def warmup(self) -> None:
        def _do() -> None:
            fg = auto.GetForegroundControl()
            if fg is not None:
                r = self._rect_of(fg)
                if r[2] > r[0] and r[3] > r[1]:
                    auto.ControlFromPoint((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
        self._submit(_do, 3000)

    def element_from_point(self, x: int, y: int, timeout_ms: int) -> UiaSnapshot | None:
        def _do() -> UiaSnapshot:
            ctrl = auto.ControlFromPoint(int(x), int(y))
            if ctrl is None:
                raise LookupError("no element at point")
            return self._snapshot(ctrl)
        ok, res, _ = self._submit(_do, timeout_ms)
        return res if ok and isinstance(res, UiaSnapshot) else None

    def element_token_from_point(self, x: int, y: int, timeout_ms: int) -> tuple[int | None, UiaSnapshot | None]:
        def _do() -> tuple[int, UiaSnapshot]:
            ctrl = auto.ControlFromPoint(int(x), int(y))
            if ctrl is None:
                raise LookupError("no element at point")
            tok = self._register(ctrl)
            return tok, self._snapshot(ctrl)
        ok, res, _ = self._submit(_do, timeout_ms)
        if ok and isinstance(res, tuple):
            tok, snap = res
            return int(tok), snap
        return None, None

    def focused_element(self, timeout_ms: int = 400) -> tuple[int | None, UiaSnapshot | None]:
        """当前焦点元素（IME 归并的差分对象）。"""
        def _do() -> tuple[int, UiaSnapshot]:
            ctrl = auto.GetFocusedControl()
            if ctrl is None:
                raise LookupError("no focus")
            tok = self._register(ctrl)
            return tok, self._snapshot(ctrl)
        ok, res, _ = self._submit(_do, timeout_ms)
        if ok and isinstance(res, tuple):
            tok, snap = res
            return int(tok), snap
        return None, None

    def element_token_from_handle(self, hwnd: int, timeout_ms: int = 400
                                  ) -> tuple[int | None, UiaSnapshot | None]:
        """按控件句柄取快照（录制期键事件锚定：句柄在输入发生时采集，语义稍后补齐）。"""
        if not hwnd:
            return None, None

        def _do() -> tuple[int, UiaSnapshot]:
            ctrl = auto.ControlFromHandle(int(hwnd))
            if ctrl is None:
                raise LookupError("no control for hwnd")
            tok = self._register(ctrl)
            return tok, self._snapshot(ctrl)

        ok, res, _ = self._submit(_do, timeout_ms)
        if ok and isinstance(res, tuple):
            tok, snap = res
            return int(tok), snap
        return None, None

    def element_by_spec(self, spec: LocatorSpec, scope: dict[str, Any] | None,
                        timeout_ms: int | None = None) -> Hit | Miss:
        t0 = time.perf_counter()
        budget = timeout_ms or spec.timeout_ms or 2000

        def _do() -> tuple[int, tuple[int, int, int, int]]:
            root = self._scope_root(scope)
            secs = max(0.05, budget / 1000.0)
            ctrl = self._find(root, spec, secs)
            if ctrl is None:
                raise LookupError("not_found")
            try:
                if not bool(ctrl.IsEnabled):
                    raise PermissionError("disabled")
            except PermissionError:
                raise
            except Exception:  # noqa: BLE001
                pass
            rect = self._rect_of(ctrl)
            if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
                raise LookupError("zero_rect")
            tok = self._register(ctrl)
            return tok, rect

        ok, res, err = self._submit(_do, budget + 300)
        elapsed = int((time.perf_counter() - t0) * 1000)
        if ok and isinstance(res, tuple):
            tok, rect = res
            return Hit(rect=rect, center=((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2),
                       element=tok, kind=spec.kind, score=spec.confidence,
                       elapsed_ms=elapsed, diagnostics={"by": spec.by})
        reason = "timeout" if isinstance(err, TimeoutError) else "not_found"
        if isinstance(err, PermissionError):
            reason = "disabled"
        return Miss(kind=spec.kind, reason=reason, elapsed_ms=elapsed,
                    diagnostics={"by": spec.by, "error": str(err) if err else ""})

    # ── 查找细节 ───────────────────────────────────────────────────
    def _scope_root(self, scope: dict[str, Any] | None) -> Any:
        hwnd = int((scope or {}).get("hwnd", 0) or 0)
        if hwnd:
            try:
                c = auto.ControlFromHandle(hwnd)
                if c is not None:
                    return c
            except Exception:  # noqa: BLE001
                pass
        return auto.GetRootControl()

    def _native_ctx(self) -> tuple[Any, Any] | None:
        """(IUIAutomation, UIAutomationCore) 缓存；通道不可用返回 None。

        单例首次调用即在 UIA worker 线程创建——与 Python 遍历路径
        （FindControl/ViewWalker）同一生命周期，不引入新的跨套间风险。
        """
        if self._native_cache is False:
            return None
        if self._native_cache is None:
            try:
                from uiautomation.uiautomation import _AutomationClient
                client = _AutomationClient.instance()
                self._native_cache = (client.IUIAutomation, client.UIAutomationCore)
            except Exception as e:  # noqa: BLE001
                logger.debug("UIA 原生通道不可用（将用 Python 遍历兜底）: %s", e)
                self._native_cache = False
                return None
        return self._native_cache

    def _native_find(self, root: Any, *, name: str = "", ctype: str = "",
                     aid: str = "", cls: str = "", index: int = 1
                     ) -> tuple[bool, Any]:
        """原生 IUIAutomationElement.FindFirst/FindAll 快路径。

        返回 (handled, control)：
        - handled=False：原生通道不可用（常量缺失/COM 异常/root 无句柄），
          调用方应回退旧 Python 遍历；
        - handled=True 且 control=None：**原生确认子树内不存在**该元素。
          旧 Python 遍历对同一棵树只会得出同样结论，却要多耗完整搜索预算
          （真机坐实：Chrome 单 HWND 深树，Compare 遍历 2s 跑不完 →
          地址栏明明在，却被记成 timeout → RP-2001「定位链耗尽」）。

        仅在 worker 线程调用（经 _find）。
        """
        if not _UIA_OK:
            return False, None
        try:
            root_el = getattr(root, "Element", None)
        except Exception:  # noqa: BLE001
            return False, None
        if root_el is None:
            # 假件 root / 未解析句柄：不建 COM 客户端，直接回退 Python 遍历
            # （保证纯 Python 单测零真实桌面依赖）。
            return False, None
        ctx = self._native_ctx()
        if ctx is None:
            return False, None
        iuia, core = ctx
        try:
            conds = []
            if aid:
                conds.append(iuia.CreatePropertyCondition(core.UIA_AutomationIdPropertyId, str(aid)))
            if name:
                conds.append(iuia.CreatePropertyCondition(core.UIA_NamePropertyId, str(name)))
            if cls:
                conds.append(iuia.CreatePropertyCondition(core.UIA_ClassNamePropertyId, str(cls)))
            if ctype:
                tid = getattr(core, f"UIA_{ctype}ControlTypeId", None)
                if tid is None:
                    return False, None  # 不认识的控件类型：回退 Python 遍历
                conds.append(iuia.CreatePropertyCondition(core.UIA_ControlTypePropertyId, tid))
            if not conds:
                return False, None
            cond = conds[0]
            for extra in conds[1:]:
                cond = iuia.CreateAndCondition(cond, extra)
            scope = core.TreeScope_Descendants
            if index > 1:
                arr = root_el.FindAll(scope, cond)
                if arr is None or arr.Length < index:
                    return True, None
                el = arr.GetElement(index - 1)
            else:
                el = root_el.FindFirst(scope, cond)
            if el is None:
                return True, None
            ctrl = auto.Control.CreateControlFromElement(el)
            if ctrl is None:
                return False, None
            return True, ctrl
        except Exception as e:  # noqa: BLE001
            logger.debug("UIA 原生查找异常（回退 Python 遍历）: %s", e)
            return False, None

    def _find(self, root: Any, spec: LocatorSpec, timeout_secs: float) -> Any | None:
        by = spec.by
        val = spec.value
        relaxed = bool(getattr(spec, "relaxed", False))
        cand: Any = None
        if by == "automation_id" and val:
            handled, ctrl = self._native_find(root, aid=str(val))
            if handled:
                return ctrl
            cand = root.Control(searchDepth=16, AutomationId=str(val))
        elif by == "name_type" and isinstance(val, dict):
            name = str(val.get("name", ""))
            ctype = str(val.get("control_type", ""))
            handled, ctrl = (False, None)
            if name:  # 名字为空时不试原生：只按类型会命中树里第一个同类元素
                handled, ctrl = self._native_find(root, name=name, ctype=ctype)
            # 原生命中直接返回；原生"确认不存在"且非 relaxed 也直接返回——
            # 精确匹配已被 C++ 遍历证伪，Python 全树重扫只是白烧预算。
            # relaxed（编辑距离模糊匹配）原生无法表达，未命中时继续走旧遍历。
            if handled and (ctrl is not None or not relaxed):
                return ctrl
            # uiautomation 以 (control, depth) 位置参数回调 Compare；命名参数必须
            # 写成 keyword-only（*args 吸收 depth），否则 depth 会覆盖闭包变量。
            cand = root.Control(searchDepth=16,
                                Compare=lambda c, *args, _n=name, _t=ctype,
                                _d=2 if relaxed else 0:
                                self._name_type_cmp(c, _n, _t, _d))
        elif by == "structure_path" and val:
            from .pathmatch import match_path, parse_path
            # 末段带名字/类型时先走原生预筛（pathmatch 的 _cmp 同样要求末段
            # 名字精确相等，原生判"不存在"与旧遍历等价；但兄弟序号约束原生
            # 表达不了，带序号的路径保守地仍走旧遍历）。
            try:
                segs = parse_path(str(val))
            except Exception:  # noqa: BLE001
                segs = []
            if segs:
                last_type, last_name, last_idx = segs[-1]
                if last_name and not last_idx:
                    handled, ctrl = self._native_find(root, name=last_name, ctype=last_type)
                    if handled:
                        return ctrl
            return match_path(self, root, str(val), relaxed=relaxed,
                              timeout_secs=timeout_secs)
        elif by == "class_index" and isinstance(val, dict):
            idx = max(1, int(val.get("index", 1) or 1))
            handled, ctrl = self._native_find(root, cls=str(val.get("class_name", "")),
                                              index=idx)
            if handled and (ctrl is not None or idx == 1):
                return ctrl
            cand = root.Control(searchDepth=16, ClassName=str(val.get("class_name", "")),
                                foundIndex=idx)
        else:
            return None
        if cand is None:
            logger.debug("UIA 定位未命中（未构造候选）by=%s value=%r", by, val)
            return None
        try:
            # 位置参数：uiautomation 各版本第二参数名不一致
            # （旧版 sleepIntervalSeconds / 新版 searchIntervalSeconds），按位传参跨版本稳定。
            if cand.Exists(timeout_secs, 0.05):
                return cand
        except Exception as e:  # noqa: BLE001
            logger.debug("UIA 定位异常 by=%s value=%r: %s", by, val, e)
            return None
        logger.debug("UIA 定位未命中（Exists=False）by=%s value=%r root=%r",
                     by, val, getattr(root, "Name", ""))
        return None

    @staticmethod
    def _name_type_cmp(ctrl: Any, name: str, ctype: str, max_edit: int) -> bool:
        try:
            cn = str(ctrl.Name or "")
            ct = _short_type(str(ctrl.ControlTypeName or ""))
        except Exception:  # noqa: BLE001
            return False
        if ctype and ct.lower() != ctype.lower():
            return False
        if cn == name:
            return True
        if max_edit > 0 and name and cn:
            from ...interpreter.textmatch import edit_distance
            try:
                return edit_distance(cn, name) <= max_edit
            except Exception:  # noqa: BLE001
                return False
        return False

    # ── Pattern 操作（回放执行层） ─────────────────────────────────
    def _pattern_call(self, token: int, pat: str, op: str, args: tuple[Any, ...],
                      timeout_ms: int = 1000) -> tuple[bool, Any]:
        def _do() -> Any:
            ctrl = self._get(token)
            if ctrl is None:
                raise LookupError("stale token")
            pid = getattr(auto.PatternId, pat, None)
            if pid is None:
                raise LookupError(f"unknown pattern {pat}")
            p = ctrl.GetPattern(pid)
            if p is None:
                raise LookupError(f"pattern {pat} unsupported")
            return getattr(p, op)(*args)
        ok, res, _err = self._submit(_do, timeout_ms)
        return ok, res

    def read_value(self, element: Any, timeout_ms: int = 500) -> str | None:
        def _do() -> str | None:
            ctrl = self._get(element)
            if ctrl is None:
                raise LookupError("stale token")
            return self._read_value_opt(ctrl)
        ok, res, _ = self._submit(_do, timeout_ms)
        return str(res) if (ok and res is not None) else None

    def is_editable(self, element: Any, timeout_ms: int = 500) -> bool:
        """元素是否可作为文本输入目标。

        不能只看 read_value 是否为 None：`_read_value` 失败时会回退到控件
        Name，窗口 Pane 的 Name 是标题（非空），会被误判成"可写控件"，
        进而走剪贴板通道把焦点聚焦到窗口根（真机坐实）。
        """
        def _do() -> bool:
            ctrl = self._get(element)
            if ctrl is None:
                return False
            try:
                if bool(getattr(ctrl, "IsPassword", False)):
                    return True
            except Exception:  # noqa: BLE001
                pass
            pats = self._patterns_of_ctrl(ctrl)
            return any(p in ("ValuePattern", "TextPattern", "RangeValuePattern") for p in pats)

        ok, res, _ = self._submit(_do, timeout_ms)
        return bool(ok and res)

    def read_length(self, element: Any, timeout_ms: int = 500) -> int | None:
        """密码框等不可读值场景：只读长度（ValuePattern 长度或 Name 长度）。"""
        def _do() -> int:
            ctrl = self._get(element)
            if ctrl is None:
                return -1
            v = self._read_value_lenient_for_length(ctrl)
            return v
        ok, res, _ = self._submit(_do, timeout_ms)
        return int(res) if ok and isinstance(res, int) and res >= 0 else None

    def _read_value_lenient_for_length(self, ctrl: Any) -> int:
        vp = getattr(auto.PatternId, "ValuePattern", None)
        try:
            pat = ctrl.GetPattern(vp) if vp is not None else None
            if pat is not None:
                val = pat.Value
                return len(str(val or ""))
        except Exception:  # noqa: BLE001
            pass
        return 0

    def set_value(self, element: Any, text: str, timeout_ms: int = 1000) -> bool:
        # 密码控件拒绝 SetValue（隐私与正确性：SetValue 会明文过进程）
        def _guard() -> bool:
            ctrl = self._get(element)
            if ctrl is None:
                return False
            try:
                if bool(ctrl.IsPassword):
                    return False
            except Exception:  # noqa: BLE001
                pass
            return True
        ok, allowed, _err = self._submit(_guard, 400)
        if not (ok and allowed):
            return False
        res_ok, _ = self._pattern_call(int(element), "ValuePattern", "SetValue", (str(text),), timeout_ms)
        return res_ok

    def invoke(self, element: Any, timeout_ms: int = 1000) -> bool:
        ok, _ = self._pattern_call(int(element), "InvokePattern", "Invoke", (), timeout_ms)
        return ok

    def toggle(self, element: Any) -> bool:
        ok, _ = self._pattern_call(int(element), "TogglePattern", "Toggle", ())
        return ok

    def select_item(self, element: Any) -> bool:
        ok, _ = self._pattern_call(int(element), "SelectionItemPattern", "Select", ())
        return ok

    def legacy_do_default(self, element: Any) -> bool:
        ok, _ = self._pattern_call(int(element), "LegacyIAccessiblePattern", "DoDefaultAction", ())
        return ok

    def scroll_item_into_view(self, element: Any) -> bool:
        ok, _ = self._pattern_call(int(element), "ScrollItemPattern", "ScrollIntoView", (), 1500)
        return ok

    def focus_element(self, element: Any) -> bool:
        def _do() -> Any:
            ctrl = self._get(element)
            if ctrl is None:
                raise LookupError("stale token")
            return ctrl.SetFocus()
        ok, _, _err = self._submit(_do, 1000)
        return ok

    def patterns_of_element(self, token: Any) -> list[str]:
        def _do() -> list[str]:
            ctrl = self._get(token)
            return self._patterns_of_ctrl(ctrl) if ctrl is not None else []
        ok, res, _ = self._submit(_do, 800)
        return res if ok and isinstance(res, list) else []

    def snapshot_of_token(self, token: Any, timeout_ms: int = 600) -> UiaSnapshot | None:
        def _do() -> UiaSnapshot:
            ctrl = self._get(token)
            if ctrl is None:
                raise LookupError("stale token")
            return self._snapshot(ctrl)
        ok, res, _ = self._submit(_do, timeout_ms)
        return res if ok and isinstance(res, UiaSnapshot) else None

    def shutdown(self) -> None:
        if self._started and self._thread is not None:
            self._q.put(None)
            self._thread.join(timeout=1.5)
            self._thread = None
        self._controls.clear()
