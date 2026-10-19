"""断言注册表与实现（§6.5 / E4）。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..models import Hit

AssertFn = Callable[[Any, dict[str, Any], Hit | None, Any], "AssertResult"]
_ASSERTS: dict[str, AssertFn] = {}


def register_assert(kind: str) -> Callable[[AssertFn], AssertFn]:
    def deco(fn: AssertFn) -> AssertFn:
        _ASSERTS[kind] = fn
        return fn
    return deco


@dataclass(slots=True)
class AssertResult:
    ok: bool
    message: str = ""


@dataclass(slots=True)
class AssertCtx:
    """断言执行上下文（只读 inspector/windows，避免拉入完整 runner）。"""

    inspector: Any
    windows: Any


def check_assert(kind: str, ctx: Any, spec: dict[str, Any], hit: Hit | None,
                 step: Any) -> AssertResult:
    fn = _ASSERTS.get(kind)
    if fn is None:
        return AssertResult(True, f"未知断言类型 {kind}（跳过）")
    res = fn(ctx, spec, hit, step)
    return res if isinstance(res, AssertResult) else AssertResult(bool(res), str(res))


@register_assert("element_exists")
def _a_exists(ctx: Any, spec: dict[str, Any], hit: Hit | None, step: Any) -> AssertResult:
    return AssertResult(hit is not None, "元素存在" if hit else "元素不存在")


@register_assert("element_absent")
def _a_absent(ctx: Any, spec: dict[str, Any], hit: Hit | None, step: Any) -> AssertResult:
    return AssertResult(hit is None, "元素应不存在")


@register_assert("value_equals")
def _a_value(ctx: Any, spec: dict[str, Any], hit: Hit | None, step: Any) -> AssertResult:
    want = str(spec.get("value", "") or "")
    if hit is None or hit.element is None:
        return AssertResult(False, "无目标元素可读")
    got = ctx.inspector.read_value(hit.element)
    return AssertResult((got or "") == want, f"期望 {want!r} 实际 {got!r}")


@register_assert("value_regex")
def _a_regex(ctx: Any, spec: dict[str, Any], hit: Hit | None, step: Any) -> AssertResult:
    pat = str(spec.get("value", "") or "")
    if hit is None or hit.element is None:
        return AssertResult(False, "无目标元素可读")
    got = ctx.inspector.read_value(hit.element) or ""
    try:
        return AssertResult(bool(re.search(pat, got)), f"值 {got!r} 对 /{pat}/")
    except re.error:
        return AssertResult(False, f"非法正则 {pat}")


@register_assert("text_contains")
def _a_text(ctx: Any, spec: dict[str, Any], hit: Hit | None, step: Any) -> AssertResult:
    want = str(spec.get("value", "") or "")
    if hit is None:
        return AssertResult(False, "无目标")
    hay = ""
    if hit.element is not None:
        hay = ctx.inspector.read_value(hit.element) or ""
    return AssertResult(want in (hay or ""), f"文本包含检查：{want!r}")


@register_assert("window_title_matches")
def _a_title(ctx: Any, spec: dict[str, Any], hit: Hit | None, step: Any) -> AssertResult:
    pat = str(spec.get("value", "") or "")
    fg = ctx.windows.foreground()
    title = fg.title if fg else ""
    try:
        return AssertResult(bool(re.search(pat, title or "")), f"标题 {title!r} 对 /{pat}/")
    except re.error:
        return AssertResult(False, f"非法正则 {pat}")
