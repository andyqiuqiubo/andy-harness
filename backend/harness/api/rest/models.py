"""REST API —— 模型列表路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from harness.api.errors import APIError
from harness.kernel.services import ServiceRegistry

router = APIRouter(prefix="/api/models", tags=["models"])


def setup_model_routes(registry: ServiceRegistry) -> None:
    """注册模型路由。"""

    @router.get("", summary="列出所有可用模型")
    async def list_models() -> list[dict[str, Any]]:
        from harness.modules.model_manager.provider_registry import ProviderRegistry

        try:
            pr = registry.get(ProviderRegistry)
        except Exception:
            raise APIError("PROVIDER_REGISTRY_UNAVAILABLE", "Provider 注册表不可用", 503)

        models: list[dict[str, Any]] = []
        for provider_info in pr.list_providers():
            for model_name in provider_info.get("models", []):
                models.append(
                    {
                        "id": model_name,
                        "name": model_name,
                        "provider": provider_info["id"],
                        "provider_name": provider_info.get("name", ""),
                    }
                )
        return models
