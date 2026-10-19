"""进度事件总线（自包含，复用 reverse_engineering/progress.py 的已验证范式）。

两个易踩坑（来自项目 MEMORY，务必保留）：
1. 历史回放必须用 list(...) **快照**——活列表迭代 + 暂停在 yield 会把并发新事件重复投递；
2. 回放循环内遇终止事件（run_done/run_error）必须立即 return——否则"运行结束后才
   订阅"会永远挂在队列上、SSE 流不关闭。

desktop_repeat 包要求可整体抽包（§2.3 依赖禁令），故在此自带一份，而非 import
reverse_engineering 模块。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from typing import Any

_EVENT_KEEP = 600
_RUN_KEEP = 50
_TERMINAL = ("run_done", "run_error")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class ProgressStore:
    def __init__(self) -> None:
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._subs: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}

    def emit(self, run_id: str, event: dict[str, Any]) -> None:
        buf = self._buffers.setdefault(run_id, [])
        buf.append(event)
        if len(buf) > _EVENT_KEEP:
            del buf[: len(buf) - _EVENT_KEEP]
        self._evict_runs()
        for q in list(self._subs.get(run_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _evict_runs(self) -> None:
        if len(self._buffers) <= _RUN_KEEP:
            return
        for rid in list(self._buffers):
            if len(self._buffers) <= _RUN_KEEP:
                break
            if self._subs.get(rid):
                continue
            self._buffers.pop(rid, None)

    async def subscribe(self, run_id: str) -> AsyncGenerator[dict[str, Any], None]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        self._subs.setdefault(run_id, []).append(q)
        try:
            # 快照回放（坑 1）；回放中即含终止事件则立即收束（坑 2）
            for ev in list(self._buffers.get(run_id, ())):
                yield ev
                if ev.get("type") in _TERMINAL:
                    return
            while True:
                ev = await q.get()
                yield ev
                if ev.get("type") in _TERMINAL:
                    break
        finally:
            subs = self._subs.get(run_id)
            if subs and q in subs:
                subs.remove(q)
            if subs is not None and not subs:
                self._subs.pop(run_id, None)

    def has_terminal(self, run_id: str) -> bool:
        return any(ev.get("type") in _TERMINAL for ev in self._buffers.get(run_id, ()))

    def has_events(self, run_id: str) -> bool:
        return bool(self._buffers.get(run_id))

    def drop(self, run_id: str) -> None:
        self._buffers.pop(run_id, None)


# 模块级单例：REST/SSE 与后台任务共享
repeat_progress_store = ProgressStore()


class Reporter:
    """repeat 事件发射器。store=None 时为 no-op（离线/单测）。"""

    def __init__(self, run_id: str = "", store: ProgressStore | None = None) -> None:
        self.run_id = run_id
        self.store = store

    def _emit(self, etype: str, **fields: Any) -> None:
        if self.store is None:
            return
        ev: dict[str, Any] = {"type": etype, "run_id": self.run_id, "timestamp": _now()}
        ev.update(fields)
        self.store.emit(self.run_id, ev)

    def run_started(self, workflow_id: str, total_steps: int) -> None:
        self._emit("run_started", workflow_id=workflow_id, total_steps=total_steps)

    def precheck_done(self, env_level: str, drift: dict[str, Any]) -> None:
        self._emit("precheck_done", env_level=env_level, drift=drift)

    def notice(self, level: str, title: str, body: str = "", actions: list[str] | None = None) -> None:
        self._emit("notice", level=level, title=title, body=body, actions=actions or [])

    def step_started(self, sid: str, index: int, action: str, label: str) -> None:
        self._emit("step_started", sid=sid, index=index, action=action, label=label)

    def step_retry(self, sid: str, attempt: int, reason: str) -> None:
        self._emit("step_retry", sid=sid, attempt=attempt, reason=reason)

    def step_done(self, sid: str, status: str, hit_index: int, kind: str, elapsed_ms: int) -> None:
        self._emit("step_done", sid=sid, status=status, hit_index=hit_index,
                   kind=kind, elapsed_ms=elapsed_ms)

    def step_failed(self, sid: str, code: str, message: str) -> None:
        self._emit("step_failed", sid=sid, code=code, message=message)

    def run_suspended(self, sid: str, code: str, diagnostics_path: str) -> None:
        self._emit("run_suspended", sid=sid, code=code, diagnostics_path=diagnostics_path)

    def run_resumed(self, decision: str) -> None:
        self._emit("run_resumed", decision=decision)

    def run_done(self, record: dict[str, Any]) -> None:
        self._emit("run_done", record=record)

    def run_error(self, code: str, message: str) -> None:
        self._emit("run_error", code=code, message=message)
