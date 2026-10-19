"""环境校验与漂移分级（§6.2 / §7.3 场景 10/11）。

- classify：录制基线 Env vs 当前 Env → L0–L3；
- graphics_session：无输入桌面/锁屏 → RP-1001；
- elevation：目标窗口提权且自身较低 → RP-4001（不绕过）；
- app_ready：exe/窗口四级放宽匹配（场景 1），供 wait_window 复用。
"""

from __future__ import annotations

import time
from typing import Any

from ..coords import classify_env
from ..errors import RepeatError
from ..models import Env, WindowRef


def classify(base: Env, cur: Env) -> str:
    return classify_env(base, cur)


def graphics_session(env_provider: Any) -> None:
    """锁屏/远程断开/UAC 安全桌面（场景 11）→ RP-1001。"""
    try:
        if not env_provider.graphics_available():
            raise RepeatError("RP-1001", "当前无图形会话（锁屏/远程断开），请解锁后重试")
        desk = env_provider.desktop_name()
        if desk and desk.lower() not in ("default", ""):
            raise RepeatError("RP-1001", f"当前处于非交互桌面（{desk}），无法安全回放")
    except RepeatError:
        raise
    except Exception as e:  # noqa: BLE001
        raise RepeatError("RP-1001", f"图形会话检测失败: {e}") from e


def check_elevation(env_provider: Any, windows: Any, win: WindowRef) -> None:
    """自身完整性低于提权目标 → RP-4001 中止（场景 10）。"""
    if win.elevated:
        try:
            self_elev = env_provider.is_elevated()
        except Exception:  # noqa: BLE001
            self_elev = False
        if not self_elev:
            raise RepeatError("RP-4001",
                              "目标窗口以管理员权限运行，当前进程权限不足（UIPI）",
                              detail={"title": win.title, "exe": win.exe})


def app_ready(windows: Any, hints: dict[str, Any], *,
              timeout_ms: int = 1500, restore_minimized: bool = True) -> WindowRef | None:
    """目标应用就绪（四级放宽 + 最小化恢复）。找不到返回 None（由调用方决定挂起）。"""
    deadline = time.perf_counter() + timeout_ms / 1000.0
    last: WindowRef | None = None
    while True:
        found = windows.find_window(hints)
        ref: WindowRef | None = found if isinstance(found, WindowRef) else None
        if ref is not None:
            if ref.minimized and restore_minimized:
                try:
                    windows.restore(ref)
                except Exception:  # noqa: BLE001
                    pass
            return ref
        last = ref
        if time.perf_counter() >= deadline:
            return last
        time.sleep(0.1)


def env_snapshot(env_provider: Any) -> dict[str, Any]:
    try:
        d = env_provider.snapshot().to_dict()
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def drift_detail(base: Env, cur: Env, level: str) -> dict[str, Any]:
    from ..coords import ENV_LEVEL_LABELS
    return {"level": level, "label": ENV_LEVEL_LABELS.get(level, level),
            "base_monitors": len(base.monitors), "cur_monitors": len(cur.monitors),
            "scale_base": [m.scale for m in base.monitors][:4],
            "scale_cur": [m.scale for m in cur.monitors][:4]}
