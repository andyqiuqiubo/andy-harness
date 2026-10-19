"""相对定位器：目标中心相对窗口客户区比例坐标（§6.1 策略 6，§场景 2）。"""

from __future__ import annotations

import time

from ..coords import from_rel
from ..models import Hit, LocatorSpec, Miss
from .registry import ExecContext, register_locator


@register_locator("relative")
class RelativeLocator:
    kind = "relative"

    def find(self, ctx: ExecContext, spec: LocatorSpec) -> Hit | Miss:
        t0 = time.perf_counter()
        win = ctx.scope_win
        if win is None or not spec.ratio:
            return Miss(kind="relative", reason="no_anchor", elapsed_ms=0)
        if win.minimized:
            return Miss(kind="relative", reason="minimized")
        client = win.client
        if client[2] - client[0] <= 0 or client[3] - client[1] <= 0:
            return Miss(kind="relative", reason="zero_client")
        x, y = from_rel(spec.ratio[0], spec.ratio[1], client)
        elapsed = int((time.perf_counter() - t0) * 1000)
        # 客户区尺寸漂移 >50% → 降权标记（执行层可据此决定是否继续）
        return Hit(rect=(x, y, x, y), center=(x, y), element=None, kind="relative",
                   score=spec.confidence, elapsed_ms=elapsed,
                   diagnostics={"client": list(client)})

    def describe_failure(self, miss: Miss) -> str:
        if miss.reason == "no_anchor":
            return "无可用窗口锚点（窗口未找到或比例缺失）"
        if miss.reason == "minimized":
            return "目标窗口已最小化（需先恢复）"
        return f"相对定位失败: {miss.reason}"
