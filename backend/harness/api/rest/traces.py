"""REST API —— 运行轨迹（Tracing）。

列出 trace、查看某条 trace 的 span 链、删除 trace。服务未启用时返回 available=False。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

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


def setup_trace_routes(registry: ServiceRegistry) -> None:
    """注册运行轨迹路由。"""

    @router.get("", summary="列出运行轨迹")
    async def list_traces(
        session_id: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        svc = _get_service(registry)
        if svc is None:
            return {"available": False, "traces": [], "count": 0}
        traces = svc.list_traces(session_id=session_id, limit=max(1, min(limit, 500)))
        return {
            "available": True,
            "traces": [t.to_dict() for t in traces],
            "count": len(traces),
        }

    @router.get("/{trace_id}", summary="查看某条 trace 的 span 链")
    async def get_trace(trace_id: str) -> dict[str, Any]:
        svc = _get_service(registry)
        if svc is None:
            raise APIError("TRACE_UNAVAILABLE", "Tracing 服务未启用", 503)
        data = svc.get_trace(trace_id)
        if data is None:
            raise APIError("TRACE_NOT_FOUND", f"未找到 trace {trace_id}", 404)
        return {"available": True, **data}

    @router.delete("/{trace_id}", summary="删除一条 trace")
    async def delete_trace(trace_id: str) -> dict[str, Any]:
        svc = _get_service(registry)
        if svc is None:
            raise APIError("TRACE_UNAVAILABLE", "Tracing 服务未启用", 503)
        removed = svc.delete_trace(trace_id)
        if removed == 0:
            raise APIError("TRACE_NOT_FOUND", f"未找到 trace {trace_id}", 404)
        return {"removed": trace_id, "spans": removed}
