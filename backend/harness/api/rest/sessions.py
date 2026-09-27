"""REST API —— 会话与消息路由。"""

from __future__ import annotations

import json
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
        result = []
        for s in sessions:
            d = s.to_dict()
            d["message_count"] = service.count_messages(s.id)
            result.append(d)
        return result

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

    @router.get("/{session_id}/context-snapshot", summary="获取会话最新上下文快照")
    async def get_context_snapshot(session_id: str) -> dict[str, Any] | None:
        from harness.modules.context_manager.service import ContextService

        service = _get_session_service(registry)
        if not service.get_session(session_id):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        try:
            context_service = registry.get(ContextService)
            return context_service.get_snapshot(session_id)
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

        # 构建 tool_call_id -> 工具结果 映射（tool 消息的 content 即执行结果）
        result_by_call_id: dict[str, str] = {}
        for m in messages:
            if m.role == "tool" and m.tool_call_id:
                result_by_call_id[m.tool_call_id] = m.content or ""

        result: list[dict[str, Any]] = []
        for m in messages:
            # tool 角色消息仅用于模型 API 上下文（tool_call_id 关联），
            # 其执行结果已并入 assistant 消息的 tool_calls 卡片中展示，
            # 不再单独返回给前端，避免渲染成孤立的工具头像行。
            if m.role == "tool":
                continue
            d = m.to_dict()
            # 数据库中的 tool_calls 以 OpenAI API 原始格式存储
            # （{id, type, function:{name, arguments}}，供上下文构建直接
            # 喂给模型 API）。序列化给前端时转换为执行结果格式
            # （{tool_name, args, result, error}），与 WS 实时推送的
            # tool_event 结构保持一致，避免历史消息工具卡片缺失名称/内容。
            raw_tool_calls = d.get("tool_calls")
            if m.role == "assistant" and isinstance(raw_tool_calls, list) and raw_tool_calls:
                converted: list[dict[str, Any]] = []
                for tc in raw_tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function")
                    if isinstance(fn, dict):
                        # OpenAI API 原始格式 → 前端执行结果格式
                        try:
                            parsed_args = json.loads(fn.get("arguments") or "{}")
                        except (json.JSONDecodeError, TypeError):
                            parsed_args = {}
                        converted.append(
                            {
                                "tool_name": fn.get("name", ""),
                                "args": parsed_args if isinstance(parsed_args, dict) else {},
                                "result": result_by_call_id.get(tc.get("id", ""), ""),
                                "error": tc.get("error"),
                            }
                        )
                    else:
                        # 已是执行结果格式（新数据/其他来源），原样保留
                        converted.append(
                            {
                                "tool_name": tc.get("tool_name", ""),
                                "args": tc.get("args", {}),
                                "result": tc.get("result", ""),
                                "error": tc.get("error"),
                            }
                        )
                d["tool_calls"] = converted
            result.append(d)
        return result

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
