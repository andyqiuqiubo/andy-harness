"""节拍、三级超时、输入设备独占与人工介入检测（§6.4 / §5.6 / 场景 12）。

- StepBudget：单步超时（默认 12s，timing.step_timeout_ms 可覆盖）；
- RunBudget：运行超时（默认 30min，超出 abort）；
- app_alive：SendMessageTimeout WM_NULL 探活（挂死 RP-1006，与超时 RP-3003 区分）；
- HumanTakeover：运行期轻量钩子监听"非注入"输入（move>30px 或任意真实按键 → RP-7002）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from ..errors import RepeatError

STEP_TIMEOUT_MS = 12000
RUN_TIMEOUT_S = 30 * 60
APP_HANG_MS = 5000
SETTLE_DEFAULT_MS = 150
BETWEEN_STEP_MS = 300
MOVE_TAKEOVER_PX = 30


@dataclass
class StepBudget:
    step_timeout_ms: int = STEP_TIMEOUT_MS

    def __post_init__(self) -> None:
        self.deadline = time.perf_counter() + self.step_timeout_ms / 1000.0

    def left_ms(self) -> int:
        return max(0, int((self.deadline - time.perf_counter()) * 1000))

    def check(self, phase: str = "") -> None:
        if time.perf_counter() > self.deadline:
            raise RepeatError("RP-3003", f"步骤超时（{phase or '执行'}，预算 {self.step_timeout_ms}ms）",
                              detail={"phase": phase, "budget_ms": self.step_timeout_ms})

    def clamp_ms(self, want_ms: int) -> int:
        """把某阶段的期望等待压缩到剩余预算内。"""
        return max(120, min(int(want_ms), self.left_ms()))


class RunBudget:
    def __init__(self, timeout_s: int = RUN_TIMEOUT_S) -> None:
        self.deadline = time.perf_counter() + timeout_s

    def check(self) -> None:
        if time.perf_counter() > self.deadline:
            raise RepeatError("RP-3003", "运行超时（整体预算耗尽）")


def _cursor_pos() -> tuple[int, int] | None:
    """当前光标位置（后端进程已声明 PerMonitorV2，返回物理坐标）。"""
    try:
        import ctypes
        import ctypes.wintypes as wt

        pt = wt.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return int(pt.x), int(pt.y)
    except Exception:  # noqa: BLE001
        pass
    return None


class HumanTakeover:
    """运行期人工介入检测（场景 12）。

    实现说明（设计偏差声明）：不另装低级钩子——GetLastInputInfo 对"任何输入"
    （含我们 SendInput 注入的）都刷新。因此判据为：
        系统最后输入时刻 严格晚于 我方最后注入时刻（留 120ms 余量）
    → 期间发生了非我注入的输入（人移动/敲键）。该判据对轻微抖动同样敏感，
    宁停勿撞：挂起可由用户"继续"恢复。
    """

    MARGIN_MS = 120
    # 指针抖动阈值：回放期间光标位移不超过该值不算"人工介入"。
    # GetLastInputInfo 对任何鼠标移动都刷新，用户手搭在鼠标上的轻微抖动
    # 会被判成接管（真机坐实：点完"重试该步"立刻又 RP-7002）。
    JITTER_PX = 6

    def __init__(self, injector: Any) -> None:
        self._inj = injector
        self._last_inject_perf = time.perf_counter()
        self._last_pos: tuple[int, int] | None = None

    def mark_injected(self, pos: tuple[int, int] | None = None) -> None:
        self._last_inject_perf = time.perf_counter()
        if pos is not None:
            self._last_pos = pos
            return
        # 未提供坐标（如 window_focus）时也记下当前光标：后续用它判断抖动，
        # 否则基准仍停在上一次点击位置，正常微动会被误判成大幅移动。
        cur = _cursor_pos()
        if cur is not None:
            self._last_pos = cur

    def detect(self) -> bool:
        try:
            from ..platform.win32.env import idle_seconds
            idle_ms = idle_seconds() * 1000.0
            own_age_ms = (time.perf_counter() - self._last_inject_perf) * 1000.0
            # 系统"距最后输入"比"距我方最后注入"更短 → 我方注入之后又有人输入
            if idle_ms + self.MARGIN_MS >= own_age_ms:
                return False
            # 有输入发生：若光标只是微动（手搭在鼠标上），不算接管
            if self._last_pos is not None:
                cur = _cursor_pos()
                if cur is not None:
                    dx = abs(cur[0] - self._last_pos[0])
                    dy = abs(cur[1] - self._last_pos[1])
                    if dx <= self.JITTER_PX and dy <= self.JITTER_PX:
                        return False
            return True
        except Exception:  # noqa: BLE001
            return False


class NullTakeover:
    """接管检测空实现：不读全局输入态。用于无头测试（假平台件本就不产生
    真实注入，全局 last-input tick 只会被环境噪音推进而误判人工接管）。"""

    def mark_injected(self, pos: tuple[int, int] | None = None) -> None:
        pass

    def detect(self) -> bool:
        return False


async def app_alive_or_raise(windows: Any, hwnd: int, timeout_ms: int = APP_HANG_MS) -> None:
    ok = await asyncio.to_thread(windows.alive, int(hwnd), timeout_ms)
    if not ok:
        raise RepeatError("RP-1006", "目标应用无响应", detail={"hwnd": int(hwnd)})
