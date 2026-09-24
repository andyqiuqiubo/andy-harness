"""REST API —— Provider 路由。"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter
from pydantic import BaseModel

from harness.api.errors import APIError
from harness.kernel.services import ServiceRegistry

router = APIRouter(prefix="/api/providers", tags=["providers"])


class ProviderCreate(BaseModel):
    """创建自定义 provider 请求。"""

    name: str
    base_url: str
    api_key: str
    models: list[str] | None = None
    extra_params: dict[str, Any] | None = None


class ProviderUpdate(BaseModel):
    """更新 provider 请求。"""

    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    models: list[str] | None = None
    extra_params: dict[str, Any] | None = None


def _get_provider_registry(registry: ServiceRegistry) -> Any:
    """获取 ProviderRegistry。"""
    from harness.modules.model_manager.provider_registry import ProviderRegistry

    try:
        return registry.get(ProviderRegistry)
    except Exception:
        raise APIError(
            "PROVIDER_REGISTRY_UNAVAILABLE",
            "Provider 注册表不可用",
            503,
        )


def setup_provider_routes(registry: ServiceRegistry) -> None:
    """注册 provider 路由。"""

    @router.get("", summary="列出 provider")
    async def list_providers() -> list[dict[str, Any]]:
        pr = _get_provider_registry(registry)
        return cast("list[dict[str, Any]]", pr.list_providers())

    @router.get("/{provider_id}", summary="获取单个 provider")
    async def get_provider(provider_id: str) -> dict[str, Any]:
        pr = _get_provider_registry(registry)
        if not pr.has_provider(provider_id):
            raise APIError(
                "PROVIDER_NOT_FOUND", f"Provider 不存在: {provider_id}", 404
            )
        config = pr.get_provider_config(provider_id) or {}
        return {
            "id": provider_id,
            "name": config.get("name", provider_id),
            "base_url": config.get("base_url", ""),
            "models": config.get("models", []),
            "has_api_key": bool(
                config.get("api_key") or config.get("api_key_encrypted")
            ),
            "extra_params": config.get("extra_params", {}),
        }

    @router.post("", summary="创建自定义 provider")
    async def create_provider(req: ProviderCreate) -> dict[str, Any]:
        from harness.infra.crypto import APIKeyEncryptor
        from harness.infra.database import Database
        from harness.modules.model_manager.openai_compatible import (
            OpenAICompatibleProvider,
        )

        pr = _get_provider_registry(registry)

        # 动态创建 provider 类
        class CustomProvider(OpenAICompatibleProvider):
            base_url = req.base_url
            default_models = req.models or ["gpt-4o"]
            provider_name = req.name

        # 加密 API Key（只存储加密后的，不存明文）
        encryptor = APIKeyEncryptor()
        encrypted_key = encryptor.encrypt(req.api_key)

        config: dict[str, Any] = {
            "name": req.name,
            "api_key_encrypted": encrypted_key,
            "base_url": req.base_url,
            "models": req.models or [],
        }
        if req.extra_params:
            config["extra_params"] = req.extra_params

        provider_id = f"custom_{req.name.lower().replace(' ', '_')}"

        # 注册并持久化到数据库
        try:
            db = registry.get(Database)
            pr.create_provider(provider_id, CustomProvider, config, db)
        except Exception:
            pr.register_provider(provider_id, CustomProvider, config)

        return {
            "id": provider_id,
            "name": req.name,
            "base_url": req.base_url,
            "has_api_key": True,
        }

    @router.patch("/{provider_id}", summary="更新 provider")
    async def update_provider(
        provider_id: str, req: ProviderUpdate
    ) -> dict[str, Any]:
        from harness.infra.crypto import APIKeyEncryptor
        from harness.infra.database import Database

        pr = _get_provider_registry(registry)
        if not pr.has_provider(provider_id):
            raise APIError(
                "PROVIDER_NOT_FOUND", f"Provider 不存在: {provider_id}", 404
            )

        current_config = pr.get_provider_config(provider_id)
        if not current_config:
            raise APIError(
                "PROVIDER_NOT_FOUND", f"Provider 不存在: {provider_id}", 404
            )

        updated_config: dict[str, Any] = dict(current_config)
        if req.name is not None:
            updated_config["name"] = req.name
        if req.base_url is not None:
            updated_config["base_url"] = req.base_url
        if req.models is not None:
            updated_config["models"] = req.models
        if req.extra_params is not None:
            updated_config["extra_params"] = req.extra_params
        if req.api_key is not None:
            encryptor = APIKeyEncryptor()
            updated_config["api_key_encrypted"] = encryptor.encrypt(req.api_key)
            updated_config.pop("api_key", None)

        pr.update_provider_config(provider_id, updated_config)

        # 持久化到数据库
        try:
            db = registry.get(Database)
            pr.persist_provider(db, provider_id, updated_config)
        except Exception:
            pass

        return {
            "id": provider_id,
            "name": updated_config.get("name", provider_id),
            "has_api_key": bool(updated_config.get("api_key_encrypted")),
        }

    @router.delete("/{provider_id}", summary="删除 provider")
    async def delete_provider(provider_id: str) -> dict[str, str]:
        from harness.infra.database import Database

        pr = _get_provider_registry(registry)
        if not pr.has_provider(provider_id):
            raise APIError("PROVIDER_NOT_FOUND", f"Provider 不存在: {provider_id}", 404)

        try:
            db = registry.get(Database)
            pr.delete_provider(provider_id, db)
        except Exception:
            pr.unregister_provider(provider_id)

        return {"status": "deleted"}

    @router.post("/{provider_id}/test", summary="测试 provider 连接")
    async def test_provider(provider_id: str) -> dict[str, Any]:
        pr = _get_provider_registry(registry)
        if not pr.has_provider(provider_id):
            raise APIError("PROVIDER_NOT_FOUND", f"Provider 不存在: {provider_id}", 404)
        provider = pr.get_provider(provider_id)
        ok = await provider.health_check()
        return {"connected": ok, "provider": provider_id}
