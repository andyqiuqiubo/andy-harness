"""REST API —— 长期记忆管理。

列表/检索、保存、更新、删除。服务未启用时返回 available=False。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from harness.api.errors import APIError
from harness.kernel.services import ServiceRegistry
from harness.modules.memory_manager.service import (
    SCOPE_GLOBAL,
    SCOPE_SESSION,
    MemoryService,
)

logger = logging.getLogger("harness.api.memories")

router = APIRouter(prefix="/api/memories", tags=["memories"])


def _current_user_id(request: Request) -> str:
    """E12：当前用户 id；未启用认证时返回 ""。"""
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict):
        return ""
    return str(user.get("id") or "")


class MemoryCreate(BaseModel):
    """新增/覆盖一条记忆。"""

    key: str
    value: str
    scope: str = SCOPE_GLOBAL
    session_id: str = ""
    tags: list[str] = Field(default_factory=list)


class MemoryUpdate(BaseModel):
    """更新一条记忆的字段（未提供则不改）。"""

    key: str | None = None
    value: str | None = None
    scope: str | None = None
    tags: list[str] | None = None


def _get_service(registry: ServiceRegistry) -> MemoryService | None:
    try:
        svc = registry.get(MemoryService)
    except Exception:
        return None
    return svc if isinstance(svc, MemoryService) else None


def setup_memory_routes(registry: ServiceRegistry) -> None:
    """注册长期记忆路由。"""

    @router.get("/summary-info", summary="记忆自动总结的配置与状态")
    async def summary_info() -> dict[str, Any]:
        from harness.modules.memory_manager.summarizer import (
            MemorySummarizer,
            summary_enabled,
            summary_interval,
            summary_max_sessions,
        )

        available = False
        try:
            available = isinstance(registry.get(MemorySummarizer), MemorySummarizer)
        except Exception:
            available = False
        return {
            "enabled": summary_enabled(),
            "available": available,
            "interval_seconds": summary_interval(),
            "max_sessions": summary_max_sessions(),
        }

    @router.post("/summarize", summary="立即执行一次记忆总结")
    async def summarize_now(max_sessions: int = 0) -> dict[str, Any]:
        from harness.modules.memory_manager.summarizer import MemorySummarizer

        try:
            summarizer = registry.get(MemorySummarizer)
        except Exception as e:
            raise APIError("MEMORY_SUMMARIZER_UNAVAILABLE", "记忆总结器未启用", 503) from e
        outcome = await summarizer.summarize_recent(max_sessions=max_sessions or None)
        return {"ok": not outcome.skipped_reason, **outcome.to_dict()}

    @router.get("", summary="列出 / 检索记忆")
    async def list_memories(
        request: Request,
        scope: str | None = None,
        session_id: str | None = None,
        query: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        svc = _get_service(registry)
        if svc is None:
            return {"available": False, "memories": [], "count": 0}
        limit = max(1, min(limit, 500))
        user_id = _current_user_id(request)
        if query:
            items = svc.search(query, scope=scope, limit=limit, user_id=user_id)
        else:
            items = svc.list_memories(
                scope=scope,
                session_id=session_id,
                limit=limit,
                user_id=user_id,
            )
        return {
            "available": True,
            "memories": [m.to_dict() for m in items],
            "count": len(items),
        }

    @router.post("", summary="保存记忆（相同 key 覆盖）")
    async def create_memory(request: Request, body: MemoryCreate) -> dict[str, Any]:
        svc = _get_service(registry)
        if svc is None:
            raise APIError("MEMORY_UNAVAILABLE", "长期记忆服务未启用", 503)
        if not body.key.strip():
            raise APIError("MEMORY_INVALID", "key 不能为空", 400)
        if body.scope == SCOPE_SESSION and not body.session_id:
            raise APIError("MEMORY_INVALID", "session 作用域需要 session_id", 400)
        record = svc.save(
            key=body.key,
            value=body.value,
            scope=body.scope,
            session_id=body.session_id,
            tags=body.tags,
            user_id=_current_user_id(request),
        )
        return {"memory": record.to_dict()}

    @router.patch("/{memory_id}", summary="更新记忆")
    async def update_memory(request: Request, memory_id: str, body: MemoryUpdate) -> dict[str, Any]:
        svc = _get_service(registry)
        if svc is None:
            raise APIError("MEMORY_UNAVAILABLE", "长期记忆服务未启用", 503)
        record = svc.update(
            memory_id,
            **body.model_dump(exclude_none=True),
            user_id=_current_user_id(request),
        )
        if record is None:
            raise APIError("MEMORY_NOT_FOUND", f"未找到记忆 {memory_id}", 404)
        return {"memory": record.to_dict()}

    @router.delete("/{memory_id}", summary="删除记忆")
    async def delete_memory(request: Request, memory_id: str) -> dict[str, Any]:
        svc = _get_service(registry)
        if svc is None:
            raise APIError("MEMORY_UNAVAILABLE", "长期记忆服务未启用", 503)
        if not svc.delete(memory_id, user_id=_current_user_id(request)):
            raise APIError("MEMORY_NOT_FOUND", f"未找到记忆 {memory_id}", 404)
        return {"removed": memory_id}
