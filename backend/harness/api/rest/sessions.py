"""REST API —— 会话与消息路由。"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter
from pydantic import BaseModel

from harness.api.errors import APIError
from harness.kernel.services import ServiceRegistry

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class SessionCreate(BaseModel):
    """创建会话请求。"""

    title: str = "New Session"


class SessionUpdate(BaseModel):
    """更新会话请求。"""

    title: str | None = None
    archived: bool | None = None


class MessageCreate(BaseModel):
    """追加消息请求。"""

    role: str
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tokens: int = 0


def _get_session_service(registry: ServiceRegistry) -> Any:
    """获取 SessionService。"""
    from harness.modules.session_manager.service import SessionService

    try:
        return registry.get(SessionService)
    except Exception:
        raise APIError(
            "SESSION_SERVICE_UNAVAILABLE",
            "会话服务不可用",
            503,
        )


def setup_session_routes(registry: ServiceRegistry) -> None:
    """注册会话路由（依赖注入 ServiceRegistry）。"""

    @router.get("", summary="列出会话")
    async def list_sessions(include_archived: bool = False) -> list[dict[str, Any]]:
        service = _get_session_service(registry)
        sessions = service.list_sessions(include_archived=include_archived)
        return [s.to_dict() for s in sessions]

    @router.post("", summary="创建会话")
    async def create_session(req: SessionCreate) -> dict[str, Any]:
        service = _get_session_service(registry)
        session = service.create_session(title=req.title)
        return cast("dict[str, Any]", session.to_dict())

    @router.get("/{session_id}", summary="获取会话")
    async def get_session(session_id: str) -> dict[str, Any]:
        service = _get_session_service(registry)
        session = service.get_session(session_id)
        if not session:
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        return cast("dict[str, Any]", session.to_dict())

    @router.patch("/{session_id}", summary="更新会话")
    async def update_session(session_id: str, req: SessionUpdate) -> dict[str, Any]:
        service = _get_session_service(registry)
        session = service.get_session(session_id)
        if not session:
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)

        if req.title is not None:
            session = service.rename_session(session_id, req.title) or session
        if req.archived is True:
            session = service.archive_session(session_id) or session

        return cast("dict[str, Any]", session.to_dict())

    @router.delete("/{session_id}", summary="删除会话")
    async def delete_session(session_id: str) -> dict[str, str]:
        service = _get_session_service(registry)
        if not service.get_session(session_id):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        service.delete_session(session_id)
        return {"status": "deleted"}

    # 快照路由
    @router.get("/{session_id}/snapshots", summary="列出会话上下文快照")
    async def list_snapshots(session_id: str) -> list[dict[str, Any]]:
        from harness.modules.context_manager.service import ContextService

        service = _get_session_service(registry)
        if not service.get_session(session_id):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        try:
            context_service = registry.get(ContextService)
            return context_service.list_snapshots(session_id)
        except Exception:
            raise APIError(
                "CONTEXT_SERVICE_UNAVAILABLE",
                "上下文服务不可用",
                503,
            )

    # 消息路由
    @router.get("/{session_id}/messages", summary="列出会话消息")
    async def list_messages(session_id: str) -> list[dict[str, Any]]:
        service = _get_session_service(registry)
        if not service.get_session(session_id):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        messages = service.list_messages(session_id)
        return [m.to_dict() for m in messages]

    @router.post("/{session_id}/messages", summary="追加消息")
    async def append_message(session_id: str, req: MessageCreate) -> dict[str, Any]:
        service = _get_session_service(registry)
        if not service.get_session(session_id):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        msg = service.append_message(
            session_id=session_id,
            role=req.role,
            content=req.content,
            tool_calls=req.tool_calls,
            tokens=req.tokens,
        )
        return cast("dict[str, Any]", msg.to_dict())
