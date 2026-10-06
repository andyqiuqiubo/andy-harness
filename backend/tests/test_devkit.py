"""插件 DevKit 测试：脚手架产物可被 loader 加载、热重载生效。

注意：插件包通过 ``plugins.<id>`` 导入，且真实 ``plugins`` 包会被 pytest 进程缓存，
因此本测试直接在真实 backend/plugins 目录下用唯一 id 脚手架并在 finally 中清理，
而不是使用 tmp 目录（tmp 目录会因 ``plugins`` 包已缓存而导入失败）。
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

import pytest

from harness.kernel.loader import PluginLoader
from harness.kernel.services import ServiceRegistry
from harness.modules.devkit.service import list_templates, reload_plugin, scaffold


@pytest.fixture
def loader() -> PluginLoader:
    from harness.kernel.eventbus import EventBus
    from harness.kernel.hooks import HookManager

    return PluginLoader(events=EventBus(), services=ServiceRegistry(), hooks=HookManager())


def test_list_templates() -> None:
    keys = {t["key"] for t in list_templates()}
    assert {"tool", "provider", "integration", "hook"} <= keys


def test_scaffold_tool_and_reload(loader: PluginLoader) -> None:
    plugins_dir = Path(__file__).resolve().parents[1] / "plugins"
    pid = f"tool_devkit_{uuid.uuid4().hex[:6]}"
    pdir = plugins_dir / pid
    try:
        result = scaffold(plugins_dir, "tool", pid, "DevKit 问候工具", "生成 echo 问候")
        assert (Path(result["dir"]) / "main.py").is_file()
        assert (Path(result["dir"]) / "plugin.json").is_file()

        # manifest 必须能通过 loader 校验
        import json

        raw = json.loads((Path(result["dir"]) / "plugin.json").read_text(encoding="utf-8"))
        manifest = loader.validate_manifest(raw)
        assert manifest.id == pid

        # 加载 + 激活 → 工具注册成功
        loader.load(manifest, plugins_dir)
        asyncio.run(loader.activate(pid))
        assert loader.is_activated(pid)

        # 热重载：修改 main.py 后重载仍能成功
        main_py = Path(result["dir"]) / "main.py"
        main_py.write_text(
            main_py.read_text(encoding="utf-8").replace("echo:", "ECHO:"),
            encoding="utf-8",
        )
        result2 = asyncio.run(reload_plugin(loader, plugins_dir, pid))
        assert result2["ok"] is True
        assert loader.is_activated(pid)

        asyncio.run(loader.deactivate(pid))
        loader.unload(pid)
    finally:
        if pdir.exists():
            shutil.rmtree(pdir, ignore_errors=True)


def test_scaffold_rejects_duplicate_and_bad_id(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    scaffold(plugins_dir, "tool", "tool_dup", "重复")
    with pytest.raises(ValueError, match="已存在"):
        scaffold(plugins_dir, "tool", "tool_dup", "重复")
    with pytest.raises(ValueError, match="id"):
        scaffold(plugins_dir, "tool", "Bad-Id", "非法 id")
    with pytest.raises(ValueError, match="未知模板"):
        scaffold(plugins_dir, "nope", "tool_x", "x")


def test_reload_missing_manifest(tmp_path: Path, loader: PluginLoader) -> None:
    with pytest.raises(ValueError, match="plugin.json"):
        asyncio.run(reload_plugin(loader, tmp_path, "tool_absent"))
