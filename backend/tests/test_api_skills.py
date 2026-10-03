"""Skill REST API 集成测试。"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    """创建测试客户端（会触发 lifespan 加载全部插件）。"""
    from harness.main import app

    with TestClient(app) as c:
        yield c


class TestSkillsAPI:
    """Skill REST API 测试。"""

    def test_list_skills(self, client: TestClient) -> None:
        """GET /api/skills 返回内置 Skill。"""
        resp = client.get("/api/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["count"] >= 3
        names = {s["name"] for s in data["skills"]}
        assert {"code-review", "git-commit-message", "skill-authoring"} <= names

    def test_skill_structure(self, client: TestClient) -> None:
        """Skill 条目包含必要字段。"""
        resp = client.get("/api/skills")
        skill = next(s for s in resp.json()["skills"] if s["name"] == "skill-authoring")
        assert skill["description"]
        assert skill["source"] == "builtin"
        assert skill["enabled"] is True
        assert "references/description-guide.md" in skill["resources"]

    def test_get_skill_detail(self, client: TestClient) -> None:
        """GET /api/skills/{name} 返回元数据与正文。"""
        resp = client.get("/api/skills/code-review")
        assert resp.status_code == 200
        data = resp.json()
        assert data["meta"]["name"] == "code-review"
        assert "# 代码评审" in data["body"]

    def test_get_unknown_skill(self, client: TestClient) -> None:
        """GET 不存在的 Skill 返回 404。"""
        resp = client.get("/api/skills/does-not-exist")
        assert resp.status_code == 404

    def test_catalog_endpoint(self, client: TestClient) -> None:
        """GET /api/skills/catalog 返回注入模型的目录文本。"""
        resp = client.get("/api/skills/catalog")
        assert resp.status_code == 200
        catalog = resp.json()["catalog"]
        assert "可用 Skills" in catalog
        assert "code-review" in catalog
        assert "# 代码评审" not in catalog  # L2 正文不进目录

    def test_toggle_and_restore(self, client: TestClient) -> None:
        """PATCH 停用后目录不再包含该 Skill，恢复后重新出现。"""
        target = "git-commit-message"

        resp = client.patch(f"/api/skills/{target}", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        assert target not in client.get("/api/skills/catalog").json()["catalog"]

        resp = client.patch(f"/api/skills/{target}", json={"enabled": True})
        assert resp.json()["enabled"] is True
        assert target in client.get("/api/skills/catalog").json()["catalog"]

    def test_toggle_unknown(self, client: TestClient) -> None:
        """PATCH 不存在的 Skill 返回 404。"""
        resp = client.patch("/api/skills/nope", json={"enabled": False})
        assert resp.status_code == 404

    def test_reload(self, client: TestClient) -> None:
        """POST /api/skills/reload 返回扫描数量。"""
        resp = client.post("/api/skills/reload")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 3

    def test_read_resource(self, client: TestClient) -> None:
        """GET /api/skills/{name}/resource 读取随附资源。"""
        resp = client.get(
            "/api/skills/skill-authoring/resource",
            params={"path": "references/description-guide.md"},
        )
        assert resp.status_code == 200
        assert "description" in resp.json()["content"]
