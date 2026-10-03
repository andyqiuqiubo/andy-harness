"""插件市场 / 技能市场 REST API 集成测试。"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    """创建测试客户端（触发 lifespan 加载全部插件）。"""
    from harness.main import app

    with TestClient(app) as c:
        yield c


# 插件市场新增的 5 个常用工具插件
EXPECTED_PLUGIN_IDS = {
    "tool_calculator",
    "tool_datetime",
    "tool_unit_converter",
    "tool_hash_encode",
    "tool_file_manager",
}

# 技能市场新增的 5 个常用 Skill
EXPECTED_SKILL_NAMES = {
    "meeting-notes",
    "email-draft",
    "explain-concept",
    "text-summary",
    "translate-text",
}


class TestPluginMarketplace:
    """插件市场接口测试。"""

    def test_lists_new_tools(self, client: TestClient) -> None:
        """GET /api/plugins/marketplace 列出新增的 5 个常用工具插件。"""
        resp = client.get("/api/plugins/marketplace")
        assert resp.status_code == 200
        data = resp.json()
        ids = {item["plugin_id"] for item in data}
        assert EXPECTED_PLUGIN_IDS <= ids, f"缺失插件: {EXPECTED_PLUGIN_IDS - ids}"

    def test_item_fields_complete(self, client: TestClient) -> None:
        """市场条目字段完整，且带 long_description 供「说明」弹窗展示。"""
        resp = client.get("/api/plugins/marketplace")
        by_id = {item["plugin_id"]: item for item in resp.json()}
        for pid in EXPECTED_PLUGIN_IDS:
            item = by_id[pid]
            assert item["name"], f"{pid} 缺少 name"
            assert item["version"], f"{pid} 缺少 version"
            assert item["type"] == "tool"
            # entry 必须与安装后的模块路径一致，否则加载会失败
            assert item["entry"] == f"plugins.{pid}.main:{_class_name(pid)}"
            assert item["description"], f"{pid} 缺少 description"
            assert item["long_description"], f"{pid} 缺少 long_description"
            # plugin_code 会在安装落成 main.py，不能为空
            assert item["plugin_code"].strip(), f"{pid} 缺少 main.py 代码"
            assert isinstance(item["installed"], bool)


def _class_name(plugin_id: str) -> str:
    """按命名约定推导插件入口类名。"""
    mapping = {
        "tool_calculator": "CalculatorPlugin",
        "tool_datetime": "DatetimePlugin",
        "tool_unit_converter": "UnitConverterPlugin",
        "tool_hash_encode": "HashEncodePlugin",
        "tool_file_manager": "FileManagerPlugin",
    }
    return mapping[plugin_id]


class TestSkillMarketplace:
    """技能市场接口测试。"""

    def test_lists_new_skills(self, client: TestClient) -> None:
        """GET /api/skills/marketplace 列出新增的 5 个常用 Skill。"""
        resp = client.get("/api/skills/marketplace")
        assert resp.status_code == 200
        data = resp.json()
        names = {item["name"] for item in data}
        assert EXPECTED_SKILL_NAMES <= names, f"缺失 Skill: {EXPECTED_SKILL_NAMES - names}"

    def test_frontmatter_parsed(self, client: TestClient) -> None:
        """技能市场 SKILL.md 的 frontmatter 能被正确解析出描述。

        回归保护：Skill 缺少 description 会被扫描器直接跳过，
        因此市场条目必须携带非空 description。
        """
        resp = client.get("/api/skills/marketplace")
        by_name = {item["name"]: item for item in resp.json()}
        for name in EXPECTED_SKILL_NAMES:
            item = by_name[name]
            assert item["description"], f"{name} 缺少 description"
            assert item["version"], f"{name} 缺少 version"
            assert item["long_description"], f"{name} 缺少正文"
            assert item["installed"] is False

    def test_install_uninstall_round_trip(self, client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """技能市场：安装 → 标记已安装 → 卸载。

        把用户级 skills 目录重定向到临时目录，避免污染真实的
        ~/.andy-harness/skills/。
        """
        from harness.api.rest import skills as skills_api

        monkeypatch.setattr(skills_api, "_user_skills_dir", lambda: tmp_path)

        pkg_id = "text-summary"

        # 安装
        resp = client.post(f"/api/skills/marketplace/{pkg_id}/install")
        assert resp.status_code == 200
        assert resp.json()["status"] == "installed"
        assert (tmp_path / pkg_id / "SKILL.md").is_file()

        # 市场列表应标记为已安装
        data = client.get("/api/skills/marketplace").json()
        target = next(i for i in data if i["package_id"] == pkg_id)
        assert target["installed"] is True

        # 重复安装应冲突
        resp2 = client.post(f"/api/skills/marketplace/{pkg_id}/install")
        assert resp2.status_code == 409

        # 卸载
        resp3 = client.delete(f"/api/skills/marketplace/{pkg_id}")
        assert resp3.status_code == 200
        assert resp3.json()["status"] == "uninstalled"
        assert not (tmp_path / pkg_id).exists()

        # 重复卸载应 404
        resp4 = client.delete(f"/api/skills/marketplace/{pkg_id}")
        assert resp4.status_code == 404

    def test_install_unknown_package(self, client: TestClient) -> None:
        """安装不存在的技能包返回 404。"""
        resp = client.post("/api/skills/marketplace/no-such-skill/install")
        assert resp.status_code == 404
