"""实时进展反馈 —— 非阻塞进度事件总线。

设计目标（对应"程序逆向运行"的实时进度需求）：

- 流水线各**阶段**与**步骤**通过 :class:`ProgressReporter` 推送结构化事件；调用方
  （SSE 端点）通过 :class:`ProgressStore` 订阅。推送是 **O(1) 内存操作**，只把事件
  追加进每 run_id 的缓冲并 ``put_nowait`` 给在线的订阅者，**从不 await 订阅者**，
  因此绝不会阻塞或拖慢逆向主流程。
- 事件先落内存环形缓冲（按 run_id 分桶），订阅者连接后**先回放历史、再接收实时增量**，
  天然支持迟到连接与断线重连。
- 事件类型：

  - ``phase_start``   阶段开始：携带阶段名 ``stage``、可读名 ``label``、序号
    ``stage_index``/``stage_total``（满足"开始时显示当前阶段名称标识"）。
  - ``phase_progress`` 步骤进行中：携带 ``done``/``total`` 与派生 ``percent``，
    以及 ``message`` 说明当前在做什么（满足"持续更新进度，已完成/总数或百分比"）。
  - ``phase_done``    阶段结束：携带最终 ``status`` 与 ``summary``（满足"明确标记完成"）。
  - ``run_done``      整条流水线结束：携带最终运行记录 ``record``。
  - ``run_error``     流水线异常（编排器层）：携带 ``error``。

所有 ``emit`` 均为同步、非阻塞；订阅端用 ``async for`` 消费，二者解耦。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from typing import Any

# 每个 run_id 最多保留的事件数（环形上限，防止长任务/大量重连拖垮内存）。
_EVENT_KEEP = 600

# 最多保留多少个 run_id 的缓冲（超出时按插入顺序淘汰**已无订阅者**的最旧 run）。
# 生产路径从不调用 drop()，若无此上限，_buffers 会随运行次数无限增长——每个 run
# 最多滞留 600 条事件，且 run_done 事件还携带完整 record，长驻进程内存只增不减。
# 被淘汰的已终结 run 不会导致前端卡死：SSE 端点在「已终结且无内存事件」时会
# 合成终态事件（见 api/rest/reverse_engineering.py 的 has_events 分支）。
_RUN_KEEP = 50


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class ProgressStore:
    """按 run_id 分桶的内存进度事件总线（单进程内有效）。"""

    def __init__(self) -> None:
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._subs: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}

    def emit(self, run_id: str, event: dict[str, Any]) -> None:
        """O(1) 写入：追加到缓冲并即时投递给在线的订阅者；绝不等待订阅者。"""
        buf = self._buffers.setdefault(run_id, [])
        buf.append(event)
        if len(buf) > _EVENT_KEEP:
            del buf[: len(buf) - _EVENT_KEEP]
        self._evict_runs()
        # 拷贝订阅者列表，避免迭代期间被 finally 改动导致漏/重投
        for q in list(self._subs.get(run_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 订阅者消费过慢：丢弃单条增量即可，主流程不受影响
                pass

    def _evict_runs(self) -> None:
        """按插入顺序淘汰最旧的 run 缓冲，把 run_id 数量限制在 ``_RUN_KEEP`` 以内。

        只淘汰**当前没有订阅者**的 run：仍有人订阅的 run 必须保留其 ``_subs`` 项，
        否则 ``emit`` 取不到队列，实时事件会静默丢失、前端流再无增量。
        """
        if len(self._buffers) <= _RUN_KEEP:
            return
        for rid in list(self._buffers):
            if len(self._buffers) <= _RUN_KEEP:
                break
            if self._subs.get(rid):
                continue
            self._buffers.pop(rid, None)

    async def subscribe(self, run_id: str) -> AsyncGenerator[dict[str, Any], None]:
        """异步生成器：先回放历史事件，再增量推送；遇到 run_done/run_error 后结束。

        回放使用缓冲的**快照**：生成器在 ``yield`` 处暂停期间主流程仍会继续发射，
        若直接迭代"活"列表，这段新事件会被回放二次投递（后经队列再投一次）造成
        重复，且环形裁剪会引起索引跳跃丢事件。快照保证"回放 = 订阅那一刻的历史"，
        之后的事件一律走队列，恰好投递一次。
        """
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        self._subs.setdefault(run_id, []).append(q)
        try:
            for ev in list(self._buffers.get(run_id, ())):
                yield ev
                # 历史回放中若已含终止事件（订阅发生在运行结束之后——常见于迟到
                # 连接/断线重连），立即收束；否则会永远挂在队列上、流不关闭。
                if ev.get("type") in ("run_done", "run_error"):
                    return
            while True:
                ev = await q.get()
                yield ev
                if ev.get("type") in ("run_done", "run_error"):
                    break
        finally:
            subs = self._subs.get(run_id)
            if subs and q in subs:
                subs.remove(q)
            # 该 run 的订阅者全部离开后移除空列表，避免 _subs 随 run_id 数量无限增长
            if subs is not None and not subs:
                self._subs.pop(run_id, None)

    def has_terminal(self, run_id: str) -> bool:
        """该 run 是否已产生终止事件（run_done / run_error），用于避免重复广播收口。"""
        return any(ev.get("type") in ("run_done", "run_error") for ev in self._buffers.get(run_id, ()))

    def has_events(self, run_id: str) -> bool:
        """该 run 是否在内存中还有任何事件（区分"同进程已完成可回放"与"重启后总线已空"）。"""
        return bool(self._buffers.get(run_id))

    def drop(self, run_id: str) -> None:
        """显式释放某 run_id 的缓冲（如确认不再需要回放时调用）。"""
        self._buffers.pop(run_id, None)
        # 仍在线订阅者由各自 finally 在断开时自行清理，这里不动 _subs


# 模块级单例：REST 层与后台任务共享同一个总线。
progress_store = ProgressStore()


class ProgressReporter:
    """阶段/步骤进度发射器。``store=None`` 时为静默 no-op（便于离线/单测）。"""

    def __init__(self, run_id: str = "", store: ProgressStore | None = None) -> None:
        self.run_id = run_id
        self.store = store

    # ── 阶段级 ──────────────────────────────────────────────────────────
    def phase_start(self, stage: str, label: str, index: int, total: int) -> None:
        if self.store is None:
            return
        self.store.emit(
            self.run_id,
            {
                "type": "phase_start",
                "run_id": self.run_id,
                "stage": stage,
                "label": label,
                "stage_index": index,
                "stage_total": total,
                "timestamp": _now(),
            },
        )

    def phase_done(self, stage: str, status: str, summary: str = "") -> None:
        if self.store is None:
            return
        self.store.emit(
            self.run_id,
            {
                "type": "phase_done",
                "run_id": self.run_id,
                "stage": stage,
                "status": status,
                "summary": summary,
                "timestamp": _now(),
            },
        )

    # ── 步骤级 ──────────────────────────────────────────────────────────
    def phase_progress(self, done: int, total: int, message: str = "", stage: str | None = None) -> None:
        if self.store is None:
            return
        pct = int(round(done / total * 100)) if total else 0
        self.store.emit(
            self.run_id,
            {
                "type": "phase_progress",
                "run_id": self.run_id,
                "stage": stage,
                "done": done,
                "total": total,
                "percent": pct,
                "message": message,
                "timestamp": _now(),
            },
        )

    # ── 运行级 ──────────────────────────────────────────────────────────
    def run_done(self, record: dict[str, Any]) -> None:
        if self.store is None:
            return
        self.store.emit(
            self.run_id,
            {
                "type": "run_done",
                "run_id": self.run_id,
                "record": record,
                "timestamp": _now(),
            },
        )

    def run_error(self, error: str) -> None:
        if self.store is None:
            return
        self.store.emit(
            self.run_id,
            {
                "type": "run_error",
                "run_id": self.run_id,
                "error": error,
                "timestamp": _now(),
            },
        )


# 静默 no-op 实例：RunContext 的默认 reporter，保证任意阶段调用 phase_progress
# 都不必判空（离线/未订阅场景下直接无操作）。
NULL_REPORTER = ProgressReporter()
