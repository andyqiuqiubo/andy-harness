"""动作动词实现矩阵（§6.3 / E2 注册表）。

主实现 UIA Pattern（不抢前台，ADR-08），二级/末级回退 SendInput（坐标/VK/Unicode）。
文本三通道（ADR-09）：ValuePattern.SetValue → 剪贴板粘贴（恢复原值）→ KEYEVENTF_UNICODE。

不同步于 UI：每个动作是短调用，由 runner 放进线程池（asyncio.to_thread）执行，
避免阻塞 backend 事件循环。返回 ActionResult（ok + 诊断 + 可选读回值）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..errors import RepeatError
from ..models import Hit, StepDef

# 坐标兜底（无元素引用）时的点击偏移
ClickLike = ("click", "double_click", "right_click")


@dataclass(slots=True)
class ActionResult:
    ok: bool
    message: str = ""
    readback: str | None = None      # type 动作读回值（校验用）
    readback_mismatch: bool = False  # 读回与预期不一致（不视为失败，仅标记复核）
    channel: str = ""                # 实际生效通道（pattern/value/clipboard/unicode/coord）
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ActionContext:
    inspector: Any
    injector: Any
    windows: Any
    grabber: Any
    budget: Any = None               # StepBudget（超时检查）
    dry_run: bool = False
    mark_injected: Callable[[tuple[int, int] | None], None] = lambda _p: None


def _element(hit: Hit) -> Any:
    return hit.element  # UIA token(int) 或 None


ActionFn = Callable[[ActionContext, StepDef, Hit], ActionResult]
_ACTIONS: dict[str, ActionFn] = {}


def register_action(name: str) -> Callable[[ActionFn], ActionFn]:
    def deco(fn: ActionFn) -> ActionFn:
        if name in _ACTIONS:
            raise ValueError(f"action 重复注册: {name}")
        _ACTIONS[name] = fn
        return fn
    return deco


def run_action(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    fn = _ACTIONS.get(step.action)
    if fn is None:
        return ActionResult(False, f"未注册动作: {step.action}")
    return fn(ctx, step, hit)


# ── 点击类 ───────────────────────────────────────────────────────
@register_action("click")
def act_click(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    if ctx.dry_run:
        return ActionResult(True, "dry", channel="dry")
    el = _element(hit)
    # 坐标校正说明 UIA 命中的只是容器（如工具栏），用户点的是容器内的具体位置；
    # 此时必须走坐标点击，invoke 容器不会触发内部按钮（真机坐实）。
    if el is not None and not hit.diagnostics.get("coord_corrected"):
        # Pattern 优先（不抢前台，§场景 6/ADR-08）：Invoke→Toggle→SelectItem→Legacy
        for method in ("invoke", "toggle", "select_item", "legacy_do_default"):
            fn = getattr(ctx.inspector, method, None)
            if fn is None:
                continue
            try:
                if fn(el):
                    ctx.mark_injected(hit.center)
                    return ActionResult(True, channel="pattern")
            except Exception:  # noqa: BLE001
                continue
    x, y = hit.center
    n = ctx.injector.click(x, y, "left", 1)
    ctx.mark_injected((x, y))
    return ActionResult(n > 0, channel="coord" if el is None else "pattern_fallback")


@register_action("double_click")
def act_double(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    if ctx.dry_run:
        return ActionResult(True, "dry", channel="dry")
    x, y = hit.center
    n = ctx.injector.click(x, y, "left", 2)  # Pattern 无双击语义
    ctx.mark_injected((x, y))
    return ActionResult(n > 0, channel="coord")


@register_action("right_click")
def act_right(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    if ctx.dry_run:
        return ActionResult(True, "dry", channel="dry")
    x, y = hit.center
    n = ctx.injector.click(x, y, "right", 1)
    ctx.mark_injected((x, y))
    return ActionResult(n > 0, channel="coord")


@register_action("drag")
def act_drag(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    if ctx.dry_run:
        return ActionResult(True, "dry", channel="dry")
    path = (step.params or {}).get("path") or hit.diagnostics.get("path") or []
    if len(path) < 2:
        return ActionResult(False, "drag 缺少路径点")
    x0, y0 = int(path[0][0]), int(path[0][1])
    x1, y1 = int(path[-1][0]), int(path[-1][1])
    if hit.kind in ("uia", "image") and hit.center:
        x1, y1 = hit.center  # 命中元素时以中心为终点更稳
    inj = ctx.injector
    # SendInput down → 插值 move → up（§6.3）
    if hasattr(inj, "press"):
        inj.press("left", True, x0, y0)
        for seg in _interpolate((x0, y0), (x1, y1), steps=12):
            inj.move(*seg)
        inj.press("left", False, x1, y1)
    else:
        # 兜底：无 press 通道的注入器，退化为两段点击（不推荐）
        inj.click(x0, y0, "left", 1)
        inj.click(x1, y1, "left", 1)
    ctx.mark_injected((x1, y1))
    return ActionResult(True, channel="coord")


def _interpolate(a: tuple[int, int], b: tuple[int, int], steps: int) -> list[tuple[int, int]]:
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        out.append((int(a[0] + (b[0] - a[0]) * t), int(a[1] + (b[1] - a[1]) * t)))
    return out


@register_action("scroll")
def act_scroll(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    if ctx.dry_run:
        return ActionResult(True, "dry", channel="dry")
    dx = int(step.params.get("dx", 0) or 0)
    dy = int(step.params.get("dy", 0) or 0)
    x, y = hit.center
    n = ctx.injector.scroll(x, y, dx, dy)
    ctx.mark_injected((x, y))
    return ActionResult(n > 0, channel="coord")


# ── 文本（三通道） ───────────────────────────────────────────────
@register_action("type")
def act_type(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    if ctx.dry_run:
        return ActionResult(True, "dry", channel="dry")
    value = str(step.params.get("value", "") or "")
    if value == "":
        return ActionResult(True, "空文本", channel="noop")
    el = _element(hit)
    mode = str(step.params.get("mode", "auto") or "auto")
    # 锚点是否"可写"：Window/Pane 这类容器没有 ValuePattern。此时**绝不能**
    # focus_element 或点击容器中心——那会把焦点从真正的输入框挪走（Chrome 实录：
    # type 锚到窗口 Pane，回放点页面中心后文字丢失）。注意不能用 read_value 判断：
    # 它读不到值时会回退成控件 Name（窗口标题非空），会把 Pane 误判成可写。
    # 这种情况下直接对**当前焦点**注入，由前置 window_focus/用户点击负责聚焦。
    try:
        writable = el is not None and bool(ctx.inspector.is_editable(el))
    except Exception:  # noqa: BLE001
        writable = False
    # 通道 1：ValuePattern.SetValue
    if writable and mode in ("auto", "uia_set_value"):
        if ctx.inspector.set_value(el, value):
            rb = ctx.inspector.read_value(el)
            return ActionResult(True, readback=rb, channel="value")
    # 通道 2：剪贴板粘贴（只在确认真实可写控件时用；容器上粘贴会把内容送到别处）
    if writable and mode in ("auto", "clipboard_paste"):
        if _paste(ctx, value, el):
            rb = ctx.inspector.read_value(el) if writable else None
            return ActionResult(True, readback=rb, channel="clipboard")
    # 通道 3：Unicode 注入（当前焦点）
    n = ctx.injector.type_unicode(value)
    ctx.mark_injected(None)
    rb = ctx.inspector.read_value(el) if writable else None
    return ActionResult(n > 0, "unicode 注入", readback=rb, channel="unicode")


def _paste(ctx: ActionContext, value: str, el: Any) -> bool:
    try:
        from ..platform.win32.clipboard import ClipboardGuard, write_text
        guard = ClipboardGuard()
        guard.snapshot()
        if not write_text(value):
            guard.restore()
            return False
        # 需要目标控件聚焦后 Ctrl+V
        if el is not None and hasattr(ctx.inspector, "focus_element"):
            ctx.inspector.focus_element(el)
        ctx.injector.hotkey(["ctrl", "v"])
        # 粘贴是异步的：目标应用从剪贴板取数据可能晚于我们恢复原值（体检
        # P2-4）。旧实现发完 Ctrl+V 立即 restore，大文本会被截和。给一个短
        # 等待（本函数经 asyncio.to_thread 跑在线程池，sleep 不阻塞事件循环）。
        time.sleep(0.2)
        guard.restore()
        return True
    except Exception:  # noqa: BLE001
        return False


@register_action("hotkey")
def act_hotkey(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    if ctx.dry_run:
        return ActionResult(True, "dry", channel="dry")
    combo = str(step.params.get("combo", "") or step.params.get("value", "") or "")
    keys = [k.strip().lower() for k in combo.split("+") if k.strip()]
    if not keys:
        return ActionResult(False, "hotkey 缺少按键")
    n = ctx.injector.hotkey(keys)
    # 注入自身要在接管基线上留痕（体检 P2-3）：否则启用 human_takeover_detect
    # 后，热键注入刷新了系统 last-input 却未刷新我方 mark 时刻，下一步 detect
    # 会把"自己的注入"判成人工介入（与 window_focus 的 ALT 技巧同教训）。
    ctx.mark_injected(None)
    return ActionResult(n > 0, channel="sendinput")


# ── 窗口 ─────────────────────────────────────────────────────────
@register_action("window_focus")
def act_focus(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    if ctx.dry_run:
        return ActionResult(True, "dry", channel="dry")
    win = step.evidence.get("_resolved_win")
    if win is None:
        # 目标窗口当前不存在（录制时开着、回放时已关闭）：跳过这一步，
        # 而不是让整轮回放失败——真机坐实：录制起点在桌面/资源管理器，
        # 回放时它已关闭，旧实现直接 RP-5001 停在整个流程之前。
        return ActionResult(True, "目标窗口未找到，跳过聚焦", channel="skip")
    # 目标窗口正被模态对话框禁用（EditPlus 点保存 → 主窗口被 Disable）：
    # SetForegroundWindow 必然失败，旧实现抛 RP-5001 把整轮回放钉死在这一步。
    # 此时模态面本就在前台（或即将出现），"切不过去"不是致命错误——跳过即可，
    # 让后续步骤在模态面里继续（与"目标窗口已关闭 → skip"同一策略）。
    try:
        if win is not None and hasattr(ctx.windows, "is_enabled") \
                and not ctx.windows.is_enabled(getattr(win, "hwnd", 0)):
            return ActionResult(True, "目标窗口被模态框禁用，跳过聚焦", channel="skip")
    except Exception:  # noqa: BLE001
        pass
    ok = ctx.windows.focus(win)
    if not ok:
        raise RepeatError("RP-5001", "无法切换到目标窗口",
                          detail={"title": getattr(win, "title", "")})
    return ActionResult(ok, channel="focus")


@register_action("wait_window")
def act_wait_window(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    # 由 runner 直接处理（需轮询），此处仅占位
    return ActionResult(True, "由 runner 等待", channel="wait")


@register_action("wait_element")
def act_wait_element(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    return ActionResult(True, "由 runner 定位", channel="wait")


@register_action("assert_element")
def act_assert_element(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    return ActionResult(hit is not None, "元素存在性由定位阶段判定", channel="assert")


@register_action("assert_text")
def act_assert_text(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    el = _element(hit)
    want = str(step.assertion.get("value", "") if step.assertion else "")
    if el is None:
        return ActionResult(False, "断言无目标元素", channel="assert")
    got = ctx.inspector.read_value(el) or ""
    return ActionResult(got == want, f"期望 {want!r} 实际 {got!r}", readback=got, channel="assert")


@register_action("manual_confirm")
def act_manual(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    # 挂起由 runner 转 suspended；dry_run 直接通过
    if ctx.dry_run:
        return ActionResult(True, "dry", channel="manual")
    raise RepeatError("RP-4004", "该步骤需人工操作", step_sid=step.sid,
                      )


@register_action("marker")
def act_marker(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    return ActionResult(True, "分段标记", channel="marker")


@register_action("launch_app")
def act_launch(ctx: ActionContext, step: StepDef, hit: Hit) -> ActionResult:
    if ctx.dry_run:
        return ActionResult(True, "dry", channel="launch")
    exe = str(step.app_hint.get("exe", "") or step.params.get("exe", ""))
    if not exe:
        raise RepeatError("RP-1002", "launch_app 缺少可执行文件名", step_sid=step.sid)
    try:
        import subprocess
        subprocess.Popen([exe], close_fds=True)
        return ActionResult(True, f"已启动 {exe}", channel="launch")
    except Exception as e:  # noqa: BLE001
        raise RepeatError("RP-1002", f"无法启动 {exe}: {e}", step_sid=step.sid) from e
