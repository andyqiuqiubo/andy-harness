"""工件（大工具输出落盘）REST API 测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    from harness.main import app

    with TestClient(app) as c:
        yield c


def test_artifacts_endpoints(client: TestClient) -> None:
    """列出 / 读取 / 删除工件全链路。"""
    from harness.main import _services
    from harness.modules.artifact_store.service import ArtifactStore

    store: ArtifactStore = _services.get(ArtifactStore)
    rec = store.offload("rest-test", "code_runner", "HELLO-" + "q" * 400)

    r = client.get("/api/artifacts")
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is True
    assert rec.id in {a["id"] for a in data["artifacts"]}

    r = client.get(f"/api/artifacts/{rec.id}")
    assert r.status_code == 200
    body = r.json()
    assert "HELLO-" in body["content"]
    assert body["artifact"]["tool_name"] == "code_runner"

    # 分页读取
    r = client.get(f"/api/artifacts/{rec.id}?offset=0&limit=6")
    assert r.json()["content"] == "HELLO-"

    # 删除
    r = client.delete(f"/api/artifacts/{rec.id}")
    assert r.status_code == 200
    assert r.json()["removed"] == rec.id
    r = client.get(f"/api/artifacts/{rec.id}")
    assert r.status_code == 404


def test_artifact_not_found(client: TestClient) -> None:
    r = client.get("/api/artifacts/art_does_not_exist")
    assert r.status_code == 404
    r = client.delete("/api/artifacts/art_does_not_exist")
    assert r.status_code == 404
