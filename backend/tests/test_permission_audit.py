"""权限审计日志增量方案 —— 单元测试 + REST 集成测试。

覆盖：
- ``record_audit_event`` 正确落库；
- ``list_tool_risks()`` / ``decide()`` **不**产生审计行（回归 §3 的关键陷阱）；
- 只读 REST：列表(过滤+分页) / stats / 详情 / 404。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    """完整应用测试客户端（conftest 已把数据库隔离到临时目录）。"""
    from harness.main import app

    with TestClient(app) as c:
        yield c


def _count_audit_rows() -> int:
    from harness.infra.database import Database

    row = Database().query_one("SELECT COUNT(*) AS c FROM permission_audit")
    return int(row["c"]) if row else 0


class TestAuditRecording:
    """直接落库测试。"""

    def test_record_writes_row(self) -> None:
        from harness.modules.permission_manager.audit import record_audit_event

        before = _count_audit_rows()
        record_audit_event(
            tool_name="shell_exec",
            risk="dangerous",
            action="confirm",
            stage="decision",
            reason="当前策略：危险操作需确认",
            policy_mode="confirm_dangerous",
            decided_by="policy",
            user_id="u_1",
            session_id="s_1",
            agent_run_id="r_1",
            trace_id="t_1",
        )
        after = _count_audit_rows()
        assert after == before + 1

        from harness.infra.database import Database

        row = Database().query_one(
            "SELECT * FROM permission_audit WHERE tool_name = ? ORDER BY id DESC",
            ("shell_exec",),
        )
        assert row is not None
        assert row["risk"] == "dangerous"
        assert row["action"] == "confirm"
        assert row["stage"] == "decision"
        assert row["outcome"] == "confirm"  # outcome 默认回落为 action
        assert row["decided_by"] == "policy"
        assert row["policy_mode"] == "confirm_dangerous"
        assert row["user_id"] == "u_1"
        assert row["session_id"] == "s_1"

    def test_resolved_outcome_override(self) -> None:
        from harness.modules.permission_manager.audit import record_audit_event

        record_audit_event(
            tool_name="file_write",
            risk="write",
            action="confirm",
            stage="resolved",
            outcome="executed",
            decided_by="human",
        )
        from harness.infra.database import Database

        row = Database().query_one(
            "SELECT * FROM permission_audit WHERE stage = 'resolved' ORDER BY id DESC",
        )
        assert row is not None
        assert row["outcome"] == "executed"
        assert row["decided_by"] == "human"


class TestListToolRisksNoAudit:
    """回归 §3 关键陷阱：decide() / list_tool_risks() 绝不写审计。"""

    def test_decide_does_not_write(self, tmp_path) -> None:
        from harness.modules.permission_manager.service import PermissionServiceImpl

        registry = SimpleNamespace(
            list_tools=lambda: [{"name": "t1"}, {"name": "t2"}],
            get=lambda name: SimpleNamespace(risk_level="write"),
        )
        service = PermissionServiceImpl(registry, state_file=tmp_path / "p.json")
        before = _count_audit_rows()
        for name in ("t1", "t2"):
            service.decide(name)
        after = _count_audit_rows()
        assert after == before, "decide() 不得触发审计落库"

    def test_list_tool_risks_does_not_write(self, tmp_path) -> None:
        from harness.modules.permission_manager.service import PermissionServiceImpl

        registry = SimpleNamespace(
            list_tools=lambda: [{"name": "t1"}, {"name": "t2"}, {"name": "t3"}],
            get=lambda name: SimpleNamespace(risk_level="dangerous"),
        )
        service = PermissionServiceImpl(registry, state_file=tmp_path / "p.json")
        before = _count_audit_rows()
        risks = service.list_tool_risks()
        after = _count_audit_rows()
        assert after == before, "list_tool_risks() 不得触发审计落库"
        assert len(risks) == 3


class TestAuditApi:
    """只读 REST 集成测试。"""

    @pytest.fixture(autouse=True)
    def _clean_audit(self) -> None:
        """会话级共享同一临时库，API 测试前清空审计表，避免累计行影响绝对计数。"""
        from harness.infra.database import Database

        Database().execute("DELETE FROM permission_audit")
        yield

    def test_empty_list(self, client: TestClient) -> None:
        resp = client.get("/api/permissions/audit")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []

    def test_list_filter_and_pagination(self, client: TestClient) -> None:
        from harness.modules.permission_manager.audit import record_audit_event

        for i in range(5):
            record_audit_event(
                tool_name="shell_exec" if i % 2 == 0 else "file_write",
                risk="dangerous" if i % 2 == 0 else "write",
                action="confirm" if i % 2 == 0 else "allow",
                stage="decision",
                decided_by="policy",
            )

        # 按 tool_name 过滤
        resp = client.get("/api/permissions/audit", params={"tool_name": "shell_exec"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert all(it["tool_name"] == "shell_exec" for it in body["items"])

        # 分页
        resp2 = client.get("/api/permissions/audit", params={"limit": 2, "offset": 0})
        assert resp2.json()["limit"] == 2

    def test_stats(self, client: TestClient) -> None:
        from harness.modules.permission_manager.audit import record_audit_event

        record_audit_event(
            tool_name="shell_exec",
            risk="dangerous",
            action="confirm",
            stage="decision",
            decided_by="policy",
        )
        record_audit_event(
            tool_name="file_write",
            risk="write",
            action="allow",
            stage="decision",
            decided_by="override",
        )
        resp = client.get("/api/permissions/audit/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["by_action"].get("confirm") == 1
        assert body["by_action"].get("allow") == 1
        assert body["by_decided_by"].get("policy") == 1
        assert body["by_decided_by"].get("override") == 1
        assert any(t["tool_name"] == "shell_exec" for t in body["top_tools"])

    def test_detail_and_404(self, client: TestClient) -> None:
        from harness.modules.permission_manager.audit import record_audit_event

        record_audit_event(
            tool_name="shell_exec",
            risk="dangerous",
            action="confirm",
            stage="decision",
            decided_by="policy",
        )
        # 刚插入的是最小 id（自增从 1 开始），但更稳妥地用 list 取 id
        lst = client.get("/api/permissions/audit").json()
        assert lst["total"] >= 1
        aid = lst["items"][0]["id"]

        resp = client.get(f"/api/permissions/audit/{aid}")
        assert resp.status_code == 200
        assert resp.json()["id"] == aid

        resp404 = client.get("/api/permissions/audit/999999")
        assert resp404.status_code == 404
