"""定位器注册表（§8.2，对齐 input_dispatch 双注册表范式）。

核心执行器只遍历注册表；新增定位器 = 新建文件 + @register_locator + __init__ import。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..models import Hit, LocatorSpec, Miss, WindowRef

logger = logging.getLogger("harness.repeat.locators")

# 干跑单步预算（§6.4）
DRY_BUDGET_MS = 6000


@dataclass(slots=True)
class ExecContext:
    """执行上下文：平台件 + 当前作用域窗口 + 环境级别 + 干跑标志。"""

    inspector: Any = None                 # UiInspector
    grabber: Any = None                   # ScreenGrabber
    windows: Any = None                   # WindowManager
    scope_win: WindowRef | None = None    # 目标窗口（None=桌面作用域）
    env_level: str = "L0"
    dry_run: bool = False
    allow_blind_coord: bool = False
    coord_gate: bool = True
    anchors: dict[str, bytes] = field(default_factory=dict)  # 锚点相对路径 → bytes（run 级缓存）
    bundle_dir: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Locator(Protocol):
    kind: str

    def find(self, ctx: ExecContext, spec: LocatorSpec) -> Hit | Miss: ...

    def describe_failure(self, miss: Miss) -> str: ...


_LOCATORS: dict[str, type[Locator]] = {}
_ORDER: list[str] = []


def register_locator(kind: str, *, after: str | None = None
                     ) -> Callable[[type[Locator]], type[Locator]]:
    def deco(cls: type[Locator]) -> type[Locator]:
        if kind in _LOCATORS:
            raise ValueError(f"locator 重复注册: {kind}")
        cls.kind = kind
        _LOCATORS[kind] = cls
        if after and after in _ORDER:
            _ORDER.insert(_ORDER.index(after) + 1, kind)
        else:
            _ORDER.append(kind)
        return cls
    return deco


def get_locator(kind: str) -> type[Locator] | None:
    return _LOCATORS.get(kind)


def registered_kinds() -> list[str]:
    return list(_ORDER)


def build_chain(specs: list[LocatorSpec]) -> list[Locator]:
    out: list[Locator] = []
    for spec in specs:
        cls = _LOCATORS.get(spec.kind)
        if cls is None:
            logger.warning("未知定位器 %s，已跳过", spec.kind)
            continue
        out.append(cls())
    return out


def resolve_chain(ctx: ExecContext, specs: list[LocatorSpec],
                  *, budget_ms: int | None = None) -> tuple[Hit | None, list[Miss], int]:
    """按链顺序尝试；返回 (首个 Hit 或 None, 尝试记录, 累计耗时ms)。

    预算（§5.3）：链内各定位器 timeout 之和封顶 budget_ms（默认 6s）。
    coord 受环境闸门控制：L2/L3 且未显式放行 → 直接判 RP-2006 并停止降级（不再点击）。
    """
    from ..coords import coord_allowed

    total_budget = budget_ms if budget_ms is not None else DRY_BUDGET_MS
    misses: list[Miss] = []
    elapsed_total = 0
    chain = build_chain(specs)
    for idx, (spec, loc) in enumerate(zip(specs, chain)):
        if spec.kind == "coord":
            if not coord_allowed(ctx.env_level, ctx.coord_gate, ctx.allow_blind_coord):
                miss = Miss(kind="coord", reason="env_gated", elapsed_ms=0,
                            diagnostics={"env_level": ctx.env_level, "index": idx})
                misses.append(miss)
                # 闸门阻断 = 链终止（ADR-05：默认拒绝盲点）
                return None, misses, elapsed_total
        remaining = max(300, total_budget - elapsed_total)
        spec.timeout_ms = min(int(spec.timeout_ms or 2000), remaining)
        try:
            res = loc.find(ctx, spec)
        except Exception as e:  # noqa: BLE001 - 定位器异常视为 miss，不炸整个运行
            res = Miss(kind=spec.kind, reason="error",
                       diagnostics={"index": idx, "error": str(e)})
        elapsed_total += int(getattr(res, "elapsed_ms", 0) or 0)
        res.diagnostics["index"] = idx
        if isinstance(res, Hit):
            res.diagnostics["chain_len"] = len(specs)
            return res, misses, elapsed_total
        misses.append(res)
    return None, misses, elapsed_total
