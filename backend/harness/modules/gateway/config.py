"""gateway 配置 —— pydantic 校验 + 不可变快照 + 原子保存。"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

BACKEND_DIR = Path(__file__).resolve().parents[3]  # backend/
DEFAULT_CONFIG_PATH = BACKEND_DIR / "data" / "gateway.json"
EXAMPLE_CONFIG_PATH = BACKEND_DIR / "gateway.example.json"


class CredentialRef(BaseModel):
    """配置中的凭据引用；source 形如 env:VAR / local:cline / public。"""

    id: str
    source: str
    weight: int = 1


class Defaults(BaseModel):
    strategy: str = "weighted"
    max_failovers: int = 2
    request_budget_seconds: int = 240
    read_timeout_seconds: int = 120
    health_cooldown_seconds: int = 30
    usage_retention_days: int = 90
    probe_interval_seconds: int = 300
    # 纯思考空停恢复：推理模型把输出预算全耗在 reasoning 时，从检查点续写一次
    # 索要正文（借鉴 dsh recovery.js；关闭后直接返回空正文 + reasoning_content）
    empty_answer_recovery: bool = True


class ChannelConfig(BaseModel):
    id: str
    name: str = ""
    enabled: bool = True
    adapter: str = "openai-like"
    base_url: str
    chat_path: str = "/chat/completions"
    probe_path: str | None = None
    auth: str = "public"
    probe_url: str | None = None
    probe_interval_seconds: int | None = None
    # probe_url 是不是可靠的"可调用模型目录"。WorkBuddy 的 /v3/config 是配置端点、
    # 不含全部可调用模型（step-5-preview 可用却不在列），置 false 跳过清单判定，
    # 避免把能用的模型误标下架而置灰。
    catalog_probe: bool = True
    stream_only: bool = False
    unwrap: str | None = None
    requires_tools: bool = False
    weight: int = 1
    strategy: str = "weighted"
    models: list[str] = Field(default_factory=list)
    model_map: dict[str, str] = Field(default_factory=dict)
    credentials: list[CredentialRef] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    allow_private: bool = False


class TenantSeed(BaseModel):
    id: str
    name: str = ""
    allowed_models: list[str] = Field(default_factory=lambda: ["*"])
    allowed_channels: list[str] = Field(default_factory=lambda: ["*"])


class GatewayConfig(BaseModel):
    version: int = 1
    defaults: Defaults = Field(default_factory=Defaults)
    channels: list[ChannelConfig] = Field(default_factory=list)
    group_aliases: dict[str, list[str]] = Field(default_factory=dict)
    tenants: list[TenantSeed] = Field(default_factory=list)


@dataclass(frozen=True)
class ConfigSnapshot:
    """请求入口捕获的不可变配置视图（热更新期间在途请求语义稳定）。"""

    version: int
    checksum: str
    channels: dict[str, ChannelConfig]
    aliases: dict[str, list[str]]
    defaults: Defaults
    tenants: list[TenantSeed]
    raw: dict[str, Any] = field(default_factory=dict)

    def channel_list(self) -> list[ChannelConfig]:
        return [c for c in self.channels.values() if c.enabled]

    def resolve_alias(self, model: str) -> list[str]:
        return list(self.aliases.get(model, []))


class ConfigError(Exception):
    """配置解析/校验失败。"""


def _reject_private_literal(channel: ChannelConfig) -> None:
    """拒绝字面量私网/回环地址（DNS 解析级别的 SSRF 防护见设计 §4.5.5，P2 接 net_guard）。"""
    if channel.allow_private:
        return
    host = urlparse(channel.base_url).hostname or ""
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise ConfigError(
            f"channel {channel.id} 指向 localhost，如为本地 Ollama/NewAPI 请显式设置 allow_private: true"
        )
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ConfigError(
            f"channel {channel.id} 指向私有地址 {host}，如为本地 Ollama/NewAPI 请显式设置 allow_private: true"
        )


def _validate_channels(config: GatewayConfig) -> None:
    seen: set[str] = set()
    for ch in config.channels:
        if ch.id in seen:
            raise ConfigError(f"channel id 重复: {ch.id}")
        seen.add(ch.id)
        if not ch.base_url.startswith(("http://", "https://")):
            raise ConfigError(f"channel {ch.id} 的 base_url 必须是 http(s)")
        _reject_private_literal(ch)
        if not ch.models:
            raise ConfigError(f"channel {ch.id} 至少需要一个 model")
        if ch.weight < 1 or ch.weight > 100:
            raise ConfigError(f"channel {ch.id} 的 weight 必须在 1..100")


def _checksum(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ConfigService:
    """配置文件读写与快照管理（版本号由 service 落库，本类只管快照）。"""

    def __init__(self, path: Path | None = None, example_path: Path | None = None) -> None:
        self.path = Path(path or os.environ.get("HARNESS_GATEWAY_CONFIG") or DEFAULT_CONFIG_PATH)
        self.example_path = Path(example_path or EXAMPLE_CONFIG_PATH)
        self._snapshot: ConfigSnapshot | None = None

    # ---- 文件 ---------------------------------------------------------- #
    def ensure_file(self) -> None:
        if self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.example_path.exists():
            shutil.copyfile(self.example_path, self.path)
        else:
            self.path.write_text(json.dumps({"version": 1, "channels": []}, indent=2), encoding="utf-8")

    def load(self) -> ConfigSnapshot:
        """读取 + 校验；失败抛 ConfigError，调用方保持现网快照不变。"""
        self.ensure_file()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ConfigError(f"配置不是合法 JSON: {e}") from e
        if not isinstance(payload, dict):
            raise ConfigError("配置根节点必须是对象")

        config = ConfigService.parse(payload)

        _validate_channels(config)
        snapshot = ConfigSnapshot(
            version=config.version,
            checksum=_checksum(payload),
            channels={c.id: c for c in config.channels},
            aliases=config.group_aliases,
            defaults=config.defaults,
            tenants=config.tenants,
            raw=payload,
        )
        self._snapshot = snapshot
        return snapshot

    @property
    def snapshot(self) -> ConfigSnapshot:
        if self._snapshot is None:
            return self.load()
        return self._snapshot

    def save(self, payload: dict[str, Any]) -> ConfigSnapshot:
        """全量校验通过后才原子写盘；任何失败都不落盘。"""
        self.validate_payload(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise ConfigError(f"配置写盘失败: {e}") from e
        return self.load()

    @staticmethod
    def parse(payload: dict[str, Any]) -> GatewayConfig:
        try:
            return GatewayConfig(**payload)
        except Exception as e:
            raise ConfigError(f"配置校验失败: {e}") from e

    @staticmethod
    def validate_payload(payload: dict[str, Any]) -> GatewayConfig:
        """不落盘的完整校验（pydantic + 业务规则），供写入口预检。"""
        config = ConfigService.parse(payload)
        _validate_channels(config)
        return config
