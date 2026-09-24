"""WebSocket /ws/chat —— 流式对话网关。

帧类型:
- token_delta: 流式 token 增量（直连 WS 推送 + EventBus 旁路广播）
- tool_event: 工具调用过程（实时推送）
- context_snapshot: 上下文构建快照
- error: 错误通知（含 code/message/detail/trace_id）
- done: 对话完成
- stop_ack: 停止确认
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

logger = logging.getLogger("harness.api.ws")

router = APIRouter()


class WSMessage(BaseModel):
    """WebSocket 消息格式。"""

    session_id: str
    content: str
    provider_id: str = "deepseek"
    model: str | None = None
    budget: int = 4096
    temperature: float = 0.7
    max_tool_iterations: int = 10
    system_prompt: str | None = None


def setup_ws_routes(services: Any, hooks: Any, tool_registry: Any) -> None:
    """注册 WebSocket 路由。

    Args:
        services: ServiceRegistry 实例
        hooks: HookManager 实例
        tool_registry: ToolRegistry 实例
    """

    async def _auto_generate_title(
        session_id: str, user_message: str, provider: Any, model: str | None
    ) -> None:
        """首次提问后用 AI 生成简短会话标题。

        仅当会话标题还是 "New Session" 时触发。
        用低成本的方式（单次非流式调用）生成不超过 20 字的标题。
        """
        try:
            from harness.modules.session_manager.service import SessionService

            session_service = services.get(SessionService)
            session = session_service.get_session(session_id)
            if not session or session.title != "New Session":
                return

            # 用 AI 生成简短标题
            title_prompt = [
                {
                    "role": "system",
                    "content": (
                        "请将用户的提问总结为一个简短的会话标题。"
                        "要求：不超过20个汉字，不要标点符号，不要引号，"
                        "直接输出标题文字。"
                    ),
                },
                {"role": "user", "content": user_message[:500]},
            ]

            title = ""
            used_model = model or "deepseek-v4-flash"
            async for chunk in provider.chat(
                messages=title_prompt,
                model=used_model,
                stream=True,
                temperature=0.3,
            ):
                if "delta" in chunk and chunk["delta"]:
                    title += chunk["delta"]
                    if len(title) > 50:
                        break

            title = title.strip().strip('"').strip("'").strip("《》").strip()
            if title and len(title) <= 50:
                session_service.rename_session(session_id, title)
                logger.info("自动生成会话标题: %s -> %s", session_id, title)
        except Exception as e:
            logger.warning("生成会话标题失败: %s", e)

    @router.websocket("/ws/chat")
    async def ws_chat(websocket: WebSocket) -> None:
        """WebSocket 对话端点。"""
        await websocket.accept()
        client_id = (
            f"{websocket.client.host}:{websocket.client.port}"
            if websocket.client
            else "unknown"
        )
        trace_id = str(uuid.uuid4())
        logger.info("WS 客户端已连接: %s (trace_id=%s)", client_id, trace_id)

        # 当前 AgentLoop 实例（供 stop 控制消息使用）
        current_loop: Any = None

        async def _send_error(code: str, message: str, detail: Any = None) -> None:
            """发送统一格式的错误帧。"""
            await websocket.send_json(
                {
                    "type": "error",
                    "data": {
                        "code": code,
                        "message": message,
                        "detail": detail,
                        "trace_id": trace_id,
                    },
                }
            )

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    data = json.loads(raw)
                except Exception as e:
                    await _send_error("INVALID_MESSAGE", "消息解析失败", str(e))
                    continue

                # 处理控制消息
                msg_type = data.get("type", "chat")
                if msg_type == "stop":
                    if current_loop is not None:
                        current_loop.stop()
                        logger.info("WS 收到停止请求: %s", client_id)
                    await websocket.send_json(
                        {"type": "stop_ack", "data": {"trace_id": trace_id}}
                    )
                    continue

                # 普通对话消息
                try:
                    msg = WSMessage(**data)
                except Exception as e:
                    await _send_error("INVALID_MESSAGE", "消息验证失败", str(e))
                    continue

                # 获取 provider
                try:
                    from harness.modules.model_manager.provider_registry import (
                        ProviderRegistry,
                    )

                    provider_registry = services.get(ProviderRegistry)
                    provider = provider_registry.get_provider(msg.provider_id)
                except Exception as e:
                    await _send_error("PROVIDER_ERROR", "获取 provider 失败", str(e))
                    continue

                # 获取 EventBus（通过 PluginLoader.events 属性）
                event_bus: Any = None
                try:
                    from harness.kernel.loader import PluginLoader

                    loader = services.get(PluginLoader)
                    event_bus = loader.events
                except Exception:
                    pass

                # 发送上下文快照
                try:
                    from harness.modules.context_manager.service import ContextService

                    context_service = services.get(ContextService)
                    context_service.build(
                        msg.session_id,
                        budget=msg.budget,
                        model=msg.model or "gpt-4o",
                    )
                    snapshot = context_service.get_snapshot(msg.session_id)
                    if snapshot:
                        await websocket.send_json(
                            {"type": "context_snapshot", "data": snapshot}
                        )
                except Exception:
                    pass  # 上下文服务可能未注册

                # 执行 AgentLoop
                from harness.engine.agent_loop import AgentLoop, AgentLoopConfig

                config = AgentLoopConfig(
                    model=msg.model or "gpt-4o",
                    budget=msg.budget,
                    temperature=msg.temperature,
                    max_tool_iterations=msg.max_tool_iterations,
                    system_prompt=msg.system_prompt or "",
                )

                loop = AgentLoop(
                    services=services,
                    hooks=hooks,
                    tool_registry=tool_registry,
                    config=config,
                )
                current_loop = loop

                # 工具事件实时回调（S18: 替代循环结束后批量发送）
                async def _on_tool_event(event: dict[str, Any]) -> None:
                    await websocket.send_json(event)

                # 停止消息并发监听器（H7: 在 loop.run 期间接收 stop 消息）
                async def _stop_listener() -> None:
                    try:
                        while True:
                            ctrl_raw = await websocket.receive_text()
                            try:
                                ctrl = json.loads(ctrl_raw)
                                if ctrl.get("type") == "stop":
                                    if current_loop is not None:
                                        current_loop.stop()
                                        logger.info(
                                            "WS 运行中收到停止请求: %s",
                                            client_id,
                                        )
                                    await websocket.send_json(
                                        {
                                            "type": "stop_ack",
                                            "data": {"trace_id": trace_id},
                                        }
                                    )
                                    break
                            except Exception:
                                pass
                    except (WebSocketDisconnect, asyncio.CancelledError):
                        pass
                    except Exception:
                        pass

                # 包装 provider 以推送 token_delta（S17: 同时发布到 EventBus）
                original_chat = provider.chat

                async def streaming_chat(*args: Any, **kwargs: Any) -> Any:
                    async for chunk in original_chat(*args, **kwargs):
                        if "delta" in chunk and chunk["delta"]:
                            await websocket.send_json(
                                {
                                    "type": "token_delta",
                                    "data": {"delta": chunk["delta"]},
                                }
                            )
                            # S17: 旁路广播到 EventBus
                            if event_bus is not None:
                                await event_bus.publish(
                                    "model.request.delta",
                                    {
                                        "session_id": msg.session_id,
                                        "delta": chunk["delta"],
                                    },
                                )
                        if "reasoning_content" in chunk:
                            await websocket.send_json(
                                {
                                    "type": "token_delta",
                                    "data": {
                                        "reasoning_content": chunk[
                                            "reasoning_content"
                                        ]
                                    },
                                }
                            )
                        yield chunk

                provider.chat = streaming_chat

                # 启动停止监听器
                stop_task = asyncio.create_task(_stop_listener())

                try:
                    result = await loop.run(
                        session_id=msg.session_id,
                        user_message=msg.content,
                        provider=provider,
                        model=msg.model,
                        budget=msg.budget,
                        on_tool_event=_on_tool_event,
                    )
                finally:
                    # S20: 恢复 provider.chat
                    provider.chat = original_chat
                    current_loop = None
                    # 取消停止监听器
                    stop_task.cancel()
                    try:
                        await stop_task
                    except asyncio.CancelledError:
                        pass

                # 发送错误或完成
                if result.error:
                    await _send_error("AGENT_LOOP_ERROR", result.error)
                else:
                    # 自动生成会话标题：首次提问后用 AI 总结
                    await _auto_generate_title(
                        msg.session_id, msg.content, provider, msg.model
                    )

                    await websocket.send_json(
                        {
                            "type": "done",
                            "data": {
                                "content": result.content,
                                "iterations": result.iterations,
                                "latency_ms": result.latency_ms,
                                "short_circuited": result.short_circuited,
                                "trace_id": trace_id,
                            },
                        }
                    )

        except WebSocketDisconnect:
            logger.info("WS 客户端已断开: %s", client_id)
        except Exception as e:
            logger.error("WS 错误: %s", e, exc_info=True)
            try:
                await _send_error("INTERNAL_ERROR", "内部错误", str(e))
            except Exception:
                pass
