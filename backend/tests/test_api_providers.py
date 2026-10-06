"""Provider REST API 测试（含 API Key 取回端点）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    """创建测试客户端（触发 lifespan 加载全部插件）。"""
    from harness.main import app

    with TestClient(app) as c:
        yield c


class TestProviderApiKeyEndpoint:
    """GET /api/providers/{id}/api-key：编辑对话框回填已存 Key。"""

    def test_roundtrip_returns_decrypted_key(self, client: TestClient) -> None:
        resp = client.post(
            "/api/providers",
            json={
                "name": "KeyRoundtrip",
                "base_url": "https://example.com/v1",
                "api_key": "sk-secret-123",
                "models": ["m1"],
            },
        )
        assert resp.status_code == 200
        pid = resp.json()["id"]

        res = client.get(f"/api/providers/{pid}/api-key")
        assert res.status_code == 200
        assert res.json()["api_key"] == "sk-secret-123"

    def test_missing_key_returns_empty_string(self, client: TestClient) -> None:
        # 内置 deepseek 在测试环境未配置 Key
        res = client.get("/api/providers/deepseek/api-key")
        assert res.status_code == 200
        assert res.json()["api_key"] == ""

    def test_unknown_provider_404(self, client: TestClient) -> None:
        res = client.get("/api/providers/no_such_provider/api-key")
        assert res.status_code == 404

    def test_patch_then_fetch_reflects_new_key(self, client: TestClient) -> None:
        resp = client.post(
            "/api/providers",
            json={
                "name": "KeyPatch",
                "base_url": "https://example.com/v1",
                "api_key": "sk-old",
                "models": ["m1"],
            },
        )
        pid = resp.json()["id"]
        patch = client.patch(f"/api/providers/{pid}", json={"api_key": "sk-new-456"})
        assert patch.status_code == 200
        res = client.get(f"/api/providers/{pid}/api-key")
        assert res.json()["api_key"] == "sk-new-456"

        client.delete(f"/api/providers/{pid}")
