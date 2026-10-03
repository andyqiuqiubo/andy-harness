"""REST API —— 运行轨迹（Tracing）。

列出 trace、查看某条 trace 的 span 链、删除 trace。服务未启用时返回 available=False。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from harness.api.errors import APIError
from harness.kernel.services import ServiceRegistry
from harness.modules.tracing.service import SpanService

logger = logging.getLogger("harness.api.traces")

router = APIRouter(prefix="/api/traces", tags=["traces"])


def _get_service(registry: ServiceRegistry) -> SpanService | None:
    try:
        svc = registry.get(SpanService)
    except Exception:
        return None
    return svc if isinstance(svc, SpanService) else None


def _owner_user_id(request: Request) -> str:
    """E12：从认证中间件注入的 request.state.user 取用户 id。

    未启用认证时 request.state.user 不存在，返回 ""（不过滤，行为不变）。
    """
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict):
        return ""
    return str(user.get("id") or "")


def _session_service(registry: ServiceRegistry) -> Any:
    from harness.modules.session_manager.service import SessionService

    try:
        return registry.get(SessionService)
    except Exception:
        return None


def setup_trace_routes(registry: ServiceRegistry) -> None:
    """注册运行轨迹路由。"""

    @router.get("", summary="列出运行轨迹")
    async def list_traces(
        session_id: str | None = None,
        limit: int = 50,
        request: Request = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        svc = _get_service(registry)
        if svc is None:
            return {"available": False, "traces": [], "count": 0}
        traces = svc.list_traces(
            session_id=session_id,
            user_id=_owner_user_id(request) or None,
            limit=max(1, min(limit, 500)),
        )
        return {
            "available": True,
            "traces": [t.to_dict() for t in traces],
            "count": len(traces),
        }

    @router.get("/{trace_id}", summary="查看某条 trace 的 span 链")
    async def get_trace(
        trace_id: str,
        request: Request = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        svc = _get_service(registry)
        if svc is None:
            raise APIError("TRACE_UNAVAILABLE", "Tracing 服务未启用", 503)
        data = svc.get_trace(trace_id)
        if data is None:
            raise APIError("TRACE_NOT_FOUND", f"未找到 trace {trace_id}", 404)
        # E12：启用认证时，trace 归属的会话必须属于当前用户（两跳：span→session→user）。
        owner = _owner_user_id(request)
        if owner:
            sess = _session_service(registry)
            summary = data.get("summary") or {}
            sid = summary.get("session_id") or ""
            if not sid or sess is None or not sess.get_session(sid, user_id=owner):
                raise APIError("TRACE_NOT_FOUND", "轨迹不存在或无权访问", 404)
        return {"available": True, **data}

    @router.delete("/{trace_id}", summary="删除一条 trace")
    async def delete_trace(
        trace_id: str,
        request: Request = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        svc = _get_service(registry)
        if svc is None:
            raise APIError("TRACE_UNAVAILABLE", "Tracing 服务未启用", 503)
        # E12：先按归属校验再删除，避免越权删除他人轨迹。
        owner = _owner_user_id(request)
        if owner:
            data = svc.get_trace(trace_id)
            if data is None:
                raise APIError("TRACE_NOT_FOUND", f"未找到 trace {trace_id}", 404)
            sess = _session_service(registry)
            summary = data.get("summary") or {}
            sid = summary.get("session_id") or ""
            if not sid or sess is None or not sess.get_session(sid, user_id=owner):
                raise APIError("TRACE_NOT_FOUND", "轨迹不存在或无权访问", 404)
        removed = svc.delete_trace(trace_id)
        if removed == 0:
            raise APIError("TRACE_NOT_FOUND", f"未找到 trace {trace_id}", 404)
        return {"removed": trace_id, "spans": removed}
