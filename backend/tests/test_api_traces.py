"""运行轨迹 REST API 测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    from harness.main import app

    with TestClient(app) as c:
        yield c


def test_traces_endpoints(client: TestClient) -> None:
    from harness.main import _services
    from harness.modules.tracing.service import SpanService

    svc: SpanService = _services.get(SpanService)
    svc.record(
        trace_id="rest-t1",
        session_id="rest-s",
        name="agent_run",
        kind="run",
        duration_ms=100,
    )
    svc.record(
        trace_id="rest-t1",
        session_id="rest-s",
        name="model_call",
        kind="model",
        duration_ms=70,
        total_tokens=25,
    )

    r = client.get("/api/traces")
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is True
    assert "rest-t1" in {t["trace_id"] for t in data["traces"]}

    r = client.get("/api/traces/rest-t1")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["trace_id"] == "rest-t1"
    assert len(body["spans"]) == 2

    r = client.delete("/api/traces/rest-t1")
    assert r.status_code == 200
    assert r.json()["spans"] == 2
    assert client.get("/api/traces/rest-t1").status_code == 404
    assert client.delete("/api/traces/rest-t1").status_code == 404
