"""REST API —— 插件路由。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from harness.api.deps import require_admin
from harness.api.errors import APIError
from harness.kernel.loader import PluginLoader
from harness.kernel.services import ServiceRegistry
from harness.modules.package_installer.service import (
    PackageInstaller,
    copy_tree,
)

router = APIRouter(prefix="/api/plugins", tags=["plugins"])

# 插件目录
_PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "plugins"

# 插件市场目录（内置可安装插件包）
_MARKETPLACE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "marketplace"


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


class UpdateConfigRequest(BaseModel):
    """更新插件配置请求。"""

    config: dict[str, Any]


class ExternalInstallRequest(BaseModel):
    """从 zip / git 安装插件请求。"""

    source: str
    kind: str = "zip"  # "zip" | "git"
    ref: str | None = None  # git 分支 / tag / sha
    subdir: str | None = None  # 包嵌套在子目录时指定


def setup_plugin_routes(registry: ServiceRegistry) -> None:
    """注册插件路由。"""

    @router.get("", summary="列出插件")
    async def list_plugins() -> list[dict[str, Any]]:
        loader = _get_loader(registry)
        return loader.list_plugins()

    @router.get("/marketplace", summary="列出插件市场")
    async def marketplace() -> list[dict[str, Any]]:
        """列出插件市场中的可安装插件（含已安装标记）。"""
        loader = _get_loader(registry)
        installed_ids = {p["id"] for p in loader.list_plugins()}

        items: list[dict[str, Any]] = []
        if _MARKETPLACE_DIR.exists():
            for pkg in sorted(_MARKETPLACE_DIR.glob("*/plugin.json")):
                try:
                    meta = json.loads(pkg.read_text(encoding="utf-8"))
                except Exception:
                    continue
                main_file = pkg.parent / "main.py"
                items.append(
                    {
                        "plugin_id": meta.get("id", ""),
                        "name": meta.get("name", ""),
                        "version": meta.get("version", "0.1.0"),
                        "type": meta.get("type", "service"),
                        "entry": meta.get("entry", ""),
                        "description": meta.get("description", ""),
                        "long_description": meta.get("long_description", ""),
                        "permissions": meta.get("permissions", []),
                        "config_schema": meta.get("config_schema"),
                        "plugin_code": main_file.read_text(encoding="utf-8") if main_file.exists() else "",
                        "installed": meta.get("id") in installed_ids,
                    }
                )
        return items

    @router.post("/{plugin_id}/activate", summary="激活插件", dependencies=[Depends(require_admin)])
    async def activate_plugin(plugin_id: str) -> dict[str, str]:
        loader = _get_loader(registry)
        if not loader.get_plugin(plugin_id):
            raise APIError("PLUGIN_NOT_FOUND", f"插件不存在: {plugin_id}", 404)
        await loader.activate(plugin_id)
        return {"status": "activated", "plugin_id": plugin_id}

    @router.post("/{plugin_id}/deactivate", summary="停用插件", dependencies=[Depends(require_admin)])
    async def deactivate_plugin(plugin_id: str) -> dict[str, str]:
        loader = _get_loader(registry)
        await loader.deactivate(plugin_id)
        return {"status": "deactivated", "plugin_id": plugin_id}

    @router.post("/install", summary="安装新插件", dependencies=[Depends(require_admin)])
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
        manifest: dict[str, Any] = {
            "id": req.plugin_id,
            "name": req.name,
            "version": req.version,
            "type": req.type,
            "entry": req.entry,
            "core_api": ">=0.1.0 <1.0.0",
            "permissions": req.permissions,
            "description": req.description,
            "source": "marketplace",
        }
        if req.config_schema:
            manifest["config_schema"] = req.config_schema

        (plugin_dir / "plugin.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

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

    @router.post(
        "/marketplace/{plugin_id}/install",
        summary="从插件市场安装插件",
        dependencies=[Depends(require_admin)],
    )
    async def install_marketplace_plugin(plugin_id: str) -> dict[str, Any]:
        """从插件市场目录安装插件（服务端直装，写入 source=marketplace）。"""
        loader = _get_loader(registry)

        # 检查是否已安装
        if loader.get_plugin(plugin_id):
            raise APIError(
                "PLUGIN_ALREADY_EXISTS",
                f"插件已存在: {plugin_id}，请先卸载",
                409,
            )

        # 读取市场包元数据
        pkg_dir = _MARKETPLACE_DIR / plugin_id
        meta_path = pkg_dir / "plugin.json"
        if not pkg_dir.exists() or not meta_path.exists():
            raise APIError(
                "MARKETPLACE_NOT_FOUND",
                f"插件市场中不存在: {plugin_id}",
                404,
            )
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            raise APIError(
                "MARKETPLACE_INVALID",
                f"插件市场包元数据无效: {plugin_id}",
                500,
            )

        main_file = pkg_dir / "main.py"
        if not main_file.exists():
            raise APIError(
                "MARKETPLACE_INVALID",
                f"插件市场包缺少 main.py: {plugin_id}",
                500,
            )

        # 创建插件目录
        plugin_dir = _PLUGINS_DIR / plugin_id
        if plugin_dir.exists():
            raise APIError(
                "PLUGIN_DIR_EXISTS",
                f"插件目录已存在: {plugin_dir}",
                409,
            )

        plugin_dir.mkdir(parents=True, exist_ok=True)

        # 写入 __init__.py
        (plugin_dir / "__init__.py").write_text("", encoding="utf-8")

        # 写入 plugin.json（来源标记为 marketplace）
        manifest = {
            "id": meta.get("id", plugin_id),
            "name": meta.get("name", plugin_id),
            "version": meta.get("version", "0.1.0"),
            "type": meta.get("type", "service"),
            "entry": meta.get("entry", ""),
            "core_api": meta.get("core_api", ">=0.1.0 <1.0.0"),
            "permissions": meta.get("permissions", []),
            "description": meta.get("description", ""),
            "source": "marketplace",
        }
        if meta.get("config_schema"):
            manifest["config_schema"] = meta["config_schema"]

        (plugin_dir / "plugin.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

        # 写入 main.py
        (plugin_dir / "main.py").write_text(main_file.read_text(encoding="utf-8"), encoding="utf-8")

        # 加载并激活
        try:
            from harness.kernel.contracts.base import PluginManifest

            manifest_obj = PluginManifest.from_dict(manifest)
            loader.load(manifest_obj, _PLUGINS_DIR)
            await loader.activate(plugin_id)
        except Exception as e:
            # 加载失败，清理目录
            import shutil

            shutil.rmtree(plugin_dir, ignore_errors=True)
            raise APIError("PLUGIN_INSTALL_FAILED", f"插件加载失败: {e}", 500) from e

        return {
            "status": "installed",
            "plugin_id": plugin_id,
            "name": manifest["name"],
        }

    @router.post("/install-external", summary="从 zip / git 安装插件", dependencies=[Depends(require_admin)])
    async def install_external_plugin(req: ExternalInstallRequest) -> dict[str, Any]:
        """从外部 zip / git 仓库安装第三方插件包（生态分发，E6）。"""
        loader = _get_loader(registry)

        installer = PackageInstaller()
        res = installer.materialize(req.source, req.kind, ref=req.ref, subdir=req.subdir)
        if not res.success:
            raise APIError("PACKAGE_MATERIALIZE_FAILED", res.error, 400)

        pkg_dir = Path(res.path)
        meta_path = pkg_dir / "plugin.json"
        if not meta_path.is_file():
            raise APIError("INVALID_PACKAGE", "包根目录缺少 plugin.json", 400)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            raise APIError("INVALID_PACKAGE", "plugin.json 解析失败", 400)

        plugin_id = meta.get("id") or pkg_dir.name
        if loader.get_plugin(plugin_id):
            raise APIError(
                "PLUGIN_ALREADY_EXISTS",
                f"插件已存在: {plugin_id}，请先卸载",
                409,
            )

        dest = _PLUGINS_DIR / plugin_id
        if dest.exists():
            raise APIError(
                "PLUGIN_DIR_EXISTS",
                f"插件目录已存在: {dest}",
                409,
            )

        dest.mkdir(parents=True, exist_ok=True)
        (dest / "__init__.py").write_text("", encoding="utf-8")
        copy_tree(pkg_dir, dest)

        manifest = {
            "id": plugin_id,
            "name": meta.get("name", plugin_id),
            "version": meta.get("version", "0.1.0"),
            "type": meta.get("type", "service"),
            "entry": meta.get("entry", ""),
            "core_api": meta.get("core_api", ">=0.1.0 <1.0.0"),
            "permissions": meta.get("permissions", []),
            "description": meta.get("description", ""),
            "source": "external",
        }
        if meta.get("config_schema"):
            manifest["config_schema"] = meta["config_schema"]

        (dest / "plugin.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

        try:
            from harness.kernel.contracts.base import PluginManifest

            manifest_obj = PluginManifest.from_dict(manifest)
            loader.load(manifest_obj, _PLUGINS_DIR)
            await loader.activate(plugin_id)
        except Exception as e:
            shutil.rmtree(dest, ignore_errors=True)
            raise APIError("PLUGIN_INSTALL_FAILED", f"插件加载失败: {e}", 500) from e

        return {
            "status": "installed",
            "plugin_id": plugin_id,
            "name": manifest["name"],
        }

    @router.delete("/{plugin_id}", summary="卸载插件", dependencies=[Depends(require_admin)])
    async def uninstall_plugin(plugin_id: str) -> dict[str, str]:
        """卸载插件（停用 + 删除文件），仅允许市场来源插件。"""
        loader = _get_loader(registry)

        plugin = loader.get_plugin(plugin_id)
        if not plugin:
            raise APIError("PLUGIN_NOT_FOUND", f"插件不存在: {plugin_id}", 404)

        # 仅市场来源插件可卸载；核心/系统插件保护
        info = next((p for p in loader.list_plugins() if p["id"] == plugin_id), None)
        if info and info.get("core"):
            raise APIError(
                "PLUGIN_CORE_UNINSTALLABLE",
                "核心插件不可卸载",
                400,
            )
        if not info or info.get("source") not in ("marketplace", "external"):
            raise APIError(
                "PLUGIN_SYSTEM_UNINSTALLABLE",
                "系统插件不可卸载（仅插件市场 / 外部安装的插件可卸载）",
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

    @router.get("/{plugin_id}/config", summary="获取插件配置")
    async def get_plugin_config(plugin_id: str) -> dict[str, Any]:
        """获取插件的配置 schema 和当前配置值。"""
        loader = _get_loader(registry)
        if not loader.get_plugin(plugin_id):
            raise APIError("PLUGIN_NOT_FOUND", f"插件不存在: {plugin_id}", 404)
        return loader.get_plugin_config(plugin_id)

    @router.patch("/{plugin_id}/config", summary="更新插件配置", dependencies=[Depends(require_admin)])
    async def update_plugin_config(plugin_id: str, req: UpdateConfigRequest) -> dict[str, Any]:
        """更新插件配置。"""
        loader = _get_loader(registry)
        if not loader.get_plugin(plugin_id):
            raise APIError("PLUGIN_NOT_FOUND", f"插件不存在: {plugin_id}", 404)
        loader.set_plugin_config(plugin_id, req.config)
        return {"status": "updated", "plugin_id": plugin_id}
