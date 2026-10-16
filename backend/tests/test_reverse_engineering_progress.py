"""进度事件总线（非阻塞实时进展反馈）的最小验证。

不依赖 REA/Ghidra/TCC（离线即可跑），覆盖：

- :class:`ProgressStore` 的回放 / 增量订阅 / 缓冲上限 / 显式释放；
- :class:`ProgressReporter` 在 ``store=None`` 时为静默 no-op（不抛错、不落地）；
- 阶段 / 步骤 / 运行级事件的字段结构符合前端约定。
"""

from __future__ import annotations

import asyncio

import pytest

from harness.modules.reverse_engineering.progress import (
    NULL_REPORTER,
    ProgressReporter,
    ProgressStore,
)


def test_null_reporter_is_noop() -> None:
    # 未注入 store：所有方法应为 no-op，绝不抛错或落地
    rp = NULL_REPORTER
    rp.phase_start("rea_static", "REA", 0, 5)
    rp.phase_progress(1, 3, "doing")
    rp.phase_done("rea_static", "ok", "done")
    rp.run_done({"run_id": "x"})
    rp.run_error("boom")
    assert True  # 到达此处即说明 no-op 安全


def test_reporter_emits_typed_events() -> None:
    store = ProgressStore()
    rp = ProgressReporter("r1", store)
    rp.phase_start("rea_static", "REA", 0, 5)
    rp.phase_progress(2, 4, "反编译中")
    rp.phase_done("rea_static", "ok", "完成")

    evs = store._buffers["r1"]
    assert len(evs) == 3
    assert evs[0]["type"] == "phase_start"
    assert evs[0]["stage"] == "rea_static"
    assert evs[0]["stage_index"] == 0 and evs[0]["stage_total"] == 5
    assert evs[1]["type"] == "phase_progress"
    assert evs[1]["done"] == 2 and evs[1]["total"] == 4
    assert evs[1]["percent"] == 50
    assert evs[1]["message"] == "反编译中"
    assert evs[2]["type"] == "phase_done"
    assert evs[2]["status"] == "ok"


async def test_subscribe_replays_then_streams_and_closes() -> None:
    store = ProgressStore()
    rp = ProgressReporter("r2", store)
    # 先产生历史事件（模拟迟到连接）
    rp.phase_start("frida", "Frida", 1, 5)
    rp.phase_done("frida", "ok", "ok")

    collected: list[dict] = []

    async def consume() -> None:
        async for ev in store.subscribe("r2"):
            collected.append(ev)
            if ev.get("type") in ("run_done", "run_error"):
                break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # 让订阅者先回放历史
    rp.run_done({"run_id": "r2", "status": "ok"})
    await task

    types = [e["type"] for e in collected]
    assert types == ["phase_start", "phase_done", "run_done"]
    assert collected[-1]["record"]["run_id"] == "r2"


async def test_no_duplicate_or_reorder_when_emitting_during_replay() -> None:
    """回放期间并发发射：事件不得重复、不得乱序。

    订阅者先回放历史事件；回放过程中（生成器暂停在 yield 处）主流程继续发射新事件，
    新事件经队列增量投递。若回放直接迭代"活"缓冲列表，新事件会被回放二次投递。
    """
    store = ProgressStore()
    rp = ProgressReporter("rD", store)
    rp.phase_progress(0, 10, "h0")
    rp.phase_progress(1, 10, "h1")

    gen = store.subscribe("rD")
    first = await gen.__anext__()  # 回放第 1 条
    assert first["message"] == "h0"
    # 此刻生成器暂停在 yield 内 —— 并发发射新事件（进缓冲 + 进队列）
    rp.phase_progress(2, 10, "l0")
    rp.run_done({"run_id": "rD"})

    rest = [ev async for ev in gen]
    seq = [ev.get("message") or ev["type"] for ev in rest]
    # 期望：回放剩余 h1 → 增量 l0（恰好一次）→ run_done
    assert seq == ["h1", "l0", "run_done"], f"事件流出现重复/乱序：{seq}"


async def test_subscribe_after_completion_ends_immediately() -> None:
    """运行结束后才订阅（迟到连接/断线重连）：回放完终止事件须立即结束流。

    回归点：回放循环若不在终止事件处收束，生成器会永远等待增量队列 →
    SSE 响应永不关闭、客户端挂起。
    """
    store = ProgressStore()
    rp = ProgressReporter("rF", store)
    rp.phase_start("x", "X", 0, 1)
    rp.phase_done("x", "ok", "done")
    rp.run_done({"run_id": "rF"})

    async def drain() -> list[dict]:
        return [ev async for ev in store.subscribe("rF")]

    got = await asyncio.wait_for(drain(), timeout=2)
    assert [e["type"] for e in got] == ["phase_start", "phase_done", "run_done"]


def test_has_terminal_flag() -> None:
    store = ProgressStore()
    rp = ProgressReporter("rT", store)
    assert store.has_terminal("rT") is False
    rp.phase_progress(1, 2, "mid")
    assert store.has_terminal("rT") is False  # 中间事件不算终止
    rp.run_done({"run_id": "rT"})
    assert store.has_terminal("rT") is True
    rp2 = ProgressReporter("rE", store)
    rp2.run_error("boom")
    assert store.has_terminal("rE") is True


def test_buffer_cap_and_drop() -> None:
    store = ProgressStore()
    rp = ProgressReporter("r3", store)
    for i in range(700):
        rp.phase_progress(i, 700, f"step {i}")
    # 缓冲上限保护：不超过 _EVENT_KEEP（600）
    assert len(store._buffers["r3"]) <= 600
    store.drop("r3")
    assert store._buffers.get("r3") is None


def test_run_buffer_count_is_bounded() -> None:
    """回归点：run_id 数量必须有上限，否则长驻进程内存随运行次数无限增长。"""
    from harness.modules.reverse_engineering import progress as progress_mod

    store = ProgressStore()
    total = progress_mod._RUN_KEEP + 30
    for i in range(total):
        ProgressReporter(f"run{i}", store).phase_start("rea_static", "REA", 0, 1)
    assert len(store._buffers) <= progress_mod._RUN_KEEP
    # 最旧的被淘汰；最新的必须仍在，保证刚结束的运行仍可回放
    assert "run0" not in store._buffers
    assert f"run{total - 1}" in store._buffers


async def test_subscribed_run_is_never_evicted() -> None:
    """回归点：仍有订阅者的 run 不得被淘汰，否则 emit 取不到队列、实时增量静默丢失。"""
    from harness.modules.reverse_engineering import progress as progress_mod

    store = ProgressStore()
    # 让本 run 成为最旧的一个，同时保持订阅
    ProgressReporter("live", store).phase_start("s", "S", 0, 1)
    gen = store.subscribe("live")
    await gen.__anext__()  # 回放历史事件，完成订阅注册

    for i in range(progress_mod._RUN_KEEP + 30):
        ProgressReporter(f"other{i}", store).phase_start("s", "S", 0, 1)

    assert "live" in store._buffers, "有订阅者的 run 被淘汰会导致实时事件丢失"
    assert store._subs.get("live"), "订阅队列必须保留"
    await gen.aclose()


def test_subscribe_cleanup_on_disconnect() -> None:
    store = ProgressStore()
    rp = ProgressReporter("r4", store)
    rp.phase_start("x", "X", 0, 1)

    async def short() -> None:
        async for _ev in store.subscribe("r4"):
            break  # 立即退出 → finally 应移除订阅者

    asyncio.run(short())
    # finally 清理后，该 run_id 不再有订阅者队列
    assert store._subs.get("r4") == [] or store._subs.get("r4") is None


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
