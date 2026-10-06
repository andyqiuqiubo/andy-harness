"""第三方系统集成插件。

提供可扩展的第三方系统对接能力，当前内置飞书（Feishu/Lark）集成。
基于飞书官方文档支持三种接入方式：

- ``cli``：飞书官方命令行 ``lark-cli``，Agent 操作飞书（出方向，主路径）
- ``channel``：基于 Channel SDK，让 Agent 在飞书里被对话调用（入方向）
- ``api``：无 CLI 环境的兜底，直接用 app_id/app_secret 调少量开放接口

暴露给 Agent 的工具：
- ``integration_list``：列出已注册的提供方及其动作
- ``integration_connect``：按 cli/api 方式完成某提供方的认证授权
- ``integration_call``：调用某提供方的一个动作
- ``integration_channel``：启动/停止/查询飞书入站渠道（channel 模式）

新增其他系统（如 Slack / GitHub）只需在 ``activate`` 中注册一个新的
``IntegrationProvider`` 实现，无需改动工具层。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

from .feishu_provider import FeishuIntegration
from .integration_base import get_registry

logger = logging.getLogger("harness.plugin.integration_hub")


def _json(obj: Any) -> str:
    """将结果对象序列化为可读 JSON 字符串（供 Agent 工具返回）。"""
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


async def _resolve_channel_provider_and_model(services: Any) -> tuple[Any, str | None]:
    """解析飞书渠道应使用的模型 provider 与模型名。

    优先用系统设置里的 default_model 定位其所属 provider；都取不到时
    回落到第一个启用的 provider 的首个模型。

    Returns:
        (provider, model)；provider 为 None 表示当前无可用的模型 provider。
    """
    from harness.modules.model_manager.provider_registry import ProviderRegistry

    prov_reg = services.get(ProviderRegistry)
    infos = [i for i in prov_reg.list_providers() if i.get("enabled", True)]
    if not infos:
        return None, None
    used_model: str | None = None
    try:
        from harness.api.rest.settings import _load_settings

        used_model = _load_settings().get("default_model") or None
    except Exception:  # noqa: BLE001 - 设置文件缺失/损坏时用默认
        used_model = None
    if used_model:
        for info in infos:
            if used_model in (info.get("models") or []):
                return prov_reg.get_provider(info["id"]), used_model
    # 未命中：回落到第一个启用的 provider，并优先用**它自己的**默认模型
    # （参照 ws/chat.py 的 P2-9：避免把别的厂商模型名硬塞给当前 provider）
    first = infos[0]
    models = first.get("models") or []
    if used_model and used_model in models:
        return prov_reg.get_provider(first["id"]), used_model
    fallback_model = models[0] if models else used_model
    return prov_reg.get_provider(first["id"]), fallback_model


class IntegrationListTool(ToolPlugin):
    """列出所有已接入的第三方系统及其能力。"""

    @property
    def tool_name(self) -> str:
        return "integration_list"

    @property
    def description(self) -> str:
        return (
            "列出所有已接入的第三方系统（如飞书）及其支持的接入方式与可调用动作。"
            "飞书支持：cli（推荐，lark-cli 操作飞书全部业务对象）、"
            "channel（在飞书里对话调用 Agent）、api（无 CLI 时的兜底）。"
            "在连接或调用前先调用本工具了解能力。"
        )

    @property
    def risk_level(self) -> str:
        return "read"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, args: dict[str, Any]) -> str:
        return _json(get_registry().list_descriptions())


class IntegrationConnectTool(ToolPlugin):
    """连接并授权一个第三方系统。"""

    @property
    def tool_name(self) -> str:
        return "integration_connect"

    @property
    def description(self) -> str:
        return (
            "连接并授权一个第三方系统。例如飞书：\n"
            "- mode='cli'：本机需已安装 lark-cli（npx @larksuite/cli@latest install）\n"
            "- mode='api'：需提供 app_id 与 app_secret（无 CLI 环境的兜底）\n"
            "授权状态会被持久化。channel（入站）请用 integration_channel 工具。"
        )

    @property
    def risk_level(self) -> str:
        return "write"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "description": "提供方标识，如 feishu"},
                "mode": {
                    "type": "string",
                    "description": "接入方式：cli / api（飞书），channel 请用 integration_channel",
                },
                "credentials": {
                    "type": "object",
                    "description": "认证所需凭证（如 {'app_id':..., 'app_secret':...}）",
                    "default": {},
                },
            },
            "required": ["provider", "mode"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        provider = get_registry().get(args.get("provider", ""))
        if not provider:
            return _json({"ok": False, "error": f"未找到提供方: {args.get('provider')}"})
        result = await provider.authenticate(args.get("mode", ""), args.get("credentials") or {})
        return _json(result.to_dict())


class IntegrationCallTool(ToolPlugin):
    """调用已连接第三方系统的一个动作。"""

    @property
    def tool_name(self) -> str:
        return "integration_call"

    @property
    def description(self) -> str:
        return (
            "调用已连接第三方系统的一个动作。飞书 CLI 模式常用动作：\n"
            "- create_doc{title,content?,identity?,doc_format?}：以用户身份在云文档创建文档并写入正文\n"
            "- send_message{chat_id,text}：通过 CLI 发消息到会话\n"
            "- whoami / auth_status / doctor：查看当前身份与授权健康状态（排查鉴权问题优先用）\n"
            "- skills{name?} / schema{method}：查 CLI 内置说明书与开放 API 参数（执行 raw 前先查）\n"
            "- raw{args:[...]}：直接透传 lark-cli 参数列表，解锁完整能力地图\n"
            "API 模式动作：create_doc{title,content?,space_id?} / write_doc / send_message / "
            "send_webhook / get_bot_info / get_user。"
        )

    @property
    def risk_level(self) -> str:
        return "write"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "description": "提供方标识，如 feishu"},
                "action": {
                    "type": "string",
                    "description": "动作名，如 send_message / get_bot_info / get_user / raw",
                },
                "params": {"type": "object", "description": "动作参数", "default": {}},
            },
            "required": ["provider", "action"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        provider = get_registry().get(args.get("provider", ""))
        if not provider:
            return _json({"ok": False, "error": f"未找到提供方: {args.get('provider')}"})
        result = await provider.call(args.get("action", ""), args.get("params") or {})
        return _json(result.to_dict())


class IntegrationChannelTool(ToolPlugin):
    """启动 / 停止 / 查询飞书入站渠道（让 Agent 在飞书里被对话调用）。"""

    @property
    def tool_name(self) -> str:
        return "integration_channel"

    @property
    def description(self) -> str:
        return (
            "管理飞书入站渠道（Channel 模式）：让 Agent 在飞书群聊/单聊/云文档评论里被对话调用。"
            "action='start' 需提供 {'app_id','app_secret'}，采用 WebSocket 长连（无需公网回调）；"
            "action='stop' 停止；action='status' 查询运行状态。"
        )

    @property
    def risk_level(self) -> str:
        return "write"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "description": "提供方标识，如 feishu"},
                "action": {
                    "type": "string",
                    "description": "操作：start / stop / status",
                    "enum": ["start", "stop", "status"],
                },
                "credentials": {
                    "type": "object",
                    "description": "start 时所需凭证 {'app_id','app_secret'}",
                    "default": {},
                },
            },
            "required": ["provider", "action"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        provider = get_registry().get(args.get("provider", ""))
        if not provider:
            return _json({"ok": False, "error": f"未找到提供方: {args.get('provider')}"})
        action = args.get("action", "status")
        if action == "start":
            result = await provider.start_channel(args.get("credentials") or {})
            return _json(result.to_dict())
        if action == "stop":
            result = await provider.stop_channel()
            return _json(result.to_dict())
        return _json({"ok": True, **provider.channel_status()})


class IntegrationHubPlugin(BasePlugin):
    """第三方系统集成插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    def _build_channel_handler(self, ctx: PluginContext) -> Any:
        """构造入站消息处理器：把飞书消息喂给 Agent 循环并流式回写。

        防御式实现：找不到 Agent 运行环境时返回友好提示，不阻断插件加载。
        """

        async def handler(text: str, msg_ctx: dict[str, Any]) -> AsyncIterator[str]:
            reply_buffer: list[str] = []
            try:
                from harness.engine.agent_loop import AgentLoop, AgentLoopConfig
                from harness.engine.tool_registry import ToolRegistry
                from harness.kernel.hooks import HookManager

                services = ctx.services
                provider, used_model = await _resolve_channel_provider_and_model(services)

                if provider is None:
                    yield "⚠️ 未配置模型 Provider，无法在飞书中应答。"
                    return

                session_id = f"feishu-channel-{msg_ctx.get('chat_id') or 'default'}"
                tool_registry = None
                try:
                    tool_registry = services.get(ToolRegistry)
                except Exception:  # noqa: BLE001
                    tool_registry = None
                try:
                    hooks = services.get(HookManager)
                except Exception:  # noqa: BLE001 - 未注册时构造空钩子管线
                    hooks = HookManager()

                loop = AgentLoop(
                    services=services,
                    hooks=hooks,
                    tool_registry=tool_registry,
                    config=AgentLoopConfig(model=used_model or AgentLoopConfig.model),
                )
                result = await loop.run(session_id, text, provider=provider)
                content = result.content or "（无内容）"
                reply_buffer.append(content)
                yield content
            except Exception as e:  # noqa: BLE001
                logger.error("飞书渠道 Agent 处理异常: %s", e)
                reply_buffer.append(f"⚠️ Agent 处理失败: {e}")
                yield reply_buffer[-1]

            # 通过飞书应用消息 API 把完整回复回写到原会话（@ 机器人所在群/单聊）
            try:
                from .feishu_provider import FeishuIntegration
                from .integration_base import get_registry

                feishu = get_registry().get("feishu")
                message_id = msg_ctx.get("message_id")
                if isinstance(feishu, FeishuIntegration) and message_id:
                    full = "".join(reply_buffer)
                    await feishu.reply_message(message_id, full)
            except Exception as e:  # noqa: BLE001
                logger.warning("飞书渠道回复消息失败（流式已返回，群内回复跳过）: %s", e)

        return handler

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx

        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        tool_registry = ctx.services.get(ToolRegistry)

        # 注册提供方到统一注册表（飞书内置；后续系统在此追加）
        registry = get_registry()
        feishu: FeishuIntegration = registry.get("feishu")  # type: ignore[assignment]
        if feishu is None:
            feishu = FeishuIntegration()
            registry.register(feishu)

        # 注入入站消息处理器（channel 模式需要）
        feishu.set_message_handler(self._build_channel_handler(ctx))

        tool_registry.register(IntegrationListTool(), owner=self.plugin_id)
        tool_registry.register(IntegrationConnectTool(), owner=self.plugin_id)
        tool_registry.register(IntegrationCallTool(), owner=self.plugin_id)
        tool_registry.register(IntegrationChannelTool(), owner=self.plugin_id)
        ctx.logger.info("第三方系统集成插件已激活（已注册飞书：cli/channel/api）")

    async def deactivate(self, ctx: PluginContext) -> None:
        ctx.logger.info("第三方系统集成插件已停用")
