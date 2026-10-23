"""gateway 管理面 —— /api/gateway/*（require_admin）。"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from harness.api.deps import require_admin
from harness.api.errors import APIError
from harness.kernel.services import ServiceRegistry
from harness.modules.gateway.config import ConfigError
from harness.modules.gateway.login import LOGIN_TIMEOUT_SECONDS
from harness.modules.gateway.service import GatewayService

logger = logging.getLogger("harness.gateway.admin")

router = APIRouter(prefix="/api/gateway", tags=["gateway"])


def _service(registry: ServiceRegistry) -> GatewayService:
    try:
        svc = cast(GatewayService, registry.get(GatewayService))
    except Exception as e:  # noqa: BLE001
        raise APIError("GATEWAY_UNAVAILABLE", "网关服务不可用", 503) from e
    if not svc.started:
        raise APIError("GATEWAY_UNAVAILABLE", "网关未启动（请检查配置）", 503)
    return svc


def _actor(admin: dict[str, Any] | None) -> str:
    if isinstance(admin, dict):
        return str(admin.get("username") or admin.get("id") or "admin")
    return "local"


def _magpie_fallback_available(svc: GatewayService, channel_id: str) -> bool:
    """该通道当前是否在"借用" Magpie 登录态（自存为空但文件里有）。"""
    if svc.credential_store.load(channel_id):
        return False
    managers: dict[str, Any] = {
        "qoder-cn": svc.credentials.qoder,
        "workbuddy": svc.credentials.workbuddy,
    }
    manager = managers.get(channel_id)
    if manager is None:
        return False
    try:
        return bool(manager.present())
    except Exception:  # noqa: BLE001 —— 探测失败按"无兜底"
        return False


def setup_gateway_routes(registry: ServiceRegistry) -> None:
    router.routes.clear()

    @router.get("/channels", summary="列出通道")
    async def channels(admin: dict[str, Any] | None = Depends(require_admin)) -> list[dict[str, Any]]:
        return _service(registry).channels_public()

    @router.get("/models", summary="列出全部模型及模型级可用性（管理面）")
    async def models(admin: dict[str, Any] | None = Depends(require_admin)) -> list[dict[str, Any]]:
        """管理面专用：不受租户白名单限制；/v1/models 需要网关 Key，前端拿不到。"""
        return _service(registry).all_models()

    # ---- 会话页免费模型可用性（供前端轮询）--------------------------- #
    # 只含通道/模型 id 与布尔可用性，不含任何凭据或用量；因此不设 require_admin，
    # 任何通过 AuthMiddleware 的登录用户都能读——会话页要实时展示"不可用"。
    @router.get("/model-availability", summary="免费模型可用性快照（会话页轮询）")
    async def model_availability(channels: str | None = None) -> dict[str, Any]:
        wanted = [c for c in channels.split(",") if c] if channels else None
        return _service(registry).availability_snapshot(wanted)

    @router.post("/model-availability/refresh", summary="立即触发一次可用性探测")
    async def model_availability_refresh(
        request: Request,
        admin: dict[str, Any] | None = Depends(require_admin),
    ) -> dict[str, Any]:
        channels: list[str] | None = None
        try:
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("channels"), list):
                channels = [str(c) for c in body["channels"]]
        except Exception:  # noqa: BLE001 —— 可选请求体
            pass
        svc = _service(registry)
        await svc.probe_now(channels)
        return svc.availability_snapshot(channels)

    # ---- 自建登录（免装 Magpie） -------------------------------------- #
    @router.get("/logins", summary="各通道自建登录能力与已存凭据")
    async def list_logins(
        admin: dict[str, Any] | None = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        svc = _service(registry)
        rows = svc.login.channels()
        for row in rows:
            row["fallback_available"] = _magpie_fallback_available(svc, str(row["channel_id"]))
        return rows

    @router.post("/channels/{channel_id}/login", summary="发起通道自建登录（返回授权 URL）")
    async def start_login(
        channel_id: str,
        admin: dict[str, Any] | None = Depends(require_admin),
    ) -> dict[str, Any]:
        svc = _service(registry)
        try:
            session = svc.login.start(channel_id)
        except ValueError as e:
            raise APIError("LOGIN_INVALID", str(e), 400) from e
        svc.store.audit(
            "channel.login.start",
            actor=_actor(admin),
            target_type="channel",
            target_id=channel_id,
            detail={"session_id": session.session_id},
        )
        return {
            "session_id": session.session_id,
            "channel_id": session.channel_id,
            "login_url": session.login_url,
            "expires_in": LOGIN_TIMEOUT_SECONDS,
        }

    @router.get("/logins/{session_id}", summary="查询登录会话状态")
    async def login_status(
        session_id: str,
        admin: dict[str, Any] | None = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return _service(registry).login.status(session_id)
        except KeyError as e:
            raise APIError("LOGIN_NOT_FOUND", "登录会话不存在或已过期", 404) from e

    @router.delete(
        "/channels/{channel_id}/credential", summary="删除通道自存凭据（退出登录）"
    )
    async def channel_logout(
        channel_id: str,
        admin: dict[str, Any] | None = Depends(require_admin),
    ) -> dict[str, Any]:
        svc = _service(registry)
        svc.login.logout(channel_id)
        svc.store.audit(
            "channel.logout",
            actor=_actor(admin),
            target_type="channel",
            target_id=channel_id,
            detail={},
        )
        return {"channel_id": channel_id, "stored": False}

    @router.get("/config", summary="读取当前配置")
    async def get_config(admin: dict[str, Any] | None = Depends(require_admin)) -> dict[str, Any]:
        svc = _service(registry)
        snap = svc.snapshot
        return {"version": snap.version, "checksum": snap.checksum, "config": snap.raw}

    @router.put("/config", summary="整体替换配置（写版本）")
    async def put_config(
        request: Request,
        admin: dict[str, Any] | None = Depends(require_admin),
    ) -> dict[str, Any]:
        svc = _service(registry)
        try:
            payload = await request.json()
        except Exception as e:  # noqa: BLE001
            raise APIError("INVALID_CONFIG", f"请求体不是合法 JSON: {e}", 400) from e
        if not isinstance(payload, dict):
            raise APIError("INVALID_CONFIG", "配置必须是 JSON 对象", 400)
        try:
            return svc.apply_config(payload, actor=_actor(admin), source="api")
        except ConfigError as e:
            raise APIError("INVALID_CONFIG", str(e), 400) from e

    @router.get("/config/versions", summary="配置版本列表")
    async def config_versions(
        limit: int = 20,
        admin: dict[str, Any] | None = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return _service(registry).store.list_config_versions(limit)

    @router.post("/config/rollback", summary="按版本回滚配置")
    async def config_rollback(
        request: Request,
        admin: dict[str, Any] | None = Depends(require_admin),
    ) -> dict[str, Any]:
        svc = _service(registry)
        body = await request.json()
        if not isinstance(body, dict) or not body.get("version"):
            raise APIError("INVALID_REQUEST", "version 必填", 400)
        payload = svc.store.get_config_version(int(body["version"]))
        if payload is None:
            raise APIError("NOT_FOUND", "配置版本不存在", 404)
        try:
            result = svc.apply_config(payload, actor=_actor(admin), source="rollback")
        except ConfigError as e:
            raise APIError("INVALID_CONFIG", str(e), 400) from e
        svc.store.audit(
            "config.rollback",
            actor=_actor(admin),
            target_type="config",
            detail={"from_version": int(body["version"]), "applied": result},
        )
        return result

    @router.get("/keys", summary="列出 API Key（仅前缀）")
    async def list_keys(
        tenant_id: str | None = None,
        admin: dict[str, Any] | None = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return _service(registry).auth.list_keys(tenant_id)

    @router.post("/keys", summary="签发 API Key（明文仅返回一次）")
    async def create_key(
        request: Request,
        admin: dict[str, Any] | None = Depends(require_admin),
    ) -> dict[str, Any]:
        svc = _service(registry)
        body = await request.json()
        if not isinstance(body, dict):
            raise APIError("INVALID_REQUEST", "请求体必须是 JSON 对象", 400)
        tenant_id = str(body.get("tenant_id") or "default")
        scopes = body.get("scopes") or ["chat", "models"]
        if not isinstance(scopes, list):
            raise APIError("INVALID_REQUEST", "scopes 必须是数组", 400)
        try:
            issued = svc.auth.issue_key(
                tenant_id,
                str(body.get("name") or "api-key"),
                [str(s) for s in scopes],
                int(body.get("rpm") or 0),
                int(body.get("tpm") or 0),
                expires_at=body.get("expires_at"),
            )
        except ValueError as e:
            raise APIError("TENANT_INVALID", str(e), 400) from e
        svc.store.audit(
            "key.issue",
            actor=_actor(admin),
            target_type="key",
            target_id=issued.key_id,
            detail={"tenant_id": tenant_id, "prefix": issued.prefix},
        )
        return {
            "id": issued.key_id,
            "key": issued.token,
            "prefix": issued.prefix,
            "tenant_id": issued.tenant_id,
        }

    @router.post("/keys/{key_id}/revoke", summary="吊销 API Key")
    async def revoke_key(
        key_id: str,
        admin: dict[str, Any] | None = Depends(require_admin),
    ) -> dict[str, Any]:
        svc = _service(registry)
        ok = svc.auth.revoke(key_id)
        if not ok:
            raise APIError("NOT_FOUND", "Key 不存在", 404)
        svc.store.audit("key.revoke", actor=_actor(admin), target_type="key", target_id=key_id)
        return {"ok": True}

    @router.post("/keys/{key_id}/rotate", summary="轮换 API Key（旧 Key 24h 宽限）")
    async def rotate_key(
        key_id: str,
        admin: dict[str, Any] | None = Depends(require_admin),
    ) -> dict[str, Any]:
        svc = _service(registry)
        issued = svc.auth.rotate(key_id)
        if issued is None:
            raise APIError("NOT_FOUND", "Key 不存在或已吊销", 404)
        svc.store.audit(
            "key.rotate",
            actor=_actor(admin),
            target_type="key",
            target_id=key_id,
            detail={"new_key_id": issued.key_id, "prefix": issued.prefix},
        )
        return {
            "id": issued.key_id,
            "key": issued.token,
            "prefix": issued.prefix,
            "tenant_id": issued.tenant_id,
            "old_key_grace_hours": 24,
        }

    @router.get("/tenants", summary="列出租户")
    async def list_tenants(admin: dict[str, Any] | None = Depends(require_admin)) -> list[dict[str, Any]]:
        return _service(registry).auth.list_tenants()

    @router.post("/tenants", summary="创建租户")
    async def create_tenant(
        request: Request,
        admin: dict[str, Any] | None = Depends(require_admin),
    ) -> dict[str, Any]:
        svc = _service(registry)
        body = await request.json()
        if not isinstance(body, dict) or not body.get("id"):
            raise APIError("INVALID_REQUEST", "id 必填", 400)
        try:
            svc.auth.create_tenant(
                str(body["id"]),
                str(body.get("name") or body["id"]),
                body.get("allowed_models") or ["*"],
                body.get("allowed_channels") or ["*"],
            )
        except sqlite3.IntegrityError as e:
            raise APIError("TENANT_EXISTS", f"租户已存在: {body['id']}", 409) from e
        svc.store.audit("tenant.create", actor=_actor(admin), target_type="tenant", target_id=str(body["id"]))
        return {"ok": True}

    @router.get("/usage", summary="用量查询")
    async def usage(
        tenant_id: str | None = None,
        limit: int = 100,
        admin: dict[str, Any] | None = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return _service(registry).store.query_usage(tenant_id, limit)

    @router.get("/audit", summary="审计查询")
    async def audit(
        limit: int = 100,
        admin: dict[str, Any] | None = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return _service(registry).store.query_audit(limit)

    @router.get("/metrics", summary="Prometheus 指标", response_class=PlainTextResponse)
    async def metrics(admin: dict[str, Any] | None = Depends(require_admin)) -> PlainTextResponse:
        return PlainTextResponse(_service(registry).metrics_text())
