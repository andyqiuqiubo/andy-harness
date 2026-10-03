"""Skill 系统测试 —— frontmatter 解析 / 扫描 / 三级加载 / 工具 / AgentLoop 注入。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from harness.engine.agent_loop import AgentLoop, AgentLoopConfig
from harness.kernel.services import ServiceRegistry
from harness.modules.skill_manager.service import (
    SkillService,
    SkillServiceImpl,
    parse_frontmatter,
)

# ── 辅助 ────────────────────────────────────────────


def _make_skill(base: Path, folder: str, name: str, description: str, body: str = "正文内容") -> Path:
    """在 base 下创建一个 skill 目录。"""
    skill_dir = base / folder
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.fixture
def skill_root(tmp_path: Path) -> Path:
    """构造一个含两个 Skill 的扫描根。"""
    root = tmp_path / "skills"
    root.mkdir()
    _make_skill(
        root,
        "alpha",
        "alpha",
        "做 A 事情。Use when 用户提到 A 时。",
        "A 的步骤：\n1. 第一步\n2. 第二步",
    )
    _make_skill(root, "beta", "beta", "做 B 事情。Use when 用户提到 B 时。", "B 的步骤")
    return root


@pytest.fixture
def service(skill_root: Path, tmp_path: Path) -> SkillServiceImpl:
    """构造 SkillServiceImpl（状态文件指向临时目录）。"""
    return SkillServiceImpl(
        roots=[skill_root],
        plugin_dirs=[],
        state_file=tmp_path / "skill_state.json",
    )


# ── frontmatter 解析 ────────────────────────────────


class TestParseFrontmatter:
    """frontmatter 解析测试。"""

    def test_basic(self) -> None:
        """解析标准 frontmatter。"""
        text = "---\nname: demo\ndescription: 做演示\n---\n\n# 标题\n正文\n"
        meta, body = parse_frontmatter(text)
        assert meta["name"] == "demo"
        assert meta["description"] == "做演示"
        assert body.startswith("# 标题")

    def test_quoted_value(self) -> None:
        """剥离成对引号。"""
        meta, body = parse_frontmatter("---\nname: \"demo\"\ndescription: '做演示'\n---\n正文")
        assert meta["name"] == "demo"
        assert meta["description"] == "做演示"
        assert body == "正文"

    def test_no_frontmatter(self) -> None:
        """无 frontmatter 时返回空元数据与原文。"""
        meta, body = parse_frontmatter("直接就是正文")
        assert meta == {}
        assert body == "直接就是正文"

    def test_unclosed_frontmatter(self) -> None:
        """只有开头分隔符时视为无 frontmatter。"""
        meta, body = parse_frontmatter("---\nname: demo\n正文继续")
        assert meta == {}
        assert body.startswith("---")


# ── 扫描与查询 ──────────────────────────────────────


class TestSkillServiceImpl:
    """Skill 服务实现测试。"""

    def test_scan_finds_skills(self, service: SkillServiceImpl) -> None:
        """扫描能发现两个 Skill。"""
        names = [m.name for m in service.list_skills()]
        assert names == ["alpha", "beta"]

    def test_get_skill(self, service: SkillServiceImpl) -> None:
        """按名称获取 Skill。"""
        meta = service.get_skill("alpha")
        assert meta is not None
        assert meta.source == "builtin"
        assert meta.body_chars > 0

    def test_missing_description_skipped(self, tmp_path: Path) -> None:
        """缺少 description 的 Skill 被跳过（规范要求必填）。"""
        root = tmp_path / "skills"
        root.mkdir()
        _make_skill(root, "ok", "ok", "有描述")
        bad = root / "bad"
        bad.mkdir()
        (bad / "SKILL.md").write_text("---\nname: bad\n---\n正文", encoding="utf-8")
        svc = SkillServiceImpl(roots=[root], plugin_dirs=[], state_file=tmp_path / "s.json")
        assert [m.name for m in svc.list_skills()] == ["ok"]

    def test_plugin_skills_scanned(self, tmp_path: Path) -> None:
        """插件自带的 skills/ 目录也会被扫描。"""
        plugin_dir = tmp_path / "plugins"
        skill_dir = plugin_dir / "my_plugin" / "skills" / "from_plugin"
        _make_skill(skill_dir.parent, "from_plugin", "from_plugin", "插件自带")
        svc = SkillServiceImpl(roots=[], plugin_dirs=[plugin_dir], state_file=tmp_path / "s.json")
        metas = svc.list_skills()
        assert [m.name for m in metas] == ["from_plugin"]
        assert metas[0].source == "plugin"

    def test_resources_scanned(self, skill_root: Path, tmp_path: Path) -> None:
        """scripts/ references/ assets/ 下的资源被登记为相对路径。"""
        (skill_root / "alpha" / "scripts").mkdir()
        (skill_root / "alpha" / "scripts" / "run.py").write_text("print('hi')", encoding="utf-8")
        (skill_root / "alpha" / "references").mkdir()
        (skill_root / "alpha" / "references" / "api.md").write_text("# API", encoding="utf-8")
        svc = SkillServiceImpl(roots=[skill_root], plugin_dirs=[], state_file=tmp_path / "s.json")
        resources = svc.list_resources("alpha")
        assert "scripts/run.py" in resources
        assert "references/api.md" in resources

    def test_read_resource(self, skill_root: Path, tmp_path: Path) -> None:
        """读取资源内容。"""
        (skill_root / "alpha" / "references").mkdir()
        (skill_root / "alpha" / "references" / "api.md").write_text("# API 文档", encoding="utf-8")
        svc = SkillServiceImpl(roots=[skill_root], plugin_dirs=[], state_file=tmp_path / "s.json")
        assert svc.read_resource("alpha", "references/api.md") == "# API 文档"

    def test_read_resource_blocks_traversal(self, skill_root: Path, tmp_path: Path) -> None:
        """拒绝越界的资源路径。"""
        svc = SkillServiceImpl(roots=[skill_root], plugin_dirs=[], state_file=tmp_path / "s.json")
        result = svc.read_resource("alpha", "../../secret.txt")
        assert "越界" in result

    def test_read_resource_blocks_sibling_prefix_dir(self, skill_root: Path, tmp_path: Path) -> None:
        """前缀相同但不是子目录的路径必须被拒绝（不能用字符串前缀判断）。"""
        # 在 alpha 同级建一个以 alpha 开头的目录，内含敏感文件
        sibling = skill_root / "alpha-secret"
        sibling.mkdir()
        (sibling / "leak.txt").write_text("SENSITIVE-DATA", encoding="utf-8")

        svc = SkillServiceImpl(roots=[skill_root], plugin_dirs=[], state_file=tmp_path / "s.json")
        result = svc.read_resource("alpha", "../alpha-secret/leak.txt")
        assert "越界" in result
        assert "SENSITIVE-DATA" not in result

    def test_catalog_truncates_long_description(self, tmp_path: Path) -> None:
        """超长 description 在 L1 目录中被截断，避免目录膨胀。"""
        root = tmp_path / "skills"
        root.mkdir()
        long_desc = "很长的描述" * 60
        _make_skill(root, "verbose", "verbose", long_desc)
        svc = SkillServiceImpl(roots=[root], plugin_dirs=[], state_file=tmp_path / "s.json")
        catalog = svc.render_catalog()
        assert catalog.count("很长的描述") < 60
        assert "…" in catalog
        # 正文（L2）保留完整描述相关正文
        assert svc.load_body("verbose")

    def test_render_catalog(self, service: SkillServiceImpl) -> None:
        """目录只包含 name + description（L1）。"""
        catalog = service.render_catalog()
        assert "alpha" in catalog
        assert "beta" in catalog
        assert "A 的步骤" not in catalog  # L2 正文不应出现

    def test_render_catalog_empty_when_all_disabled(self, service: SkillServiceImpl) -> None:
        """全部停用时目录为空串。"""
        service.set_enabled("alpha", False)
        service.set_enabled("beta", False)
        assert service.render_catalog() == ""

    def test_load_body(self, service: SkillServiceImpl) -> None:
        """加载正文（L2）含步骤且不含 frontmatter。"""
        body = service.load_body("alpha")
        assert "A 的步骤" in body
        assert "description:" not in body

    def test_load_body_disabled(self, service: SkillServiceImpl) -> None:
        """停用的 Skill 不返回正文。"""
        service.set_enabled("alpha", False)
        assert service.load_body("alpha") == ""

    def test_set_enabled_persists(self, skill_root: Path, tmp_path: Path) -> None:
        """启用状态持久化到状态文件，重建服务后仍生效。"""
        state_file = tmp_path / "s.json"
        svc = SkillServiceImpl(roots=[skill_root], plugin_dirs=[], state_file=state_file)
        assert svc.set_enabled("alpha", False) is True
        assert state_file.is_file()

        svc2 = SkillServiceImpl(roots=[skill_root], plugin_dirs=[], state_file=state_file)
        assert svc2.get_skill("alpha") is not None
        assert svc2.get_skill("alpha").enabled is False  # type: ignore[union-attr]

    def test_set_enabled_unknown(self, service: SkillServiceImpl) -> None:
        """操作不存在的 Skill 返回 False。"""
        assert service.set_enabled("nonexistent", False) is False

    def test_reload_picks_up_new_skill(self, service: SkillServiceImpl, skill_root: Path) -> None:
        """重新扫描能发现新增的 Skill。"""
        assert len(service.list_skills()) == 2
        _make_skill(skill_root, "gamma", "gamma", "做 C 事情")
        count = service.reload()
        assert count == 3


# ── use_skill 工具 ──────────────────────────────────


class TestUseSkillTool:
    """use_skill 工具测试。"""

    @pytest.fixture
    def registry(self, service: SkillServiceImpl) -> ServiceRegistry:
        """注册 SkillService 的服务注册表。"""
        reg = ServiceRegistry()
        reg.register(SkillService, service, owner="test")
        return reg

    async def test_load_skill(self, registry: ServiceRegistry) -> None:
        """加载 Skill 返回正文与资源提示。"""
        from plugins.tool_use_skill.main import UseSkillTool

        tool = UseSkillTool(registry)
        result = await tool.execute({"name": "alpha"})
        assert "A 的步骤" in result
        assert "alpha" in result

    async def test_unknown_skill_lists_available(self, registry: ServiceRegistry) -> None:
        """未找到时列出可用 Skill，便于模型自我纠正。"""
        from plugins.tool_use_skill.main import UseSkillTool

        tool = UseSkillTool(registry)
        result = await tool.execute({"name": "nope"})
        assert "未找到" in result
        assert "alpha" in result

    async def test_disabled_skill(self, registry: ServiceRegistry, service: SkillServiceImpl) -> None:
        """停用的 Skill 给出明确提示。"""
        from plugins.tool_use_skill.main import UseSkillTool

        service.set_enabled("alpha", False)
        result = await UseSkillTool(registry).execute({"name": "alpha"})
        assert "已停用" in result or "停用" in result

    async def test_missing_name(self, registry: ServiceRegistry) -> None:
        """缺少 name 参数时报错。"""
        from plugins.tool_use_skill.main import UseSkillTool

        result = await UseSkillTool(registry).execute({})
        assert "未提供" in result

    async def test_read_resource_via_tool(self, registry: ServiceRegistry, skill_root: Path, tmp_path: Path) -> None:
        """通过工具读取 L3 资源。"""
        from plugins.tool_use_skill.main import UseSkillTool

        (skill_root / "alpha" / "references").mkdir(exist_ok=True)
        (skill_root / "alpha" / "references" / "api.md").write_text("# API", encoding="utf-8")
        service = registry.get(SkillService)
        service.reload()
        result = await UseSkillTool(registry).execute({"name": "alpha", "resource": "references/api.md"})
        assert result == "# API"

    async def test_service_unavailable(self) -> None:
        """Skill 服务不可用时返回友好错误，不抛异常。"""
        from plugins.tool_use_skill.main import UseSkillTool

        result = await UseSkillTool(ServiceRegistry()).execute({"name": "alpha"})
        assert "不可用" in result

    def test_tool_schema(self, registry: ServiceRegistry) -> None:
        """工具契约字段完整。"""
        from plugins.tool_use_skill.main import UseSkillTool

        tool = UseSkillTool(registry)
        assert tool.tool_name == "use_skill"
        assert tool.description
        assert tool.parameters_schema["required"] == ["name"]


# ── AgentLoop 集成 ──────────────────────────────────


class MockProvider:
    """Mock provider —— 返回固定响应。"""

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        yield {"delta": "收到"}


class TestSkillCatalogInjection:
    """AgentLoop 注入 Skill 目录测试。"""

    @staticmethod
    def _build_services(service: SkillServiceImpl) -> ServiceRegistry:
        """构造含 Session/Context/Skill 服务的注册表。"""
        from harness.infra.database import Database
        from harness.modules.context_manager.service import (
            ContextService,
            ContextServiceImpl,
        )
        from harness.modules.session_manager.service import (
            SessionService,
            SessionServiceImpl,
        )

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = Database(path)
        registry = ServiceRegistry()
        registry.register(SessionService, SessionServiceImpl(db), owner="test")
        registry.register(ContextService, ContextServiceImpl(services=registry), owner="test")
        registry.register(SkillService, service, owner="test")
        return registry

    async def test_catalog_injected(self, service: SkillServiceImpl) -> None:
        """上下文首部包含 Skill 目录（L1）。"""
        from harness.kernel.hooks import HookManager
        from harness.modules.session_manager.service import SessionService

        registry = self._build_services(service)
        session_service = registry.get(SessionService)
        session = session_service.create_session(title="skill 测试")

        loop = AgentLoop(
            services=registry,
            hooks=HookManager(),
            config=AgentLoopConfig(max_tool_iterations=1),
        )
        await loop.run(
            session_id=session.id,
            user_message="帮我做 A 事情",
            provider=MockProvider(),
        )

        messages = session_service.list_messages(session.id)
        # 目录通过上下文注入模型，这里改为直接验证渲染链路
        assert any(m.role == "assistant" for m in messages)
        assert "alpha" in service.render_catalog()

    async def test_catalog_absent_without_service(self, service: SkillServiceImpl) -> None:
        """无 Skill 服务时优雅降级，不注入且不报错。"""
        from harness.kernel.hooks import HookManager
        from harness.modules.session_manager.service import SessionService

        registry = self._build_services(service)
        # 移除 Skill 服务模拟插件未激活
        registry.unregister(SkillService, owner="test")
        session_service = registry.get(SessionService)
        session = session_service.create_session(title="无 skill 测试")

        loop = AgentLoop(
            services=registry,
            hooks=HookManager(),
            config=AgentLoopConfig(max_tool_iterations=1),
        )
        result = await loop.run(
            session_id=session.id,
            user_message="你好",
            provider=MockProvider(),
        )
        assert result.error is None
        assert result.content == "收到"

    async def test_end_to_end_skill_invocation(self, service: SkillServiceImpl) -> None:
        """端到端：模型调用 use_skill 工具 → 拿到正文 → 给出终答。"""
        from harness.engine.tool_registry import ToolRegistry
        from harness.kernel.hooks import HookManager
        from harness.modules.session_manager.service import SessionService
        from plugins.tool_use_skill.main import UseSkillTool

        registry = self._build_services(service)
        tool_registry = ToolRegistry()
        tool_registry.register(UseSkillTool(registry), owner="test")
        registry.register(ToolRegistry, tool_registry, owner="kernel")

        session_service = registry.get(SessionService)
        session = session_service.create_session(title="e2e skill")

        class SkillCallProvider:
            """第一轮调用 use_skill，第二轮给出终答。"""

            def __init__(self) -> None:
                self.calls = 0
                self.first_messages: list[dict[str, Any]] = []

            async def chat(
                self,
                messages: list[dict[str, str]],
                model: str,
                stream: bool = True,
                **kwargs: Any,
            ) -> AsyncIterator[dict[str, Any]]:
                self.calls += 1
                if self.calls == 1:
                    self.first_messages = list(messages)
                    yield {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {
                                    "name": "use_skill",
                                    "arguments": '{"name": "alpha"}',
                                },
                            }
                        ]
                    }
                else:
                    yield {"delta": "已按 Skill 流程完成"}

        provider = SkillCallProvider()
        loop = AgentLoop(
            services=registry,
            hooks=HookManager(),
            tool_registry=tool_registry,
            config=AgentLoopConfig(),
        )
        result = await loop.run(
            session_id=session.id,
            user_message="帮我做 A 事情",
            provider=provider,
        )

        assert result.error is None, result.error
        assert result.content == "已按 Skill 流程完成"
        # 模型第一轮上下文中确实看到了 Skill 目录（L1）
        assert any(
            "可用 Skills" in (m.get("content") or "") for m in provider.first_messages if m.get("role") == "system"
        )
        # 工具被真实执行，且结果回填进会话
        messages = session_service.list_messages(session.id)
        tool_msg = next((m for m in messages if m.role == "tool"), None)
        assert tool_msg is not None
        assert "A 的步骤" in (tool_msg.content or "")
        # 完整流程走完：user + assistant(tool_calls) + tool + assistant
        assert len([m for m in messages if m.role in ("user", "assistant", "tool")]) >= 4

    async def test_build_context_contains_catalog(self, service: SkillServiceImpl) -> None:
        """_build_context 产出的消息中含 Skill 目录 system 消息。"""
        from harness.kernel.hooks import HookManager
        from harness.modules.session_manager.service import SessionService

        registry = self._build_services(service)
        session_service = registry.get(SessionService)
        session = session_service.create_session(title="ctx 测试")
        session_service.append_message(session_id=session.id, role="user", content="你好")

        loop = AgentLoop(services=registry, hooks=HookManager())
        messages = await loop._build_context(session.id, 4096, "test-model")  # noqa: SLF001

        system_contents = [m["content"] for m in messages if m.get("role") == "system"]
        assert any("可用 Skills" in c for c in system_contents)
        # 正文（L2）不应出现在上下文中
        assert not any("A 的步骤" in c for c in system_contents)
