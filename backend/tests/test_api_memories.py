"""长期记忆 REST API 测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    from harness.main import app

    with TestClient(app) as c:
        yield c


def test_memory_crud(client: TestClient) -> None:
    """保存 → 列表 → 检索 → 更新 → 删除 全链路。"""
    r = client.post("/api/memories", json={"key": "rest.k", "value": "v1"})
    assert r.status_code == 200
    mem = r.json()["memory"]
    mid = mem["id"]
    assert mem["scope"] == "global"

    r = client.get("/api/memories", params={"query": "rest.k"})
    assert r.status_code == 200
    assert r.json()["available"] is True
    assert mid in {m["id"] for m in r.json()["memories"]}

    # 相同 key 覆盖
    r = client.post("/api/memories", json={"key": "rest.k", "value": "v2"})
    assert r.json()["memory"]["id"] == mid
    assert r.json()["memory"]["value"] == "v2"

    # 更新
    r = client.patch(f"/api/memories/{mid}", json={"value": "v3", "tags": ["x"]})
    assert r.status_code == 200
    assert r.json()["memory"]["value"] == "v3"
    assert r.json()["memory"]["tags"] == ["x"]

    # 删除
    r = client.delete(f"/api/memories/{mid}")
    assert r.status_code == 200
    assert r.json()["removed"] == mid
    r = client.delete(f"/api/memories/{mid}")
    assert r.status_code == 404


def test_memory_validation_and_404(client: TestClient) -> None:
    assert client.post("/api/memories", json={"key": "  ", "value": "v"}).status_code == 400
    assert (
        client.post(
            "/api/memories",
            json={"key": "k", "value": "v", "scope": "session"},
        ).status_code
        == 400
    )
    assert client.patch("/api/memories/nope", json={"value": "x"}).status_code == 404


def test_memory_summary_endpoints(client: TestClient) -> None:
    """自动总结：配置查询 + 手动触发（无可用 provider 时返回跳过原因而非报错）。"""
    r = client.get("/api/memories/summary-info")
    assert r.status_code == 200
    info = r.json()
    assert info["available"] is True
    assert "enabled" in info and "interval_seconds" in info

    r = client.post("/api/memories/summarize")
    assert r.status_code == 200
    body = r.json()
    assert "scanned" in body and "saved" in body and "skipped_reason" in body
