"""REST API —— 插件路由。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from harness.api.errors import APIError
from harness.kernel.loader import PluginLoader
from harness.kernel.services import ServiceRegistry

router = APIRouter(prefix="/api/plugins", tags=["plugins"])

# 插件目录
_PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "plugins"


def _get_loader(registry: ServiceRegistry) -> PluginLoader:
    """获取 PluginLoader。"""
    try:
        loader = registry.get(PluginLoader)
        return loader  # type: ignore[no-any-return]
    except Exception:
        raise APIError("PLUGIN_LOADER_UNAVAILABLE", "插件加载器不可用", 503)


class InstallPluginRequest(BaseModel):
    """安装插件请求。"""

    plugin_id: str
    name: str
    version: str = "0.1.0"
    type: str = "tool"
    entry: str
    permissions: list[str] = []
    description: str = ""
    config_schema: dict[str, Any] | None = None
    plugin_code: str  # Python 源码


def setup_plugin_routes(registry: ServiceRegistry) -> None:
    """注册插件路由。"""

    @router.get("", summary="列出插件")
    async def list_plugins() -> list[dict[str, Any]]:
        loader = _get_loader(registry)
        return loader.list_plugins()

    @router.post("/{plugin_id}/activate", summary="激活插件")
    async def activate_plugin(plugin_id: str) -> dict[str, str]:
        loader = _get_loader(registry)
        if not loader.get_plugin(plugin_id):
            raise APIError("PLUGIN_NOT_FOUND", f"插件不存在: {plugin_id}", 404)
        await loader.activate(plugin_id)
        return {"status": "activated", "plugin_id": plugin_id}

    @router.post("/{plugin_id}/deactivate", summary="停用插件")
    async def deactivate_plugin(plugin_id: str) -> dict[str, str]:
        loader = _get_loader(registry)
        await loader.deactivate(plugin_id)
        return {"status": "deactivated", "plugin_id": plugin_id}

    @router.post("/install", summary="安装新插件")
    async def install_plugin(req: InstallPluginRequest) -> dict[str, Any]:
        """安装新插件到 plugins 目录。"""
        loader = _get_loader(registry)

        # 检查是否已存在
        if loader.get_plugin(req.plugin_id):
            raise APIError(
                "PLUGIN_ALREADY_EXISTS",
                f"插件已存在: {req.plugin_id}，请先卸载",
                409,
            )

        # 创建插件目录
        plugin_dir = _PLUGINS_DIR / req.plugin_id
        if plugin_dir.exists():
            raise APIError(
                "PLUGIN_DIR_EXISTS",
                f"插件目录已存在: {plugin_dir}",
                409,
            )

        plugin_dir.mkdir(parents=True, exist_ok=True)

        # 写入 __init__.py
        (plugin_dir / "__init__.py").write_text("", encoding="utf-8")

        # 写入 plugin.json
        manifest = {
            "id": req.plugin_id,
            "name": req.name,
            "version": req.version,
            "type": req.type,
            "entry": req.entry,
            "core_api": ">=0.1.0 <1.0.0",
            "permissions": req.permissions,
            "description": req.description,
        }
        if req.config_schema:
            manifest["config_schema"] = req.config_schema

        (plugin_dir / "plugin.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # 写入 main.py
        (plugin_dir / "main.py").write_text(req.plugin_code, encoding="utf-8")

        # 加载并激活
        try:
            from harness.kernel.contracts.base import PluginManifest

            manifest_obj = PluginManifest.from_dict(manifest)
            loader.load(manifest_obj, _PLUGINS_DIR)
            await loader.activate(req.plugin_id)
        except Exception as e:
            # 加载失败，清理目录
            import shutil

            shutil.rmtree(plugin_dir, ignore_errors=True)
            raise APIError("PLUGIN_INSTALL_FAILED", f"插件加载失败: {e}", 500) from e

        return {
            "status": "installed",
            "plugin_id": req.plugin_id,
            "name": req.name,
        }

    @router.delete("/{plugin_id}", summary="卸载插件")
    async def uninstall_plugin(plugin_id: str) -> dict[str, str]:
        """卸载插件（停用 + 删除文件）。"""
        loader = _get_loader(registry)

        plugin = loader.get_plugin(plugin_id)
        if not plugin:
            raise APIError("PLUGIN_NOT_FOUND", f"插件不存在: {plugin_id}", 404)

        # 检查是否为核心插件
        for info in loader.list_plugins():
            if info["id"] == plugin_id and info.get("core"):
                raise APIError(
                    "PLUGIN_CORE_UNINSTALLABLE",
                    "核心插件不可卸载",
                    400,
                )

        # 停用
        if loader.is_activated(plugin_id):
            await loader.deactivate(plugin_id)

        # 卸载
        try:
            loader.unload(plugin_id)
        except Exception:
            pass

        # 删除目录
        plugin_dir = _PLUGINS_DIR / plugin_id
        if plugin_dir.exists():
            import shutil

            shutil.rmtree(plugin_dir, ignore_errors=True)

        return {"status": "uninstalled", "plugin_id": plugin_id}
