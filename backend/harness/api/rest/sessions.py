"""REST API —— 会话与消息路由。"""

from __future__ import annotations

import json
from typing import Any, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from harness.api.errors import APIError
from harness.kernel.services import ServiceRegistry

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _current_user_id(request: Request) -> str:
    """E12：从认证中间件注入的 request.state.user 取用户 id。

    未启用认证时 request.state.user 不存在，返回 ""（不过滤，行为不变）。
    """
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict):
        return ""
    return str(user.get("id") or "")


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
    attachments: list[dict[str, Any]] | None = None


class SessionImport(BaseModel):
    """导入会话请求。"""

    payload: dict[str, Any]
    title: str | None = None


class SessionFork(BaseModel):
    """分叉会话请求。"""

    at_message_id: str | None = None


class TodoReplace(BaseModel):
    """替换会话任务清单请求（覆盖式写入）。"""

    todos: list[dict[str, Any]] = Field(default_factory=list)


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
    async def list_sessions(request: Request, include_archived: bool = False) -> list[dict[str, Any]]:
        service = _get_session_service(registry)
        user_id = _current_user_id(request)
        sessions = service.list_sessions(include_archived=include_archived, user_id=user_id)
        result = []
        for s in sessions:
            d = s.to_dict()
            d["message_count"] = service.count_messages(s.id)
            result.append(d)
        return result

    @router.post("", summary="创建会话")
    async def create_session(request: Request, req: SessionCreate) -> dict[str, Any]:
        service = _get_session_service(registry)
        session = service.create_session(title=req.title, user_id=_current_user_id(request))
        return cast("dict[str, Any]", session.to_dict())

    @router.get("/search", summary="搜索会话（标题 / 消息内容）")
    async def search_sessions(
        request: Request,
        q: str,
        include_archived: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """按会话标题或消息内容搜索（E9）。

        须在 `/{session_id}` 路由之前注册，避免 "search" 被当作会话 id。
        """
        service = _get_session_service(registry)
        return cast(
            "list[dict[str, Any]]",
            service.search_sessions(
                q,
                include_archived=include_archived,
                limit=limit,
                user_id=_current_user_id(request),
            ),
        )

    @router.get("/{session_id}", summary="获取会话")
    async def get_session(request: Request, session_id: str) -> dict[str, Any]:
        service = _get_session_service(registry)
        session = service.get_session(session_id, user_id=_current_user_id(request))
        if not session:
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        return cast("dict[str, Any]", session.to_dict())

    @router.patch("/{session_id}", summary="更新会话")
    async def update_session(request: Request, session_id: str, req: SessionUpdate) -> dict[str, Any]:
        service = _get_session_service(registry)
        user_id = _current_user_id(request)
        session = service.get_session(session_id, user_id=user_id)
        if not session:
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)

        if req.title is not None:
            session = service.rename_session(session_id, req.title, user_id=user_id) or session
        if req.archived is True:
            session = service.archive_session(session_id, user_id=user_id) or session

        return cast("dict[str, Any]", session.to_dict())

    @router.delete("/{session_id}", summary="删除会话")
    async def delete_session(request: Request, session_id: str) -> dict[str, str]:
        service = _get_session_service(registry)
        user_id = _current_user_id(request)
        if not service.get_session(session_id, user_id=user_id):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        service.delete_session(session_id, user_id=user_id)
        return {"status": "deleted"}

    # 快照路由
    @router.get("/{session_id}/snapshots", summary="列出会话上下文快照")
    async def list_snapshots(request: Request, session_id: str) -> list[dict[str, Any]]:
        from harness.modules.context_manager.service import ContextService

        service = _get_session_service(registry)
        if not service.get_session(session_id, user_id=_current_user_id(request)):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        try:
            context_service = registry.get(ContextService)
            return cast(
                "list[dict[str, Any]]",
                context_service.list_snapshots(session_id),
            )
        except Exception:
            raise APIError(
                "CONTEXT_SERVICE_UNAVAILABLE",
                "上下文服务不可用",
                503,
            )

    @router.get("/{session_id}/context-snapshot", summary="获取会话最新上下文快照")
    async def get_context_snapshot(request: Request, session_id: str) -> dict[str, Any] | None:
        from harness.modules.context_manager.service import ContextService

        service = _get_session_service(registry)
        if not service.get_session(session_id, user_id=_current_user_id(request)):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        try:
            context_service = registry.get(ContextService)
            return cast(
                "dict[str, Any] | None",
                context_service.get_snapshot(session_id),
            )
        except Exception:
            raise APIError(
                "CONTEXT_SERVICE_UNAVAILABLE",
                "上下文服务不可用",
                503,
            )

    # 导出 / 导入 / 分叉
    @router.get("/{session_id}/export", summary="导出会话")
    async def export_session(request: Request, session_id: str) -> dict[str, Any]:
        service = _get_session_service(registry)
        payload = service.export_session(session_id, user_id=_current_user_id(request))
        if payload is None:
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        return cast("dict[str, Any]", payload)

    @router.post("/import", summary="导入会话")
    async def import_session(request: Request, req: SessionImport) -> dict[str, Any]:
        service = _get_session_service(registry)
        try:
            session = service.import_session(
                req.payload,
                title=req.title,
                user_id=_current_user_id(request),
            )
        except Exception as e:
            raise APIError("IMPORT_FAILED", f"导入失败: {e}", 400)
        return cast("dict[str, Any]", session.to_dict())

    @router.post("/{session_id}/fork", summary="从某条消息分叉出新会话")
    async def fork_session(request: Request, session_id: str, req: SessionFork) -> dict[str, Any]:
        service = _get_session_service(registry)
        user_id = _current_user_id(request)
        if not service.get_session(session_id, user_id=user_id):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        forked = service.fork_session(session_id, req.at_message_id, user_id=user_id)
        if forked is None:
            raise APIError(
                "FORK_FAILED",
                f"分叉失败：会话或消息不存在（at_message_id={req.at_message_id}）",
                404,
            )
        return cast("dict[str, Any]", forked.to_dict())

    # Todo 路由（会话级任务清单）
    @router.get("/{session_id}/todos", summary="列出会话任务清单")
    async def list_todos(request: Request, session_id: str) -> dict[str, Any]:
        from harness.modules.todo_manager.service import TodoService

        service = _get_session_service(registry)
        if not service.get_session(session_id, user_id=_current_user_id(request)):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        try:
            todo_service = registry.get(TodoService)
        except Exception:
            return {"available": False, "todos": [], "summary": {}}
        items = todo_service.list_todos(session_id)
        return {
            "available": True,
            "todos": [t.to_dict() for t in items],
            "summary": todo_service.summary(session_id),
        }

    @router.delete("/{session_id}/todos", summary="清空会话任务清单")
    async def clear_todos(request: Request, session_id: str) -> dict[str, Any]:
        from harness.modules.todo_manager.service import TodoService

        service = _get_session_service(registry)
        if not service.get_session(session_id, user_id=_current_user_id(request)):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        try:
            todo_service = registry.get(TodoService)
        except Exception:
            raise APIError("TODO_SERVICE_UNAVAILABLE", "Todo 服务不可用", 503)
        removed = todo_service.clear_todos(session_id)
        return {"removed": removed}

    @router.put("/{session_id}/todos", summary="替换会话任务清单")
    async def replace_todos(request: Request, session_id: str, payload: TodoReplace) -> dict[str, Any]:
        from harness.modules.todo_manager.service import TodoService

        service = _get_session_service(registry)
        if not service.get_session(session_id, user_id=_current_user_id(request)):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        try:
            todo_service = registry.get(TodoService)
        except Exception:
            raise APIError("TODO_SERVICE_UNAVAILABLE", "Todo 服务不可用", 503)
        # 去除来源 id：写入的是新记录，复用原 id 会撞主键
        clean = [{k: v for k, v in item.items() if k != "id"} for item in payload.todos if isinstance(item, dict)]
        todo_service.replace_todos(session_id, clean)
        return {
            "available": True,
            "saved": len(clean),
            "todos": [t.to_dict() for t in todo_service.list_todos(session_id)],
        }

    # 消息路由
    @router.get("/{session_id}/messages", summary="列出会话消息")
    async def list_messages(
        request: Request,
        session_id: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """列出会话消息。

        P2-3：支持 `limit` / `offset` 分页。长会话此前会一次性返回全部消息，
        前端每轮结束还要全量重拉做 id 对账，形成明显的 O(n) 拉取与处理开销。
        注意 tool→结果映射**始终基于全部消息**构建（tool 消息可能落在页之外），
        分页只作用于最终返回给前端的那部分。
        不传 limit 时行为与旧版完全一致。
        """
        service = _get_session_service(registry)
        user_id = _current_user_id(request)
        if not service.get_session(session_id, user_id=user_id):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        all_messages = service.list_messages(session_id)

        # 构建 tool_call_id -> 工具结果 映射（tool 消息的 content 即执行结果）
        result_by_call_id: dict[str, str] = {}
        for m in all_messages:
            if m.role == "tool" and m.tool_call_id:
                result_by_call_id[m.tool_call_id] = m.content or ""

        result: list[dict[str, Any]] = []
        for m in all_messages:
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

        if limit is not None and limit >= 0:
            start = max(offset or 0, 0)
            return result[start : start + limit]
        return result

    @router.post("/{session_id}/messages", summary="追加消息")
    async def append_message(request: Request, session_id: str, req: MessageCreate) -> dict[str, Any]:
        service = _get_session_service(registry)
        if not service.get_session(session_id, user_id=_current_user_id(request)):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        msg = service.append_message(
            session_id=session_id,
            role=req.role,
            content=req.content,
            tool_calls=req.tool_calls,
            tokens=req.tokens,
            attachments=req.attachments,
        )
        return cast("dict[str, Any]", msg.to_dict())

    @router.delete("/{session_id}/messages/{message_id}", summary="物理删除消息（可整轮）")
    async def delete_message(
        request: Request,
        session_id: str,
        message_id: str,
        with_turn: bool = True,
    ) -> dict[str, Any]:
        """删除一条消息。

        `with_turn=true`（默认）且该消息是 user 消息时，删除**整轮问答**
        （该提问 + 紧随的回答与工具消息）；否则只删这一条。
        """
        service = _get_session_service(registry)
        user_id = _current_user_id(request)
        if not service.get_session(session_id, user_id=user_id):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        msg = service.get_message(message_id)
        if msg is None or msg.session_id != session_id:
            raise APIError("MESSAGE_NOT_FOUND", f"消息不存在: {message_id}", 404)

        if with_turn and msg.role == "user":
            removed = service.delete_turn(session_id, message_id)
            if removed < 0:
                raise APIError("MESSAGE_NOT_FOUND", f"消息不存在: {message_id}", 404)
            return {"removed": removed, "mode": "turn"}

        service.delete_message(message_id)
        return {"removed": 1, "mode": "message"}
