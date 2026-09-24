"""插件基础契约 —— PluginType / PluginManifest / BasePlugin。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from harness.kernel.context import PluginContext


class PluginType(StrEnum):
    """插件类型枚举。"""

    PROVIDER = "provider"
    TOOL = "tool"
    SERVICE = "service"
    HOOK = "hook"
    CHANNEL = "channel"


@dataclass
class PluginManifest:
    """插件清单（从 plugin.json 解析）。"""

    id: str
    name: str
    version: str
    type: str
    entry: str  # 格式 "module:ClassName"
    core_api: str = ">=0.1.0 <1.0.0"
    permissions: list[str] = field(default_factory=list)
    config_schema: dict[str, Any] | None = None
    core: bool = False  # 核心插件不可停用
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginManifest:
        """从字典构造清单（假设已做 schema 校验）。"""
        return cls(
            id=data["id"],
            name=data["name"],
            version=data["version"],
            type=data["type"],
            entry=data["entry"],
            core_api=data.get("core_api", ">=0.1.0 <1.0.0"),
            permissions=data.get("permissions", []),
            config_schema=data.get("config_schema"),
            core=data.get("core", False),
            description=data.get("description", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        import dataclasses

        return dataclasses.asdict(self)


class BasePlugin(ABC):
    """所有插件的抽象基类。"""

    manifest: PluginManifest

    @abstractmethod
    async def activate(self, ctx: PluginContext) -> None:
        """激活插件：注册服务/事件/钩子。"""
        ...

    @abstractmethod
    async def deactivate(self, ctx: PluginContext) -> None:
        """停用插件：注销服务/事件/钩子。"""
        ...

    @property
    def plugin_id(self) -> str:
        """插件 ID。"""
        return self.manifest.id
