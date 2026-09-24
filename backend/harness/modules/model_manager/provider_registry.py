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

    def register_provider(
        self,
        provider_id: str,
        provider_class: type[OpenAICompatibleProvider],
        config: dict[str, Any] | None = None,
    ) -> None:
        """注册一个 provider。"""
        self._providers[provider_id] = (provider_class, config or {})
        # 清除旧实例缓存
        self._instances.pop(provider_id, None)
        logger.info("Provider 已注册: %s", provider_id)

    def unregister_provider(self, provider_id: str) -> None:
        """注销一个 provider。"""
        self._providers.pop(provider_id, None)
        self._instances.pop(provider_id, None)
        logger.info("Provider 已注销: %s", provider_id)

    def get_provider(self, provider_id: str) -> OpenAICompatibleProvider:
        """获取 provider 实例。

        Args:
            provider_id: provider 标识

        Raises:
            ProviderNotFoundError: provider 未注册
        """
        if provider_id not in self._providers:
            raise ProviderNotFoundError(f"Provider 未找到: {provider_id}")

        if provider_id not in self._instances:
            provider_class, config = self._providers[provider_id]
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
        """列出所有已注册 provider。"""
        result: list[dict[str, Any]] = []
        for provider_id, (provider_class, config) in self._providers.items():
            result.append(
                {
                    "id": provider_id,
                    "name": config.get("name", provider_id),
                    "base_url": config.get("base_url", provider_class.base_url),
                    "models": config.get("models", provider_class.default_models),
                    "has_api_key": bool(
                        config.get("api_key") or config.get("api_key_encrypted")
                    ),
                }
            )
        return result

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
        if existing:
            db.execute(
                "UPDATE providers SET name = ?, base_url = ?, "
                "api_key_encrypted = ?, models_json = ?, "
                "extra_params_json = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (
                    config.get("name", provider_id),
                    config.get("base_url", ""),
                    config.get("api_key_encrypted", ""),
                    json.dumps(config.get("models", [])),
                    json.dumps(config.get("extra_params", {})),
                    provider_id,
                ),
            )
        else:
            db.execute(
                "INSERT INTO providers (id, name, base_url, api_key_encrypted, "
                "models_json, extra_params_json, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, datetime('now'), datetime('now'))",
                (
                    provider_id,
                    config.get("name", provider_id),
                    config.get("base_url", ""),
                    config.get("api_key_encrypted", ""),
                    json.dumps(config.get("models", [])),
                    json.dumps(config.get("extra_params", {})),
                ),
            )
        logger.info("Provider 已持久化到数据库: %s", provider_id)

    def load_from_db(self, db: Any) -> None:
        """从数据库加载自定义 provider。"""
        rows = db.query("SELECT * FROM providers WHERE enabled = 1")
        for row in rows:
            provider_id = row["id"]
            if self.has_provider(provider_id):
                # 不覆盖已注册的内置 provider
                continue

            models = json.loads(row["models_json"]) if row["models_json"] else []
            extra_params = (
                json.loads(row["extra_params_json"])
                if row["extra_params_json"]
                else {}
            )

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
            }
            self.register_provider(provider_id, CustomProvider, config)
            logger.info("从数据库加载 provider: %s", provider_id)
