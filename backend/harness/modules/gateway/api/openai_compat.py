"""OpenAI 兼容数据面 —— /v1/models 与 /v1/chat/completions。"""

from __future__ import annotations

import logging
import uuid
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from harness.api.errors import APIError
from harness.infra.crypto import redact_secrets
from harness.kernel.services import ServiceRegistry
from harness.modules.gateway.config import ConfigError
from harness.modules.gateway.contracts import GatewayError
from harness.modules.gateway.service import GatewayService

logger = logging.getLogger("harness.gateway.api")

router = APIRouter(prefix="/v1", tags=["gateway"])


def _service(registry: ServiceRegistry) -> GatewayService:
    try:
        svc = cast(GatewayService, registry.get(GatewayService))
    except Exception as e:  # noqa: BLE001
        raise APIError("GATEWAY_UNAVAILABLE", "网关服务不可用", 503) from e
    if not svc.started:
        raise APIError("GATEWAY_UNAVAILABLE", "网关未启动（请检查配置）", 503)
    return svc


MAX_BODY_BYTES = 8 * 1024 * 1024


def _error(err: GatewayError, trace_id: str) -> JSONResponse:
    body = err.to_openai(trace_id)
    body["error"]["message"] = redact_secrets(str(body["error"]["message"]))[:500]
    headers: dict[str, str] = {}
    if err.http_status in (429, 503):
        headers["Retry-After"] = str(err.cooldown_seconds or 30)
    return JSONResponse(status_code=err.http_status, content=body, headers=headers)


def _key(service: GatewayService, request: Request) -> Any:
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    ctx = service.auth.verify(token) if token else None
    if ctx is None:
        raise GatewayError("GW_AUTH_FAILED", "无效或缺失的 API Key", 401)
    return ctx


def setup_gateway_v1_routes(registry: ServiceRegistry) -> None:
    """注册 /v1 路由（模块级 router 可被重复装配，先清理旧闭包）。"""
    router.routes.clear()

    @router.get("/models", summary="列出可用模型（OpenAI 兼容）")
    async def list_models(request: Request) -> Any:
        svc = _service(registry)
        trace_id = str(uuid.uuid4())
        try:
            ctx = _key(svc, request)
            return {"object": "list", "data": svc.models(ctx)}
        except GatewayError as e:
            return _error(e, trace_id)

    @router.post("/chat/completions", summary="对话补全（OpenAI 兼容）")
    async def chat_completions(request: Request) -> Any:
        svc = _service(registry)
        trace_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        try:
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
                raise GatewayError("GW_INVALID_REQUEST", "请求体过大（>8MB）", 413)
            ctx = _key(svc, request)
            try:
                body = await request.json()
            except Exception as e:  # noqa: BLE001
                raise GatewayError("GW_INVALID_REQUEST", f"请求体不是合法 JSON: {e}", 400) from e
            if not isinstance(body, dict):
                raise GatewayError("GW_INVALID_REQUEST", "请求体必须是 JSON 对象", 400)
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages:
                raise GatewayError("GW_INVALID_REQUEST", "messages 必须是非空数组", 400)
            result = await svc.chat(body, ctx, trace_id)
            if result.stream and result.iterator is not None:
                return StreamingResponse(
                    result.iterator,
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Trace-Id": trace_id},
                )
            return JSONResponse(result.payload or {})
        except GatewayError as e:
            logger.info("gateway 请求失败: code=%s status=%s", e.code, e.http_status)
            return _error(e, trace_id)
        except ConfigError as e:
            logger.warning("gateway 配置不可用: %s", e)
            return _error(GatewayError("GW_CONFIG_ERROR", f"网关配置不可用: {e}", 503), trace_id)
