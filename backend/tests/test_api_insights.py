"""洞察看板 API 与指标贡献者契约测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    from harness.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    """与 app 同库的 Database 单例（HARNESS_DB_PATH 已被 conftest 隔离）。"""
    from harness.infra.database import Database

    _db = Database()
    yield _db
    _db.close()


def _seed(db: Any) -> None:
    """种子数据：1 个会话、2 条消息、2 条模型 span、3 条工具 span（1 条失败）、1 条高危审计。"""
    now = datetime.now()
    utc = datetime.utcnow()
    local_iso = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    utc_str = (utc - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    audit_iso = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")

    db.execute(
        "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("sess-ins-1", "洞察测试会话", local_iso, local_iso),
    )
    db.execute(
        "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        ("msg-ins-1", "sess-ins-1", "user", "你好", local_iso),
    )
    db.execute(
        "INSERT INTO messages (id, session_id, role, content, tokens, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("msg-ins-2", "sess-ins-1", "assistant", "你好！", 100, local_iso),
    )
    db.execute(
        "INSERT INTO spans (id, trace_id, session_id, name, kind, status, duration_ms, "
        "prompt_tokens, completion_tokens, total_tokens, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("span-ins-m1", "tr-ins", "sess-ins-1", "model_call", "model", "ok", 800, 50, 30, 80, utc_str),
    )
    db.execute(
        "INSERT INTO spans (id, trace_id, session_id, name, kind, status, duration_ms, "
        "prompt_tokens, completion_tokens, total_tokens, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("span-ins-m2", "tr-ins", "sess-ins-1", "model_call", "model", "ok", 600, 40, 20, 60, utc_str),
    )
    for i, (name, status) in enumerate((("calculator", "ok"), ("calculator", "ok"), ("web_search", "error"))):
        db.execute(
            "INSERT INTO spans (id, trace_id, session_id, name, kind, status, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f"span-ins-t{i}", "tr-ins", "sess-ins-1", name, "tool", status, 100 + i, utc_str),
        )
    db.execute(
        "INSERT INTO permission_audit (created_at, user_id, tool_name, risk, action, stage) "
        "VALUES (?, 'system', 'code_runner', 'dangerous', 'allow', 'decision')",
        (audit_iso,),
    )


class TestInsightsOverview:
    def test_overview_shape_and_counts(self, client: TestClient, db: Any) -> None:
        _seed(db)
        res = client.get("/api/insights/overview?range=7d")
        assert res.status_code == 200
        data = res.json()
        assert data["range_days"] == 7
        assert len(data["messages_by_day"]) == 7
        assert len(data["tokens_by_day"]) == 7

        totals = data["totals"]
        assert totals["messages"] >= 2
        assert totals["sessions"] >= 1
        assert totals["tool_calls"] >= 3
        assert totals["tool_errors"] >= 1
        assert totals["tool_success_rate"] is not None and totals["tool_success_rate"] < 1
        assert totals["model_tokens_total"] >= 140
        assert totals["dangerous_ops"] >= 1
        assert totals["active_days"] >= 1

        # 按日序列已零填充且包含种子日
        assert sum(d["count"] for d in data["messages_by_day"]) >= 2
        assert sum(d["total"] for d in data["tokens_by_day"]) >= 140

        # 工具 Top10：calculator 2 次且无错误
        tools = {t["name"]: t for t in data["top_tools"]}
        assert "calculator" in tools
        assert tools["calculator"]["calls"] >= 2
        assert tools["calculator"]["errors"] == 0
        assert "web_search" in tools and tools["web_search"]["errors"] >= 1

    def test_invalid_range_falls_back_to_7d(self, client: TestClient) -> None:
        res = client.get("/api/insights/overview?range=bogus")
        assert res.status_code == 200
        assert res.json()["range_days"] == 7

    def test_90d_supported(self, client: TestClient) -> None:
        res = client.get("/api/insights/overview?range=90d")
        assert res.status_code == 200
        assert res.json()["range_days"] == 90
        assert len(res.json()["messages_by_day"]) == 90


class TestPluginMetricsContract:
    """插件指标贡献者契约：注册即出现在 /api/insights/plugins，失败不拖垮整体。"""

    class _GoodContributor:
        metric_id = "test_docs_created"
        metric_name = "测试文档创建数"

        def collect(self, since: str, until: str) -> dict[str, Any]:
            return {"value": 12, "unit": "篇"}

    class _BrokenContributor:
        metric_id = "test_broken"
        metric_name = "坏掉的指标"

        def collect(self, since: str, until: str) -> dict[str, Any]:
            raise RuntimeError("boom")

    def test_registered_contributor_appears_and_broken_one_is_skipped(self, client: TestClient) -> None:
        from harness.modules.insights.service import (
            clear_metric_contributors,
            register_metric_contributor,
        )

        clear_metric_contributors()
        register_metric_contributor(self._GoodContributor())
        register_metric_contributor(self._BrokenContributor())
        try:
            res = client.get("/api/insights/plugins?range=30d")
            assert res.status_code == 200
            items = {i["metric_id"]: i for i in res.json()}
            assert items["test_docs_created"]["data"]["value"] == 12
            assert "test_broken" not in items  # 失败者被跳过
        finally:
            clear_metric_contributors()

    def test_registration_requires_contract(self) -> None:
        from harness.modules.insights.service import (
            clear_metric_contributors,
            register_metric_contributor,
        )

        clear_metric_contributors()
        with pytest.raises(ValueError):
            register_metric_contributor(object())  # 缺 metric_id / collect
        clear_metric_contributors()
