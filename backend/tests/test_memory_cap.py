"""tool_memory 总条数上限（环形淘汰）回归测试。

覆盖：
- 记忆表总条数超过 HARNESS_MEMORIES_MAX 后，按 updated_at 最早优先淘汰；
- 保留最新的一批（含最近写入的条目）；
- 更新既有 key（upsert）不触发淘汰且不增加总行数。
"""

from __future__ import annotations

from harness.infra.database import Database
from harness.modules.memory_manager.service import MemoryServiceImpl


def test_memory_total_cap_eviction(tmp_path, monkeypatch) -> None:
    # 注意：_max_memories_total 有 100 的安全下限，因此这里用 120（高于下限）。
    monkeypatch.setenv("HARNESS_MEMORIES_MAX", "120")
    db = Database(str(tmp_path / "mem_cap.db"))
    svc = MemoryServiceImpl(db)

    for i in range(150):
        svc.save(key=f"k{i}", value=f"v{i}", user_id="u1")

    row = db.query_one("SELECT COUNT(*) AS n FROM memories")
    assert int(row["n"]) == 120

    keys = {m.key for m in svc.search("", limit=200, user_id="u1")}
    # 保留的是最新的 120 条（k30..k149），最早的 k0 已被淘汰
    assert "k149" in keys
    assert "k0" not in keys


def test_memory_update_does_not_grow_or_evict(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_MEMORIES_MAX", "120")
    db = Database(str(tmp_path / "mem_upd.db"))
    svc = MemoryServiceImpl(db)

    for i in range(80):
        # 同一 key 反复保存 → upsert，不产生新行
        svc.save(key="same", value=f"v{i}", user_id="u1")

    row = db.query_one("SELECT COUNT(*) AS n FROM memories")
    assert int(row["n"]) == 1
