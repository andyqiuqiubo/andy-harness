"""插件开发者工作台（DevKit）—— 脚手架 + 热重载（P2）。

脚手架：按模板渲染出可直接加载的插件目录（plugin.json + main.py），
manifest 带 ``config_schema``（DevKit 页据此自动渲染配置表单）。

热重载：停用 → 卸载 → 清除 sys.modules 缓存 → 重新加载并激活。
只允许非核心插件；重载失败时返回完整错误（代码问题不会导致服务崩溃）。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from harness.kernel.loader import PluginLoader

logger = logging.getLogger("harness.devkit")

_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,48}$")

# 模板占位符：__PLUGIN_ID__ / __CLASS_NAME__ / __PLUGIN_CLASS__ / __PLUGIN_NAME__ / __PLUGIN_DESC__

_TOOL_MAIN = '''"""__PLUGIN_DESC__

由 DevKit 脚手架生成：一个最小可用的 ToolPlugin 插件包。
"""

from __future__ import annotations

from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin


class __CLASS_NAME__(ToolPlugin):
    @property
    def tool_name(self) -> str:
        return "__PLUGIN_ID__"

    @property
    def description(self) -> str:
        return "__PLUGIN_NAME__：把这里的描述换成你的工具说明（Agent 依据它决定何时调用）。"

    @property
    def risk_level(self) -> str:
        return "read"  # read / write / dangerous，权限层按此管控

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "输入文本"},
            },
            "required": ["text"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        text = str(args.get("text", ""))
        return f"echo: {text}"


class __PLUGIN_CLASS__(BasePlugin):
    """插件入口：把工具注册进 ToolRegistry。"""

    manifest: PluginManifest

    async def activate(self, ctx: PluginContext) -> None:
        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        ctx.services.get(ToolRegistry).register(__CLASS_NAME__(), owner=self.plugin_id)
        ctx.logger.info("__PLUGIN_NAME__ 已激活")

    async def deactivate(self, ctx: PluginContext) -> None:
        ctx.logger.info("__PLUGIN_NAME__ 已停用")
'''

_PROVIDER_MAIN = '''"""__PLUGIN_DESC__

由 DevKit 脚手架生成：OpenAI 兼容 Provider 插件包。
"""

from __future__ import annotations

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.modules.model_manager.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderError,
)
from harness.modules.model_manager.provider_registry import ProviderRegistry


class __CLASS_NAME__(OpenAICompatibleProvider):
    base_url = "https://api.example.com/v1"  # 改成你的端点
    default_models = ["example-model"]
    provider_name = "__PLUGIN_ID__"
    supports_reasoning_content = False  # DeepSeek 类推理模型才置 True


class __PLUGIN_CLASS__(BasePlugin):
    manifest: PluginManifest

    async def activate(self, ctx: PluginContext) -> None:
        if not ctx.services.has(ProviderRegistry):
            ctx.services.register(ProviderRegistry, ProviderRegistry(), owner=self.plugin_id)
        registry = ctx.services.get(ProviderRegistry)
        registry.register_provider(
            "__PLUGIN_ID__",
            __CLASS_NAME__,
            {
                "name": "__PLUGIN_NAME__",
                "api_key": ctx.config.get("api_key", ""),
                "base_url": ctx.config.get("base_url", __CLASS_NAME__.base_url),
                "models": __CLASS_NAME__.default_models,
            },
        )
        ctx.logger.info("__PLUGIN_NAME__ 已激活")

    async def deactivate(self, ctx: PluginContext) -> None:
        from harness.modules.model_manager.provider_registry import ProviderRegistry

        if ctx.services.has(ProviderRegistry):
            try:
                ctx.services.get(ProviderRegistry).unregister_provider("__PLUGIN_ID__")
            except ProviderError:
                pass
'''

_INTEGRATION_MAIN = '''"""__PLUGIN_DESC__

由 DevKit 脚手架生成：第三方系统集成（IntegrationProvider）插件包。
"""

from __future__ import annotations

from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from plugins.integration_hub.integration_base import (
    ActionSpec,
    AuthResult,
    IntegrationProvider,
    IntegrationResult,
    get_registry,
)


class __CLASS_NAME__(IntegrationProvider):
    provider_id = "__PLUGIN_ID__"
    provider_name = "__PLUGIN_NAME__"
    supported_modes = ["api"]

    def list_actions(self) -> list[ActionSpec]:
        return [
            ActionSpec(
                name="ping",
                description="示例动作：返回问候语",
                parameters_schema={"type": "object", "properties": {}},
            ),
        ]

    async def authenticate(self, mode: str, credentials: dict[str, Any]) -> AuthResult:
        return AuthResult(ok=True, mode=mode, detail="示例实现：无需凭证")

    def is_authenticated(self) -> bool:
        return True

    async def call(self, action: str, params: dict[str, Any]) -> IntegrationResult:
        if action == "ping":
            return IntegrationResult(ok=True, data={"message": "pong"})
        return IntegrationResult(ok=False, error=f"不支持的动作: {action}")


class __PLUGIN_CLASS__(BasePlugin):
    manifest: PluginManifest

    async def activate(self, ctx: PluginContext) -> None:
        registry = get_registry()
        if registry.get("__PLUGIN_ID__") is None:
            registry.register(__CLASS_NAME__())
        ctx.logger.info("__PLUGIN_NAME__ 已激活")

    async def deactivate(self, ctx: PluginContext) -> None:
        ctx.logger.info("__PLUGIN_NAME__ 已停用")
'''

_HOOK_MAIN = '''"""__PLUGIN_DESC__

由 DevKit 脚手架生成：Hook 插件包（pre_model_call 示例：注入一句系统提示）。
"""

from __future__ import annotations

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.engine.hook_types import ModelRequest
from harness.kernel.hooks import HookManager


class __PLUGIN_CLASS__(BasePlugin):
    manifest: PluginManifest

    async def activate(self, ctx: PluginContext) -> None:
        async def pre_model_call(hctx) -> None:
            from harness.kernel.contracts.hook import HookResult

            if isinstance(hctx.data, ModelRequest):
                # 示例：在最前面追加一条系统提示（可按需改写 messages/model/params）
                hctx.data.messages = [
                    {"role": "system", "content": "__PLUGIN_NAME__ 注入的提示"},
                    *hctx.data.messages,
                ]
                return HookResult(data=hctx.data)
            return HookResult(data=hctx.data)

        ctx.hooks.register("pre_model_call", pre_model_call, owner=self.plugin_id)
        ctx.logger.info("__PLUGIN_NAME__ 已激活（pre_model_call 钩子已注册）")

    async def deactivate(self, ctx: PluginContext) -> None:
        ctx.logger.info("__PLUGIN_NAME__ 已停用")
'''

_TEMPLATES: dict[str, dict[str, str]] = {
    "tool": {"main": _TOOL_MAIN, "type": "tool"},
    "provider": {"main": _PROVIDER_MAIN, "type": "provider"},
    "integration": {"main": _INTEGRATION_MAIN, "type": "integration"},
    "hook": {"main": _HOOK_MAIN, "type": "hook"},
}

_LABELS = {
    "tool": "工具插件",
    "provider": "模型 Provider",
    "integration": "第三方集成",
    "hook": "钩子插件",
}


def list_templates() -> list[dict[str, str]]:
    """列出可用脚手架模板。"""
    return [{"key": key, "type": cfg["type"], "label": _LABELS[key]} for key, cfg in _TEMPLATES.items()]


def _pascal(rest: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[_\-]+", rest) if part)


def _derive_class(id: str, template: str) -> tuple[str, str]:
    """从插件 id 推导 (工具类名, 插件入口类名)：tool_greeter → (GreeterToolPlugin, GreeterPlugin)。"""
    rest = id
    for p in ("tool_", "provider_", "integration_", "channel_", "hook_"):
        if id.startswith(p):
            rest = id[len(p) :]
            break
    rest = _pascal(rest or "My")
    tool_suffix = {
        "tool": "ToolPlugin",
        "provider": "ProviderPlugin",
        "integration": "IntegrationPlugin",
        "hook": "HookPlugin",
    }[template]
    return f"{rest}{tool_suffix}", f"{rest}Plugin"


def scaffold(
    plugins_dir: str | Path,
    template: str,
    plugin_id: str,
    name: str,
    description: str = "",
) -> dict[str, Any]:
    """按模板生成插件目录。返回 {dir, files}。"""
    if template not in _TEMPLATES:
        raise ValueError(f"未知模板: {template}，可选 {list(_TEMPLATES)}")
    if not _PLUGIN_ID_RE.match(plugin_id):
        raise ValueError("插件 id 需以小写字母开头，仅含小写字母/数字/下划线（3-49 位）")
    if not name.strip():
        raise ValueError("插件名称不能为空")
    cfg = _TEMPLATES[template]
    class_name, plugin_class = _derive_class(plugin_id, template)

    pdir = Path(plugins_dir) / plugin_id
    if pdir.exists():
        raise ValueError(f"插件目录已存在: {pdir}")

    tokens = {
        "__PLUGIN_ID__": plugin_id,
        "__CLASS_NAME__": class_name,
        "__PLUGIN_CLASS__": plugin_class,
        "__PLUGIN_NAME__": name,
        "__PLUGIN_DESC__": description or name,
    }
    main_src = cfg["main"]
    for k, v in tokens.items():
        main_src = main_src.replace(k, v)

    manifest = {
        "id": plugin_id,
        "name": name,
        "version": "0.1.0",
        "type": cfg["type"],
        "entry": f"plugins.{plugin_id}.main:{plugin_class}",
        "core_api": ">=0.1.0 <1.0.0",
        "description": description or name,
        "config_schema": {
            "type": "object",
            "properties": {
                "greeting": {"type": "string", "default": "hello", "description": "示例配置项"},
            },
        },
    }
    pdir.mkdir(parents=True)
    files = {
        "__init__.py": "",
        "main.py": main_src,
        "plugin.json": json.dumps(manifest, ensure_ascii=False, indent=2),
    }
    for fname, content in files.items():
        (pdir / fname).write_text(content, encoding="utf-8", newline="\n")
    logger.info("DevKit 脚手架已生成: %s (%s)", plugin_id, template)
    return {"dir": str(pdir), "files": list(files)}


async def reload_plugin(loader: PluginLoader, plugins_dir: str | Path, plugin_id: str) -> dict[str, Any]:
    """热重载插件：停用 → 卸载 → 清 sys.modules 缓存 → 重新加载 → 激活。"""
    pdir = Path(plugins_dir) / plugin_id
    manifest_path = pdir / "plugin.json"
    if not manifest_path.is_file():
        raise ValueError(f"插件目录或 plugin.json 不存在: {manifest_path}")

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    # 核心插件常驻且跨生命周期，不支持热重载（停用会破坏系统能力）
    if raw.get("core"):
        raise ValueError(f"核心插件不支持热重载: {plugin_id}")

    if loader.is_activated(plugin_id):
        await loader.deactivate(plugin_id)
    try:
        loader.unload(plugin_id)
    except Exception:  # noqa: BLE001 - 从未加载过时 unload 会抛，忽略
        pass

    module_path = str(raw.get("entry", "")).split(":")[0]
    # 清除模块缓存，确保 import_module 拿到最新代码
    for name in [m for m in list(sys.modules) if m == module_path or m.startswith(module_path + ".")]:
        sys.modules.pop(name, None)

    manifest = loader.validate_manifest(raw)
    loader.load(manifest, plugins_dir)
    await loader.activate(plugin_id)
    return {"ok": True, "plugin_id": plugin_id, "status": "activated"}
