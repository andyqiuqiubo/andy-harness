"""ProviderRegistry —— 统一管理内置与自定义 provider。

通过 get_provider(id) 获取 provider 实例，不感知配置来源差异。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from harness.modules.model_manager.openai_compatible import (
    OpenAICompatibleProvider,
)

logger = logging.getLogger("harness.model_manager")


class ProviderNotFoundError(Exception):
    """Provider 未找到。"""


class ProviderConfigError(Exception):
    """Provider 配置错误。"""


class ProviderRegistry:
    """Provider 注册表。

    统一管理:
    - 内置 provider（通过插件加载，配置存在 plugins 表）
    - 自定义 provider（通过 API/配置文件创建，存在 providers 表）
    """

    def __init__(self) -> None:
        # provider_id -> (provider_class, config)
        self._providers: dict[str, tuple[type[OpenAICompatibleProvider], dict[str, Any]]] = {}
        # provider_id -> 实例缓存
        self._instances: dict[str, OpenAICompatibleProvider] = {}
        # provider_id -> 数据库中的配置覆盖项（用于内置 provider 的启用状态/密钥持久化）
        self._config_overrides: dict[str, dict[str, Any]] = {}

    def register_provider(
        self,
        provider_id: str,
        provider_class: type[OpenAICompatibleProvider],
        config: dict[str, Any] | None = None,
    ) -> None:
        """注册一个 provider（自动合并数据库覆盖项：enabled / api_key_encrypted）。"""
        merged = dict(config or {})
        overrides = self._config_overrides.get(provider_id)
        if overrides:
            if "enabled" in overrides:
                merged["enabled"] = overrides["enabled"]
            # 用户在设置页保存的加密密钥优先于插件默认配置/环境变量
            if overrides.get("api_key_encrypted"):
                merged["api_key_encrypted"] = overrides["api_key_encrypted"]
                merged.pop("api_key", None)
        self._providers[provider_id] = (provider_class, merged)
        # 清除旧实例缓存
        self._instances.pop(provider_id, None)
        logger.info("Provider 已注册: %s", provider_id)

    def unregister_provider(self, provider_id: str) -> None:
        """注销一个 provider。"""
        self._providers.pop(provider_id, None)
        self._instances.pop(provider_id, None)
        logger.info("Provider 已注销: %s", provider_id)

    def get_provider(
        self, provider_id: str, *, include_disabled: bool = False
    ) -> OpenAICompatibleProvider:
        """获取 provider 实例。

        Args:
            provider_id: provider 标识
            include_disabled: 为 True 时允许获取已停用的 provider（用于连接测试）

        Raises:
            ProviderNotFoundError: provider 未注册
            ProviderConfigError: provider 已停用或未配置 API Key
        """
        if provider_id not in self._providers:
            raise ProviderNotFoundError(f"Provider 未找到: {provider_id}")

        provider_class, config = self._providers[provider_id]
        if not include_disabled and not config.get("enabled", True):
            raise ProviderConfigError(f"Provider [{provider_id}] 已停用")

        if provider_id not in self._instances:
            # 从配置中提取参数
            api_key = config.get("api_key", "")

            # 如果没有明文 api_key 但有加密 key，则解密
            if not api_key and config.get("api_key_encrypted"):
                try:
                    from harness.infra.crypto import APIKeyEncryptor

                    encryptor = APIKeyEncryptor()
                    api_key = encryptor.decrypt(config["api_key_encrypted"])
                except Exception as e:
                    raise ProviderConfigError(
                        f"Provider [{provider_id}] API Key 解密失败: {e}"
                    ) from e

            if not api_key:
                raise ProviderConfigError(
                    f"Provider [{provider_id}] 未配置 API Key"
                )

            self._instances[provider_id] = provider_class(
                api_key=api_key,
                base_url=config.get("base_url"),
                models=config.get("models"),
                extra_params=config.get("extra_params"),
            )

        return self._instances[provider_id]

    def list_providers(self) -> list[dict[str, Any]]:
        """列出所有已注册 provider（含 enabled 状态）。"""
        return [self.get_provider_info(pid) for pid in self._providers]

    def get_provider_info(self, provider_id: str) -> dict[str, Any]:
        """返回单个 provider 的完整信息（用于 API 响应）。"""
        provider_class, config = self._providers[provider_id]
        # 内置 provider 默认 enabled=True；自定义 provider 从 DB 加载时含 enabled 字段
        enabled = config.get("enabled", True)
        return {
            "id": provider_id,
            "name": config.get("name", provider_id),
            "base_url": config.get("base_url", provider_class.base_url),
            "models": config.get("models", provider_class.default_models),
            "has_api_key": bool(
                config.get("api_key") or config.get("api_key_encrypted")
            ),
            "enabled": bool(enabled),
        }

    def has_provider(self, provider_id: str) -> bool:
        """检查 provider 是否已注册。"""
        return provider_id in self._providers

    def update_provider_config(
        self,
        provider_id: str,
        config: dict[str, Any],
    ) -> None:
        """更新 provider 配置（清除实例缓存，下次 get_provider 时重建）。"""
        if provider_id not in self._providers:
            raise ProviderNotFoundError(f"Provider 未找到: {provider_id}")

        provider_class, _ = self._providers[provider_id]
        self._providers[provider_id] = (provider_class, config)
        self._instances.pop(provider_id, None)
        logger.info("Provider 配置已更新: %s", provider_id)

    def get_provider_config(self, provider_id: str) -> dict[str, Any] | None:
        """获取 provider 当前配置（返回副本）。"""
        if provider_id not in self._providers:
            return None
        _, config = self._providers[provider_id]
        return dict(config)

    def create_provider(
        self,
        provider_id: str,
        provider_class: type[OpenAICompatibleProvider],
        config: dict[str, Any],
        db: Any | None = None,
    ) -> None:
        """注册并持久化自定义 provider。"""
        self.register_provider(provider_id, provider_class, config)
        if db is not None:
            self.persist_provider(db, provider_id, config)

    def delete_provider(
        self,
        provider_id: str,
        db: Any | None = None,
    ) -> None:
        """注销并从数据库删除自定义 provider。"""
        self.unregister_provider(provider_id)
        if db is not None:
            db.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
            logger.info("Provider 已从数据库删除: %s", provider_id)

    def persist_provider(
        self,
        db: Any,
        provider_id: str,
        config: dict[str, Any],
    ) -> None:
        """将 provider 配置持久化到数据库（INSERT 或 UPDATE）。"""
        existing = db.query_one(
            "SELECT id FROM providers WHERE id = ?", (provider_id,)
        )
        enabled = 1 if config.get("enabled", True) else 0
        if existing:
            db.execute(
                "UPDATE providers SET name = ?, base_url = ?, "
                "api_key_encrypted = ?, models_json = ?, "
                "extra_params_json = ?, enabled = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (
                    config.get("name", provider_id),
                    config.get("base_url", ""),
                    config.get("api_key_encrypted", ""),
                    json.dumps(config.get("models", [])),
                    json.dumps(config.get("extra_params", {})),
                    enabled,
                    provider_id,
                ),
            )
        else:
            db.execute(
                "INSERT INTO providers (id, name, base_url, api_key_encrypted, "
                "models_json, extra_params_json, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                (
                    provider_id,
                    config.get("name", provider_id),
                    config.get("base_url", ""),
                    config.get("api_key_encrypted", ""),
                    json.dumps(config.get("models", [])),
                    json.dumps(config.get("extra_params", {})),
                    enabled,
                ),
            )
        logger.info("Provider 已持久化到数据库: %s", provider_id)

    def load_from_db(self, db: Any) -> None:
        """从数据库加载自定义 provider，并记录内置 provider 的配置覆盖项。"""
        rows = db.query("SELECT * FROM providers")
        for row in rows:
            provider_id = row["id"]
            enabled = bool(row["enabled"])

            models = json.loads(row["models_json"]) if row["models_json"] else []
            extra_params = (
                json.loads(row["extra_params_json"])
                if row["extra_params_json"]
                else {}
            )
            overrides: dict[str, Any] = {
                "enabled": enabled,
                "api_key_encrypted": row["api_key_encrypted"] or "",
            }

            # 已注册的 provider（如内置 provider 插件先激活）：应用覆盖项
            if self.has_provider(provider_id):
                provider_class, config = self._providers[provider_id]
                merged = dict(config)
                merged.update(overrides)
                if overrides.get("api_key_encrypted"):
                    merged.pop("api_key", None)
                self._providers[provider_id] = (provider_class, merged)
                self._instances.pop(provider_id, None)
                logger.info("应用数据库覆盖项到 provider: %s", provider_id)
                continue

            # 未注册：记录覆盖项，供后续插件激活时合并
            self._config_overrides[provider_id] = overrides

            # 仅注册自定义 provider（custom_ 前缀）；内置 provider 等插件激活后自动合并覆盖项
            if not provider_id.startswith("custom_"):
                continue

            class CustomProvider(OpenAICompatibleProvider):
                base_url = row["base_url"]
                default_models = models
                provider_name = row["name"]

            config: dict[str, Any] = {
                "name": row["name"],
                "api_key_encrypted": row["api_key_encrypted"] or "",
                "base_url": row["base_url"],
                "models": models,
                "extra_params": extra_params,
                "enabled": enabled,
            }
            self.register_provider(provider_id, CustomProvider, config)
            logger.info("从数据库加载 provider: %s (enabled=%s)", provider_id, enabled)
