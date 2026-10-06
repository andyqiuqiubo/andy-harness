"""REST API —— 第三方系统集成（前端配置与交互入口）。

设计要点：
- 与 ``permissions.py`` 同构（``APIRouter`` + ``require_admin``）。
- 认证未启用（单用户）时自动放行，启用后仅管理员可访问。
- 真正的实现在 ``plugins.integration_hub`` 插件内（统一接口 + 各系统提供方），
  这里只做 HTTP 门面，延迟导入注册表，避免插件未启用时产生硬依赖。
- 端点：
  - ``GET    /api/integrations``                 列出所有提供方（接入方式/认证状态/动作）
  - ``GET    /api/integrations/{provider}``        单个提供方详情
  - ``POST   /api/integrations/{provider}/connect`` 按 api/cli 完成授权
  - ``POST   /api/integrations/{provider}/disconnect`` 断开并清除本地认证状态
  - ``POST   /api/integrations/{provider}/call``    调用一个动作
  - ``POST   /api/integrations/{provider}/channel/start`` 启动入站渠道（WebSocket 长连）
  - ``POST   /api/integrations/{provider}/channel/stop``  停止入站渠道
  - ``POST   /api/integrations/{provider}/channel/status`` 查询入站渠道状态
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from harness.api.deps import require_admin
from plugins.integration_hub.integration_base import (
    IntegrationProvider,
    IntegrationRegistry,
    get_registry,
)

logger = logging.getLogger("harness.api.integrations")

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


def _registry() -> IntegrationRegistry:
    """延迟导入集成注册表（插件激活后才存在）。"""
    return get_registry()


def _get_provider(provider_id: str) -> IntegrationProvider:
    provider = _registry().get(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"未找到集成提供方: {provider_id}")
    return provider


@router.get("")
async def list_integrations(
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """列出所有已注册的第三方系统集成。"""
    return {"providers": [p.describe() for p in _registry().list()]}


@router.get("/{provider_id}")
async def get_integration(
    provider_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """单个提供方详情（含认证状态与动作清单）。"""
    return _get_provider(provider_id).describe()


@router.post("/{provider_id}/connect")
async def connect_integration(
    provider_id: str,
    body: dict[str, Any] = Body(default={}),
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """按指定接入方式（api/cli）完成认证授权。

    API 模式需在 credentials 中提供 app_id / app_secret；
    CLI 模式 credentials 可留空（依赖本机已安装的官方命令行工具）。
    """
    provider = _get_provider(provider_id)
    mode = body.get("mode", "")
    credentials = body.get("credentials") or {}
    result = await provider.authenticate(mode, credentials)
    return result.to_dict()


@router.post("/{provider_id}/disconnect")
async def disconnect_integration(
    provider_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """断开并清除本地认证状态（token / CLI 记录）。"""
    provider = _get_provider(provider_id)
    provider.disconnect()
    return {"ok": True}


@router.post("/{provider_id}/call")
async def call_integration(
    provider_id: str,
    body: dict[str, Any] = Body(default={}),
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """调用一个动作。body: { action, params }。"""
    provider = _get_provider(provider_id)
    action = body.get("action", "")
    params = body.get("params") or {}
    result = await provider.call(action, params)
    return result.to_dict()


@router.post("/{provider_id}/channel/start")
async def start_channel(
    provider_id: str,
    body: dict[str, Any] = Body(default={}),
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """启动入站渠道（Channel 模式）：让 Agent 在飞书里被对话调用。

    body: { credentials: { app_id, app_secret } }。采用 WebSocket 长连，无需公网回调。
    """
    provider = _get_provider(provider_id)
    result = await provider.start_channel(body.get("credentials") or {})
    return result.to_dict()


@router.post("/{provider_id}/channel/stop")
async def stop_channel(
    provider_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """停止入站渠道。"""
    provider = _get_provider(provider_id)
    result = await provider.stop_channel()
    return result.to_dict()


@router.post("/{provider_id}/channel/status")
async def channel_status(
    provider_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """查询入站渠道运行状态。"""
    provider = _get_provider(provider_id)
    return provider.channel_status()
