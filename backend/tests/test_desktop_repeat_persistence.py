"""desktop_repeat 持久化层集成测试（真库、真 SQL）。

覆盖：三张表 DDL 幂等、run 生命周期落库、step hits 落库与脱敏、audit 落库、
hit_stats 聚合、get_run 重启后可读（服务内存丢失后走 DB 路径）。
"""
import json
from pathlib import Path

import pytest

from harness.infra.database import Database
from harness.modules.desktop_repeat.repository import RepeatRepository


@pytest.fixture()
def repo(tmp_path: Path) -> RepeatRepository:
    db = Database(db_path=str(tmp_path / "r.db"))
    r = RepeatRepository(db)
    r.setup()
    return r


def _rec_doc():
    return {"status": "succeeded", "graph_sha256": "ab" * 32, "unattended": True,
            "started_at": "2026-10-19T08:00:00+08:00",
            "vars": {"cust_name": "ACME", "password": "***"}}


def _record(status="done", **kw):
    class R:
        def to_dict(self):
            base = {"sid": "s01", "index": 0, "action": "click", "status": status,
                    "hit_index": 0, "hit_kind": "uia", "attempts": [{"kind": "uia", "result": "hit"}],
                    "elapsed_ms": 12, "degraded": False, "blind": False, "code": "", "message": "",
                    }
            base.update(kw)
            return base
    return R()


class TestRunsLifecycle:
    def test_insert_and_finish(self, repo: RepeatRepository) -> None:
        repo.insert_run("R1", "wf-1", _rec_doc(), steps_total=2)
        row = repo.get_run("R1")
        assert row is not None and row["status"] == "succeeded"
        assert row["vars"] == {"cust_name": "ACME", "password": "***"}
        assert row["unattended"] is True
        repo.finish_run("R1", {"status": "failed", "env_level": "L1",
                               "drift": {"d": 1}, "stats": {"done": 1, "degraded": 1},
                               "attempts_total": 3, "error_code": "RP-2001",
                               "error_message": "定位失败", "diagnostics_path": "x",
                               "checkpoint": {"index": 1}, "blind_used": True,
                               "duration_ms": 1500, "finished_at": "t"})
        got = repo.get_run("R1")
        assert got["status"] == "failed" and got["env_level"] == "L1"
        assert got["drift"] == {"d": 1} and got["done_steps"] == 1 and got["degraded_steps"] == 1
        assert got["attempts_total"] == 3 and got["error_code"] == "RP-2001"
        assert got["checkpoint"] == {"index": 1} and got["blind_used"] is True
        assert got["duration_ms"] == 1500

    def test_list_and_filter(self, repo: RepeatRepository) -> None:
        repo.insert_run("R1", "wf-1", _rec_doc(), steps_total=1)
        repo.insert_run("R2", "wf-2", _rec_doc(), steps_total=1)
        assert {r["run_id"] for r in repo.list_runs()} == {"R1", "R2"}
        only1 = repo.list_runs("wf-1")
        assert len(only1) == 1 and only1[0]["run_id"] == "R1"

    def test_setup_idempotent(self, repo: RepeatRepository) -> None:
        repo.setup()
        repo.setup()
        repo.insert_run("R1", "wf", _rec_doc(), steps_total=1)

    def test_missing_run_none(self, repo: RepeatRepository) -> None:
        assert repo.get_run("nope") is None


class TestStepHits:
    def test_record_and_read(self, repo: RepeatRepository) -> None:
        repo.insert_run("R1", "wf", _rec_doc(), steps_total=2)
        repo.record_step("R1", _record())
        repo.record_step("R1", _record(status="failed", index=1, sid="s02",
                                       hit_index=-1, hit_kind="", degraded=True,
                                       code="RP-2001", message="目标未找到"))
        hits = repo.step_hits("R1")
        assert [h["sid"] for h in hits] == ["s01", "s02"]
        assert hits[0]["attempts"][0]["kind"] == "uia"
        assert hits[1]["degraded"] is True and hits[1]["code"] == "RP-2001"

    def test_message_redacted(self, repo: RepeatRepository) -> None:
        repo.insert_run("R1", "wf", _rec_doc(), steps_total=1)
        long_msg = "秘密：my-password-is-123；" + "x" * 400
        repo.record_step("R1", _record(message=long_msg))
        got = repo.step_hits("R1")[0]["message"]
        assert len(got) <= 301  # 300 + 省略号
        assert got.endswith("…")

    def test_get_run_embeds_steps(self, repo: RepeatRepository) -> None:
        repo.insert_run("R1", "wf", _rec_doc(), steps_total=1)
        repo.record_step("R1", _record())
        assert len(repo.get_run("R1")["steps"]) == 1


class TestAudit:
    def test_decision_and_approval(self, repo: RepeatRepository) -> None:
        repo.insert_audit({"run_id": "R1", "sid": "s01", "action": "click",
                           "risk": "write", "decision": "allow", "reason": "会话已授权"})
        repo.insert_approval("R1", {"sid": "s02", "action": "type", "risk": "dangerous",
                                    "value": "明文不该出现", "extra": 1})
        rows = repo.list_audit("R1")
        kinds = {r["kind"] for r in rows}
        assert kinds == {"decision", "approval"}
        approval = [r for r in rows if r["kind"] == "approval"][0]
        assert approval["decision"] == "confirm"
        assert "value" not in json.dumps(approval.get("detail", {}), ensure_ascii=False)
        assert "明文不该出现" not in json.dumps(approval, ensure_ascii=False)

    def test_reason_redacted(self, repo: RepeatRepository) -> None:
        repo.insert_audit({"run_id": "R1", "sid": "", "action": "a", "risk": "r",
                           "decision": "deny", "reason": "y" * 500})
        rows = repo.list_audit("R1")
        assert len(rows[0]["reason"]) <= 301


class TestHitStats:
    def test_primary_rate_and_fragile(self, repo: RepeatRepository) -> None:
        # 同一步连续 6 次 miss（hit_index=-1）→ 应进 fragile
        for i in range(6):
            rid = f"R{i}"
            repo.insert_run(rid, "wf", _rec_doc(), steps_total=1)
            repo.record_step(rid, _record(hit_index=-1))
        st = repo.hit_stats("wf")
        assert st["per_step"]["s01"]["total"] == 6
        assert st["per_step"]["s01"]["primary"] == 0
        assert any(f["sid"] == "s01" and f["primary_rate"] == 0.0 for f in st["fragile"])

    def test_healthy_step_not_fragile(self, repo: RepeatRepository) -> None:
        for i in range(3):
            rid = f"R{i}"
            repo.insert_run(rid, "wf", _rec_doc(), steps_total=1)
            repo.record_step(rid, _record(hit_index=0))
        st = repo.hit_stats("wf")
        assert st["fragile"] == []


class TestDbRawIntegrity:
    def test_tables_exist(self, repo: RepeatRepository) -> None:
        names = {r[0] for r in repo._db.query(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'repeat_%'")}
        assert {"repeat_sessions", "repeat_runs", "repeat_step_hits", "repeat_audit"} <= names


if __name__ == "__main__":  # 便于单独调试
    pytest.main([__file__, "-q"])


def test_touch_run_updates_status_and_done_while_running(repo):
    """回归（真机）：挂起/长时间运行时 DB 必须反映真实状态。

    旧实现只在 run() 返回后写库：17 步流程执行到第 2 步挂起，库里仍是
    running / done_steps=0，页面刷新后完全看不到进度。
    """
    repo.insert_run("run-x", "wf1", {"status": "running", "vars": {}, "started_at": "t0"},
                    steps_total=17)

    class Rec:
        def to_dict(self):
            return {"status": "suspended", "env_level": "L0", "attempts_total": 2,
                    "error_code": "RP-4003", "error_message": "等待人工审批",
                    "stats": {"total": 17, "done": 1, "degraded": 1}}

    repo.touch_run("run-x", Rec())
    d = repo.get_run("run-x")
    assert d["status"] == "suspended"
    assert d["done_steps"] == 1
    assert d["error_code"] == "RP-4003"


def test_mark_stale_runs_marks_only_non_terminal(repo):
    """服务重启后遗留的 running/suspended 必须收口为 interrupted（RP-7005）。"""
    repo.insert_run("run-a", "wf1", {"status": "running", "vars": {}, "started_at": "t0"},
                    steps_total=3)
    repo.insert_run("run-b", "wf1", {"status": "succeeded", "vars": {}, "started_at": "t0"},
                    steps_total=3)
    n = repo.mark_stale_runs()
    assert n == 1
    assert repo.get_run("run-a")["status"] == "interrupted"
    assert repo.get_run("run-a")["error_code"] == "RP-7005"
    assert repo.get_run("run-b")["status"] == "succeeded", "终态记录被误改"
