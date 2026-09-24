"""健康检查端点测试。"""

from fastapi.testclient import TestClient

from harness.main import app

client = TestClient(app)


def test_health() -> None:
    """GET /api/health 返回 {"status": "ok"}。"""
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
