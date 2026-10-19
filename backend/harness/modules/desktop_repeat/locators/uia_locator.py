"""UIA 语义定位器（链首主力，ADR-08）。"""

from __future__ import annotations

from ..models import Hit, LocatorSpec, Miss
from .registry import ExecContext, register_locator


@register_locator("uia")
class UiaLocator:
    kind = "uia"

    def find(self, ctx: ExecContext, spec: LocatorSpec) -> Hit | Miss:
        if ctx.inspector is None or not getattr(ctx.inspector, "available", lambda: False)():
            return Miss(kind="uia", reason="not_available",
                        diagnostics={"by": spec.by})
        scope = {"hwnd": ctx.scope_win.hwnd} if ctx.scope_win else None
        timeout = spec.timeout_ms or 2000
        if ctx.dry_run:
            timeout = min(timeout, 1200)
        res = ctx.inspector.element_by_spec(spec, scope, timeout)
        # inspector 为 Protocol/Any 注入件，显式归一到 Hit | Miss
        if isinstance(res, Hit):
            return res
        if isinstance(res, Miss):
            return res
        return Miss(kind="uia", reason="bad_response", diagnostics={"by": spec.by})

    def describe_failure(self, miss: Miss) -> str:
        r = miss.reason
        if r == "not_found":
            return f"UIA 未找到目标（{miss.diagnostics.get('by', '')}）"
        if r == "timeout":
            return "UIA 查询超时（应用可能未启用无障碍或过慢）"
        if r == "disabled":
            return "目标控件当前不可用（disabled）"
        if r == "zero_rect":
            return "目标控件无可见区域"
        return f"UIA 定位失败: {r}"
