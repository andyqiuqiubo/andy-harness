"""REST API —— 工件（大工具输出落盘）管理。

列出已落盘的工件、读取正文、删除。服务未启用时返回 available=False。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from harness.api.errors import APIError
from harness.kernel.services import ServiceRegistry
from harness.modules.artifact_store.service import ArtifactStore

logger = logging.getLogger("harness.api.artifacts")

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])


def _get_service(registry: ServiceRegistry) -> ArtifactStore | None:
    try:
        svc = registry.get(ArtifactStore)
    except Exception:
        return None
    return svc if isinstance(svc, ArtifactStore) else None


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


def setup_artifact_routes(registry: ServiceRegistry) -> None:
    """注册工件管理路由。"""

    @router.get("", summary="列出工件")
    async def list_artifacts(
        session_id: str | None = None,
        limit: int = 100,
        request: Request = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        svc = _get_service(registry)
        if svc is None:
            return {"available": False, "artifacts": [], "count": 0}
        user_id = _owner_user_id(request)
        items = svc.list_artifacts(
            session_id=session_id,
            user_id=user_id or None,
            limit=max(1, min(limit, 500)),
        )
        return {
            "available": True,
            "artifacts": [r.to_dict() for r in items],
            "count": len(items),
        }

    @router.get("/{artifact_id}", summary="读取工件正文")
    async def get_artifact(
        artifact_id: str,
        offset: int = 0,
        limit: int | None = None,
        request: Request = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        svc = _get_service(registry)
        if svc is None:
            raise APIError("ARTIFACT_UNAVAILABLE", "工件服务未启用", 503)
        record = svc.get(artifact_id)
        if record is None:
            raise APIError("ARTIFACT_NOT_FOUND", f"未找到工件 {artifact_id}", 404)
        # E12：启用认证时，工件归属的会话必须属于当前用户（两跳：artifact→session→user）。
        owner = _owner_user_id(request)
        if owner:
            sess = _session_service(registry)
            if sess is None or not sess.get_session(record.session_id, user_id=owner):
                raise APIError("ARTIFACT_NOT_FOUND", "工件不存在或无权访问", 404)
        content = svc.read(artifact_id, offset=offset, limit=limit)
        if content is None:
            raise APIError("ARTIFACT_FILE_MISSING", f"工件 {artifact_id} 的正文文件缺失", 404)
        return {"artifact": record.to_dict(), "content": content, "offset": offset}

    @router.delete("/{artifact_id}", summary="删除工件")
    async def delete_artifact(
        artifact_id: str,
        request: Request = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        svc = _get_service(registry)
        if svc is None:
            raise APIError("ARTIFACT_UNAVAILABLE", "工件服务未启用", 503)
        record = svc.get(artifact_id)
        if record is None:
            raise APIError("ARTIFACT_NOT_FOUND", f"未找到工件 {artifact_id}", 404)
        # E12：启用认证时，先校验归属再删除，避免越权删除他人工件。
        owner = _owner_user_id(request)
        if owner:
            sess = _session_service(registry)
            if sess is None or not sess.get_session(record.session_id, user_id=owner):
                raise APIError("ARTIFACT_NOT_FOUND", "工件不存在或无权访问", 404)
        removed = svc.delete(artifact_id)
        if not removed:
            raise APIError("ARTIFACT_NOT_FOUND", f"未找到工件 {artifact_id}", 404)
        return {"removed": artifact_id}
