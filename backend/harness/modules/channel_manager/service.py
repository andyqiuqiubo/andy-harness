"""ChannelManager —— 多渠道接入服务。

把外部渠道（Webhook / Telegram / 飞书 等）的入站消息统一接入 AgentLoop：

    外部消息 → ChannelManager.process
             → 按 (channel, user) 映射 / 复用会话（channel_links 表）
             → AgentLoop 跑完整工具循环
             → 终答经渠道回发（或由 Webhook 同步返回）

渠道本身只实现「如何收发」（start/stop/send），会话与 Agent 逻辑全部由
ChannelManager 共享，避免每个渠道各写一套对话流程。
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, cast

from harness.engine.agent_loop import AgentLoop, AgentLoopConfig
from harness.engine.tool_registry import ToolRegistry
from harness.infra.database import Database
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry

logger = logging.getLogger("harness.channel")


@dataclass
class InboundMessage:
    """入站消息（渠道无关的统一表示）。"""

    channel: str
    user_id: str
    text: str
    # 回发目标（如 Telegram 的 chat_id）；为空时回发到 user_id
    reply_to: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelResult:
    """一次渠道消息处理的结果。"""

    reply: str
    session_id: str
    error: str | None = None


class Channel(ABC):
    """渠道抽象：只负责「如何收发」。"""

    #: 渠道名称（唯一标识）
    name: str

    @abstractmethod
    async def start(self) -> None:
        """启动渠道（如开启轮询协程）。"""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止渠道。"""
        ...

    @abstractmethod
    async def send(self, target: str, text: str) -> None:
        """向目标用户回发消息。"""
        ...

    @property
    def running(self) -> bool:
        return False

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "running": self.running}


class WebhookChannel(Channel):
    """Webhook 渠道：外部系统 POST 入站，终答由 HTTP 响应同步返回。

    可选：
    - secret：调用方须携带 X-Channel-Secret 或 ?secret= 一致才受理（防端点被滥用）；
    - allowed_users：非空时仅名单内的 user_id 可调用。
    """

    def __init__(
        self,
        name: str = "webhook",
        secret: str = "",
        allowed_users: list[str] | None = None,
    ) -> None:
        self.name = name
        self.secret = secret
        self.allowed_users: list[str] = list(allowed_users or [])
        self._running = False

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, target: str, text: str) -> None:
        # Webhook 为同步应答（见 REST 路由），无需主动回发。
        return None

    @property
    def running(self) -> bool:
        return self._running

    def check_secret(self, provided: str) -> bool:
        """校验调用方密钥；未配置 secret 时放行。"""
        return not self.secret or provided == self.secret


@dataclass
class ChannelManagerConfig:
    """ChannelManager 配置。"""

    provider_id: str = ""
    model: str = ""
    budget: int = 4096
    system_prompt: str = ""
    max_tool_iterations: int = 10


class ChannelManager:
    """渠道管理器：注册渠道、统一跑 AgentLoop、维护会话映射。"""

    def __init__(
        self,
        services: ServiceRegistry,
        hooks: HookManager | None = None,
        tool_registry: ToolRegistry | None = None,
        config: ChannelManagerConfig | None = None,
    ) -> None:
        self._services = services
        self._hooks = hooks
        self._tool_registry = tool_registry
        self._config = config or ChannelManagerConfig()
        self._channels: dict[str, Channel] = {}

    # ── 渠道注册 ──────────────────────────────────────

    def register(self, channel: Channel) -> None:
        self._channels[channel.name] = channel
        logger.info("渠道已注册: %s", channel.name)

    def get(self, name: str) -> Channel | None:
        return self._channels.get(name)

    async def unregister(self, name: str) -> None:
        """注销渠道：先停止再移除。"""
        channel = self._channels.pop(name, None)
        if channel is not None:
            try:
                await channel.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning("注销渠道 %s 时停止失败: %s", name, e)
            logger.info("渠道已注销: %s", name)

    def list_channels(self) -> list[dict[str, Any]]:
        return [ch.status() for ch in self._channels.values()]

    async def start_all(self) -> None:
        for ch in self._channels.values():
            await ch.start()
        logger.info("已启动 %d 个渠道", len(self._channels))

    async def stop_all(self) -> None:
        for ch in self._channels.values():
            try:
                await ch.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning("停止渠道 %s 失败: %s", ch.name, e)

    # ── 核心处理 ──────────────────────────────────────

    async def process(self, msg: InboundMessage) -> ChannelResult:
        """处理一条入站消息，返回终答。"""
        channel = self._channels.get(msg.channel)
        if channel is None:
            raise ChannelError("CHANNEL_NOT_FOUND", f"渠道不存在: {msg.channel}")
        if not msg.text or not msg.text.strip():
            raise ChannelError("EMPTY_MESSAGE", "消息内容为空")

        if isinstance(channel, WebhookChannel) and channel.allowed_users:
            if msg.user_id not in channel.allowed_users:
                raise ChannelError("USER_NOT_ALLOWED", f"用户不在允许名单: {msg.user_id}")

        # 先解析 provider：无可用模型时快速失败，避免无谓建会话
        provider = self._get_provider()
        model = self._config.model or None

        # E12：渠道会话归属系统用户（启用认证时）；未启用则为 ""。
        owner_id = self._resolve_owner()

        session_id = await self._resolve_session(channel, msg.user_id, owner_id)
        loop_config = AgentLoopConfig(
            model=self._config.model or "gpt-4o",
            budget=self._config.budget,
            system_prompt=self._config.system_prompt,
            max_tool_iterations=self._config.max_tool_iterations,
        )
        loop = AgentLoop(
            services=self._services,
            hooks=self._require_hooks(),
            tool_registry=self._require_tool_registry(),
            config=loop_config,
        )

        logger.info(
            "渠道消息开始: channel=%s user=%s session=%s",
            msg.channel,
            msg.user_id,
            session_id,
        )
        result = await loop.run(
            session_id=session_id,
            user_message=msg.text,
            provider=provider,
            model=model,
            budget=self._config.budget,
            user_id=owner_id,
        )

        reply = result.content or ""
        if result.error and not reply:
            reply = f"处理失败：{result.error}"
        if result.error:
            logger.warning(
                "渠道消息处理有错误: channel=%s error=%s",
                msg.channel,
                result.error,
            )

        if reply:
            await channel.send(msg.reply_to or msg.user_id, reply)
        return ChannelResult(reply=reply, session_id=session_id, error=result.error)

    # ── 会话映射 ──────────────────────────────────────

    async def _resolve_session(self, channel: Channel, user_id: str, owner_id: str = "") -> str:
        """按 (channel, user) 取稳定会话；缺失则新建并记录映射。"""
        db = self._services.get(Database)
        row = db.query_one(
            "SELECT session_id FROM channel_links WHERE channel=? AND external_user=?",
            (channel.name, user_id),
        )
        if row is not None:
            return cast("str", row["session_id"])

        from harness.modules.session_manager.service import SessionService

        session_service = cast("SessionService", self._services.get(SessionService))
        title = f"{channel.name} · {user_id}"
        session = session_service.create_session(title=title, user_id=owner_id)
        db.execute(
            "INSERT INTO channel_links (channel, external_user, session_id) VALUES (?, ?, ?)",
            (channel.name, user_id, session.id),
        )
        return session.id

    def _resolve_owner(self) -> str:
        """E12：渠道会话归属用户。

        认证启用时取默认系统用户（HARNESS_CHANNEL_USERNAME，默认首位 admin）；
        未注册/未启用时返回 ""，行为与旧版一致。
        """
        try:
            from harness.modules.auth_manager.service import AuthService

            if not self._services.has(AuthService):
                return ""
            auth = self._services.get(AuthService)
            if not auth.enabled:
                return ""
            return str(auth.default_user_id())
        except Exception as e:  # noqa: BLE001
            logger.debug("解析渠道归属用户失败（已忽略）: %s", e)
            return ""

    # ── 依赖解析 ──────────────────────────────────────

    def _get_provider(self) -> Any:
        from harness.modules.model_manager.provider_registry import ProviderRegistry

        registry = self._services.get(ProviderRegistry)
        provider_id = self._config.provider_id or os.environ.get("HARNESS_CHANNEL_PROVIDER", "")
        if provider_id:
            return registry.get_provider(provider_id)
        providers = registry.list_providers()
        usable = [p for p in providers if p.get("enabled") and p.get("has_api_key")]
        if not usable:
            raise ChannelError("NO_PROVIDER", "未配置任何可用的模型 provider")
        return registry.get_provider(str(usable[0]["id"]))

    def _require_hooks(self) -> HookManager:
        if self._hooks is not None:
            return self._hooks
        return cast("HookManager", self._services.get(HookManager))

    def _require_tool_registry(self) -> ToolRegistry:
        if self._tool_registry is not None:
            return self._tool_registry
        return cast("ToolRegistry", self._services.get(ToolRegistry))

    def configure_from_env(self) -> None:
        """从环境变量补齐配置（provider / model / 系统提示）。"""
        if not self._config.provider_id:
            self._config.provider_id = os.environ.get("HARNESS_CHANNEL_PROVIDER", "")
        if not self._config.model:
            self._config.model = os.environ.get("HARNESS_CHANNEL_MODEL", "")


class ChannelError(Exception):
    """渠道处理错误（带错误码，供 REST 映射）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _json_dumps(data: Any) -> str:
    """统一 JSON 序列化（供渠道工具复用）。"""
    return json.dumps(data, ensure_ascii=False)
