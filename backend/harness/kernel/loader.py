"""PluginLoader —— 插件加载器。

负责：发现 → 校验 → 加载 → 注册 → activate/deactivate。
核心插件（core: true）不可被 deactivate。
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.eventbus import EventBus
from harness.kernel.exceptions import (
    PluginAlreadyLoadedError,
    PluginDeactivateError,
    PluginLoadError,
    PluginNotLoadedError,
    PluginValidationError,
)
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry

logger = logging.getLogger("harness.loader")

# 内核 API 版本
CORE_API_VERSION = "0.1.0"


class PluginLoader:
    """插件加载器。

    生命周期: discover → validate → load → activate ⇄ deactivate → unload
    """

    def __init__(
        self,
        events: EventBus | None = None,
        services: ServiceRegistry | None = None,
        hooks: HookManager | None = None,
    ) -> None:
        self._events: EventBus = events or EventBus()
        self._services: ServiceRegistry = services or ServiceRegistry()
        self._hooks: HookManager = hooks or HookManager()

        # 已加载的插件: plugin_id -> (plugin_instance, manifest, ctx)
        self._loaded: dict[str, tuple[BasePlugin, PluginManifest, PluginContext]] = {}
        # 已激活的插件: plugin_id -> True
        self._activated: set[str] = set()

    @property
    def events(self) -> EventBus:
        """事件总线。"""
        return self._events

    @property
    def services(self) -> ServiceRegistry:
        """服务注册表。"""
        return self._services

    @property
    def hooks(self) -> HookManager:
        """钩子管理器。"""
        return self._hooks

    # ── 发现 ──────────────────────────────────────────

    def discover(self, plugins_dir: str | Path) -> list[Path]:
        """扫描插件目录，返回所有 plugin.json 路径。"""
        plugins_path = Path(plugins_dir)
        if not plugins_path.exists():
            return []
        return sorted(plugins_path.glob("*/plugin.json"))

    # ── 校验 ──────────────────────────────────────────

    def validate_manifest(self, raw: dict[str, Any]) -> PluginManifest:
        """校验插件清单。

        Raises:
            PluginValidationError: 校验失败
        """
        plugin_id = raw.get("id", "<unknown>")

        # 必填字段检查
        required = ["id", "name", "version", "type", "entry"]
        for field in required:
            if field not in raw:
                raise PluginValidationError(plugin_id, f"缺少必填字段: {field}")

        # type 合法性
        from harness.kernel.contracts.base import PluginType

        valid_types = {t.value for t in PluginType}
        if raw["type"] not in valid_types:
            raise PluginValidationError(
                plugin_id, f"无效的 type: {raw['type']}，应为 {valid_types}"
            )

        # core_api 兼容性检查
        core_api = raw.get("core_api", ">=0.1.0 <1.0.0")
        if not self._check_core_api_compat(core_api):
            raise PluginValidationError(
                plugin_id,
                f"core_api 不兼容: 要求 {core_api}，当前内核版本 {CORE_API_VERSION}",
            )

        return PluginManifest.from_dict(raw)

    @staticmethod
    def _check_core_api_compat(range_spec: str) -> bool:
        """检查 core_api 版本范围是否与当前内核版本兼容。

        支持简单的语义化版本范围，如 ">=0.1.0 <1.0.0"。
        """
        version = CORE_API_VERSION
        # 提取版本号各部分
        try:
            parts = [int(p) for p in version.split(".")]
        except ValueError:
            return True  # 无法解析则放行

        # 解析范围约束
        constraints = re.findall(r"(>=|<=|>|<|==|!=)\s*(\d+\.\d+\.\d+)", range_spec)
        for op, ver in constraints:
            ver_parts = [int(p) for p in ver.split(".")]
            if op == ">=" and not (parts >= ver_parts):
                return False
            elif op == "<=" and not (parts <= ver_parts):
                return False
            elif op == ">" and not (parts > ver_parts):
                return False
            elif op == "<" and not (parts < ver_parts):
                return False
            elif op == "==" and not (parts == ver_parts):
                return False
            elif op == "!=" and not (parts != ver_parts):
                return False

        return True

    # ── 加载 ──────────────────────────────────────────

    def load(self, manifest: PluginManifest, plugins_dir: str | Path) -> BasePlugin:
        """加载插件（import 模块并实例化），不 activate。

        Raises:
            PluginAlreadyLoadedError: 插件已加载
            PluginLoadError: 加载失败
        """
        if manifest.id in self._loaded:
            raise PluginAlreadyLoadedError(manifest.id)

        try:
            # 解析 entry: "module.path:ClassName"
            module_path, class_name = manifest.entry.split(":")

            # 确保 plugins 目录在 sys.path 中
            parent_path = str(Path(plugins_dir).resolve().parent)
            if parent_path not in sys.path:
                sys.path.insert(0, parent_path)

            module = importlib.import_module(module_path)
            plugin_class = getattr(module, class_name)
            plugin_instance: BasePlugin = plugin_class()

            # 设置 manifest
            plugin_instance.manifest = manifest

            # 创建 PluginContext，传入配置
            from harness.kernel.context import PluginConfig

            config = PluginConfig()
            if manifest.config_schema:
                # 应用 config_schema 中声明的默认值
                properties = manifest.config_schema.get("properties", {})
                for key, prop in properties.items():
                    if "default" in prop:
                        config.set(key, prop["default"])

            ctx = PluginContext(
                plugin_id=manifest.id,
                config=config,
                events=self._events,
                services=self._services,
                hooks=self._hooks,
            )

            self._loaded[manifest.id] = (plugin_instance, manifest, ctx)
            logger.info("插件已加载: %s", manifest.id)
            return plugin_instance

        except Exception as e:
            raise PluginLoadError(manifest.id, str(e)) from e

    # ── 激活/停用 ────────────────────────────────────

    async def activate(self, plugin_id: str) -> None:
        """激活插件。"""
        if plugin_id not in self._loaded:
            raise PluginNotLoadedError(plugin_id)
        if plugin_id in self._activated:
            logger.warning("插件已激活: %s", plugin_id)
            return

        plugin, manifest, ctx = self._loaded[plugin_id]
        await plugin.activate(ctx)
        self._activated.add(plugin_id)
        logger.info("插件已激活: %s", plugin_id)

    async def deactivate(self, plugin_id: str) -> None:
        """停用插件。

        Raises:
            PluginNotLoadedError: 插件未加载
            PluginDeactivateError: 核心插件不可停用
        """
        if plugin_id not in self._loaded:
            raise PluginNotLoadedError(plugin_id)
        if plugin_id not in self._activated:
            logger.warning("插件未激活: %s", plugin_id)
            return

        plugin, manifest, ctx = self._loaded[plugin_id]

        # 核心插件保护
        if manifest.core:
            raise PluginDeactivateError(
                plugin_id, "核心插件不可停用（core: true）"
            )

        await plugin.deactivate(ctx)

        # 清理：注销服务、事件订阅、钩子、工具
        self._services.unregister_all(plugin_id)
        await self._events.unsubscribe_all(plugin_id)
        self._hooks.unregister_all(plugin_id)

        # 清理 ToolRegistry 中该插件注册的工具
        try:
            from harness.engine.tool_registry import ToolRegistry

            tool_registry = self._services.get(ToolRegistry)
            tool_registry.unregister_all(plugin_id)
        except Exception:
            pass

        self._activated.discard(plugin_id)
        logger.info("插件已停用: %s", plugin_id)

    # ── 卸载 ──────────────────────────────────────────

    def unload(self, plugin_id: str) -> None:
        """卸载插件（从内存移除）。

        核心插件不可卸载。需先 deactivate。
        """
        if plugin_id not in self._loaded:
            raise PluginNotLoadedError(plugin_id)

        manifest = self._loaded[plugin_id][1]
        if manifest.core:
            raise PluginDeactivateError(
                plugin_id, "核心插件不可卸载（core: true）"
            )

        if plugin_id in self._activated:
            raise PluginDeactivateError(
                plugin_id, "请先停用插件再卸载"
            )

        del self._loaded[plugin_id]
        logger.info("插件已卸载: %s", plugin_id)

    # ── 查询 ──────────────────────────────────────────

    def get_plugin(self, plugin_id: str) -> BasePlugin | None:
        """获取已加载的插件实例。"""
        entry = self._loaded.get(plugin_id)
        return entry[0] if entry else None

    def is_activated(self, plugin_id: str) -> bool:
        """检查插件是否已激活。"""
        return plugin_id in self._activated

    def list_plugins(self) -> list[dict[str, Any]]:
        """列出所有已加载插件的状态。"""
        result: list[dict[str, Any]] = []
        for plugin_id, (_plugin, manifest, _ctx) in self._loaded.items():
            result.append(
                {
                    "id": plugin_id,
                    "name": manifest.name,
                    "version": manifest.version,
                    "type": manifest.type,
                    "activated": plugin_id in self._activated,
                    "core": manifest.core,
                }
            )
        return result

    # ── 一键加载 ──────────────────────────────────────

    async def load_and_activate_all(
        self, plugins_dir: str | Path
    ) -> list[str]:
        """发现并加载激活所有插件，返回成功激活的 plugin_id 列表。"""
        manifest_paths = self.discover(plugins_dir)
        activated: list[str] = []

        for path in manifest_paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                manifest = self.validate_manifest(raw)
                self.load(manifest, plugins_dir)
                await self.activate(manifest.id)
                activated.append(manifest.id)
            except Exception as e:
                logger.error("加载插件失败 (%s): %s", path, e)

        return activated
