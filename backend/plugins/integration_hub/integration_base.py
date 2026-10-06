"""第三方系统集成 —— 统一接口与注册表。

为可扩展的第三方系统对接提供抽象基类 ``IntegrationProvider``，
以及管理提供方的 ``IntegrationRegistry``。后续接入 Slack / GitHub 等
系统，只需实现 ``IntegrationProvider`` 并在插件 activate 时注册即可。
"""

from __future__ import annotations

import abc
import builtins
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActionSpec:
    """集成动作说明（供 Agent 工具生成参数 schema 与描述）。"""

    name: str
    description: str
    parameters_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class IntegrationResult:
    """动作调用结果。"""

    ok: bool
    data: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True, "data": self.data}
        return {"ok": False, "error": self.error}


@dataclass
class AuthResult:
    """认证授权结果。"""

    ok: bool
    mode: str = ""
    detail: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True, "mode": self.mode, "detail": self.detail}
        return {"ok": False, "error": self.error}


class IntegrationProvider(abc.ABC):
    """第三方系统集成提供方统一接口。

    新增一个第三方系统（如 Slack / GitHub）只需继承本类，实现以下方法，
    并在插件 ``activate`` 时通过 ``IntegrationRegistry.register`` 注册。
    """

    #: 提供方唯一标识，如 ``"feishu"``
    provider_id: str = ""
    #: 提供方展示名
    provider_name: str = ""
    #: 支持的接入方式，如 ``["cli", "channel", "api"]``
    supported_modes: list[str] = []
    #: CLI 模式的一键安装命令（若支持 CLI）。空字符串表示无。
    cli_install_command: str = ""
    #: 入站渠道（让 Agent 在第三方里被对话调用）的能力描述列表。
    #: 每项形如 ``{"name": "群聊", "description": "..."}``；空列表表示不支持渠道。
    channel_capabilities: list[dict[str, str]] = []

    @abc.abstractmethod
    def list_actions(self) -> list[ActionSpec]:
        """列出本提供方支持的动作。"""
        ...

    @abc.abstractmethod
    async def authenticate(self, mode: str, credentials: dict[str, Any]) -> AuthResult:
        """按指定接入方式完成认证授权。

        Args:
            mode: 接入方式，必须是 ``supported_modes`` 之一（如 ``api`` / ``cli``）。
            credentials: 该方式所需的凭证（如 API 的 app_id/app_secret）。
        """
        ...

    @abc.abstractmethod
    def is_authenticated(self) -> bool:
        """是否已认证（token 有效 / CLI 已登录）。"""
        ...

    @abc.abstractmethod
    async def call(self, action: str, params: dict[str, Any]) -> IntegrationResult:
        """调用一个动作。"""
        ...

    def auth_instructions(self, mode: str) -> str:
        """返回指定接入方式的人工授权指引（展示给用户 / Agent）。

        默认返回通用提示；具体提供方应覆盖以给出精确的安装与授权命令。
        """
        if mode == "cli":
            cmd = self.cli_install_command or "请安装官方命令行工具"
            return f"CLI 模式：先执行「{cmd}」安装，再执行对应登录命令完成授权。"
        if mode == "api":
            return "API 模式：在开放平台创建应用，填入 app_id 与 app_secret。"
        return f"未知接入方式：{mode}"

    def cli_available(self) -> bool | None:
        """本机是否已具备 CLI 模式所需二进制。

        ``None`` 表示该提供方不涉及 CLI 模式（或无法判断）；
        支持_cli 的提供方应覆盖本方法做真实检测（如 ``shutil.which``），
        供前端在 CLI 页签上直接提示「未安装」，避免用户填完表单才报错。
        """
        return None

    def cli_path(self) -> str | None:
        """本机 CLI 二进制的绝对路径（若可检测到）。

        与 ``cli_available()`` 配套：前者只给布尔值，这里给出具体路径，
        便于前端展示「已检测到：<绝对路径>」，也让用户能手动指定路径。
        默认 ``None``（不展示）。
        """
        return None

    def disconnect(self) -> None:
        """断开并清除本地认证状态（默认无操作，子类按需覆盖）。

        例如飞书会在实现中清除 tenant_access_token 与 CLI 记录，
        并删除本地状态文件，使下次必须重新授权。
        """
        return None

    # ── 入站渠道（让 Agent 在第三方里被对话调用） ──────────
    def channel_status(self) -> dict[str, Any]:
        """返回入站渠道运行状态（默认未启用）。"""
        return {"running": False, "detail": "该提供方不支持入站渠道"}

    async def start_channel(self, credentials: dict[str, Any]) -> AuthResult:
        """启动入站渠道（默认不支持）。"""
        return AuthResult(ok=False, error="该提供方不支持入站渠道（channel）模式")

    async def stop_channel(self) -> AuthResult:
        """停止入站渠道（默认无操作）。"""
        return AuthResult(ok=True, detail="无运行中的渠道")

    def describe(self) -> dict[str, Any]:
        """生成提供给 Agent 的元信息（无需子类覆盖）。"""
        desc: dict[str, Any] = {
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "supported_modes": list(self.supported_modes),
            "cli_install_command": self.cli_install_command,
            "channel_capabilities": list(self.channel_capabilities),
            "auth_instructions": {m: self.auth_instructions(m) for m in self.supported_modes},
            "authenticated": self.is_authenticated(),
            "channel_running": self.channel_status().get("running", False),
            "actions": [
                {
                    "name": a.name,
                    "description": a.description,
                    "parameters_schema": a.parameters_schema,
                }
                for a in self.list_actions()
            ],
        }
        cli_ok = self.cli_available()
        if cli_ok is not None:
            desc["cli_available"] = cli_ok
            desc["cli_path"] = self.cli_path()
        return desc


class IntegrationRegistry:
    """集成提供方注册表（单例）。"""

    def __init__(self) -> None:
        self._providers: dict[str, IntegrationProvider] = {}

    def register(self, provider: IntegrationProvider) -> None:
        """注册一个提供方。"""
        if not provider.provider_id:
            raise ValueError("provider_id 不能为空")
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> IntegrationProvider | None:
        """按 id 获取提供方。"""
        return self._providers.get(provider_id)

    def list(self) -> builtins.list[IntegrationProvider]:
        """列出所有已注册提供方。"""
        return list(self._providers.values())

    def list_descriptions(self) -> builtins.list[dict[str, Any]]:
        """生成所有提供方的描述列表（供 Agent 工具使用）。"""
        return [p.describe() for p in self._providers.values()]


_REGISTRY: IntegrationRegistry | None = None


def get_registry() -> IntegrationRegistry:
    """获取全局集成注册表单例。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = IntegrationRegistry()
    return _REGISTRY
