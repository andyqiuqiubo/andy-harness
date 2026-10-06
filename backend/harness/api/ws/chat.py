"""WebSocket /ws/chat —— 流式对话网关。

帧类型:
- token_delta: 流式 token 增量（直连 WS 推送 + EventBus 旁路广播）
- tool_event: 工具调用过程（实时推送）
- context_snapshot: 上下文构建快照
- error: 错误通知（含 code/message/detail/trace_id）
- done: 对话完成
- stop_ack: 停止确认
- confirm_request: 请求人工确认某次工具调用（权限策略命中时）
- confirm_timeout: 确认超时（按拒绝处理）

控制消息（客户端 → 服务端）:
- {"type": "stop"}: 中断生成
- {"type": "confirm_reply", "data": {"request_id": "...", "approved": true|false}}
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

# 人工确认等待超时（秒）。超时按拒绝处理，避免请求永久挂起。
CONFIRM_TIMEOUT = 120.0


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
    attachments: list[dict[str, Any]] = []
    # 重新回答：指向被重新生成的 user 消息 id。设置后不再新建 user 消息，
    # 仅生成一条新的 assistant 答案版本（parent_id 指向该 user 消息）。
    parent_message_id: str | None = None


class _StreamingProviderProxy:
    """每连接的 provider 代理：仅覆写 `chat`，**绝不改动共享 provider 实例**。

    背景（P0-1）：`ProviderRegistry.get_provider()` 返回的是**共享单例**
    （按 provider_id 缓存在 `_instances`）。过去这里直接执行
    `provider.chat = wrapper`，于是每个 WS 连接都在改同一个对象：
    - 并发连接会互相嵌套包装，后者的 `original_chat` 捕获到前者的 wrapper；
    - 一次模型响应被多个 wrapper 各自 `send_json`，**跨会话串流、内容互泄**；
    - 任一连接结束时 `provider.chat = original_chat` 可能回滚掉别人刚打好的补丁。

    改为每连接持有一个代理对象：通过 `__getattr__` 把除 `chat` 之外的
    一切属性/方法委托给真实 provider（保持 TokenCounter 等能力可用），
    真实实例始终保持干净。
    """

    def __init__(self, provider: Any, session_id: str, emit: Any) -> None:
        self._provider = provider
        self._session_id = session_id
        self._emit = emit

    def __getattr__(self, name: str) -> Any:
        # `__getattr__` 仅在常规属性查找失败时触发，
        # 因此代理上显式定义的 `chat` 不会被委托，其余全部转发给真实实例。
        return getattr(self._provider, name)

    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        from harness.engine.runtime import get_runtime

        async for chunk in self._provider.chat(*args, **kwargs):
            runtime = get_runtime()
            # 子代理运行时（contextvar 指向别的会话）不向主界面流式输出，
            # 避免把子代理的中间过程混进主对话
            is_subagent = runtime is not None and getattr(runtime, "session_id", "") != self._session_id
            if is_subagent:
                yield chunk
                continue
            await self._emit(chunk)
            yield chunk


def _resolve_attachment_metas(services: Any, session_id: str, refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把客户端传来的附件引用（id 列表）校验并展开为公开元信息。

    校验：附件必须存在且归属于当前 session，否则忽略（安全默认，不阻断对话）。
    返回可直接存入消息 / 渲染多模态内容的公开元信息列表（不含存储路径）。
    """
    if not refs:
        return []
    ids = [str(r.get("id", "")) for r in refs if r.get("id")]
    if not ids:
        return []
    try:
        from harness.infra.database import Database
        from harness.infra.repository import AttachmentRepository

        db = services.get(Database)
        repo = AttachmentRepository(db)
        metas = repo.list_by_ids(ids)
        # 仅保留归属当前会话的附件，且保持客户端传入顺序
        valid = {m["id"]: m for m in metas if m.get("session_id") == session_id}
        return [valid[i] for i in ids if i in valid]
    except Exception as e:
        logger.warning("解析附件元信息失败: %s", e)
        return []


def setup_ws_routes(services: Any, hooks: Any, tool_registry: Any) -> None:
    """注册 WebSocket 路由。

    Args:
        services: ServiceRegistry 实例
        hooks: HookManager 实例
        tool_registry: ToolRegistry 实例
    """

    async def _auto_generate_title(
        session_id: str,
        user_message: str,
        provider: Any,
        model: str | None,
        trace_id: str = "",
    ) -> None:
        """首次提问后用 AI 生成简短会话标题。

        仅当会话标题还是 "New Session" 时触发。
        用低成本的方式（单次非流式调用）生成不超过 20 字的标题。
        同时把这次调用作为 model_call span 挂到当前 run 的 trace 下，
        让轨迹页能看到这次额外的 LLM 开销（0.0.24）。
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
            # P2-9：优先回退到 **该 provider 自己的默认模型**，而不是写死某个厂商 id。
            # 旧实现写死 "deepseek-v4-flash"，在非 deepseek provider 上会静默失效（仅有 warning），
            # 会话标题退化成 New Session。厂商 id 仅作为最后兜底。
            default_model = ""
            provider_models = getattr(provider, "models", None) or []
            if isinstance(provider_models, list) and provider_models:
                default_model = str(provider_models[0])
            used_model = model or default_model or "deepseek-flash"

            import time as _time

            _t0 = _time.time()
            _usage: dict[str, Any] | None = None
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
                if "usage" in chunk and chunk["usage"]:
                    _usage = chunk["usage"]

            # 把这次标题生成也作为 model_call span 挂到当前 run trace 下
            try:
                from harness.kernel.eventbus import EventBus

                if trace_id and services.has(EventBus):
                    bus = services.get(EventBus)
                    await bus.publish(
                        "trace.span",
                        {
                            "trace_id": trace_id,
                            "parent_id": trace_id,
                            "session_id": session_id,
                            "name": "title_generation",
                            "kind": "model",
                            "status": "ok",
                            "duration_ms": int((_time.time() - _t0) * 1000),
                            "prompt_tokens": (_usage or {}).get("prompt_tokens", 0),
                            "completion_tokens": (_usage or {}).get("completion_tokens", 0),
                            "total_tokens": (_usage or {}).get("total_tokens", 0),
                            "input_preview": "生成会话标题",
                            "output_preview": title[:200],
                        },
                    )
            except Exception:
                pass

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
        client_id = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "unknown"
        trace_id = str(uuid.uuid4())
        logger.info("WS 客户端已连接: %s (trace_id=%s)", client_id, trace_id)

        # E12：认证启用时，WS 连接须携带 ?token=（Bearer 无法用于 WS 握手）。
        # 无效或过期 token：关闭 1008（Policy Violation）并结束。
        auth_user_id = ""
        try:
            from harness.modules.auth_manager.service import AuthService

            if services.has(AuthService):
                auth_service = services.get(AuthService)
                if auth_service.enabled:
                    token = websocket.query_params.get("token", "")
                    user = auth_service.user_from_token(token) if token else None
                    if user is None:
                        logger.warning("WS 认证失败（token 无效/过期）: %s", client_id)
                        await websocket.close(code=1008)
                        return
                    auth_user_id = user.id
        except Exception as e:  # noqa: BLE001
            logger.error("WS 认证检查异常，关闭连接: %s", e)
            await websocket.close(code=1008)
            return

        # 当前 AgentLoop 实例（供 stop 控制消息使用）
        current_loop: Any = None

        # 待处理的确认请求：request_id -> Future[bool]
        pending_confirms: dict[str, Any] = {}

        def _reject_pending_confirms() -> None:
            """把所有未决的确认请求按「拒绝」兑现，避免协程永久挂起。

            P2-7：客户端中途断开时同样需要调用，否则挂起的工具要等到
            CONFIRM_TIMEOUT 超时才释放。
            """
            for fut in list(pending_confirms.values()):
                if not fut.done():
                    fut.set_result(False)
            pending_confirms.clear()

        async def _request_confirm(tool_name: str, args: dict[str, Any], risk: str, reason: str) -> bool:
            """向客户端发起人工确认请求并等待答复。

            超时或连接异常均按拒绝处理（安全默认）。
            """
            request_id = str(uuid.uuid4())
            fut: Any = asyncio.get_running_loop().create_future()
            pending_confirms[request_id] = fut

            await websocket.send_json(
                {
                    "type": "confirm_request",
                    "data": {
                        "request_id": request_id,
                        "tool_name": tool_name,
                        "args": args,
                        "risk": risk,
                        "reason": reason,
                        "timeout": CONFIRM_TIMEOUT,
                        "trace_id": trace_id,
                    },
                }
            )
            logger.info(
                "已发起人工确认请求: %s (tool=%s risk=%s)",
                request_id,
                tool_name,
                risk,
            )

            try:
                return bool(await asyncio.wait_for(fut, timeout=CONFIRM_TIMEOUT))
            except TimeoutError:
                logger.warning("人工确认超时，按拒绝处理: %s", request_id)
                try:
                    await websocket.send_json(
                        {
                            "type": "confirm_timeout",
                            "data": {"request_id": request_id, "tool_name": tool_name},
                        }
                    )
                except Exception:
                    pass
                return False
            finally:
                pending_confirms.pop(request_id, None)

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

        # ── P0-4：单 reader 统一读取入向帧，再分发，消除双 receive_text 竞争 ──
        # 旧实现：主循环与 _stop_listener 两个协程并发调用 websocket.receive_text()，
        # 长回答进行中用户追加提问 / 发 stop 时，帧会被监听协程抢走后静默丢弃
        # （except Exception: pass），消息凭空消失且无任何提示。
        # 新实现：仅一个 reader 协程读 socket，按帧类型分发到两条队列：
        #   - control_queue：stop / confirm_reply（由 dispatcher 处理）
        #   - message_queue：普通对话帧（由主循环处理），避免丢帧。
        message_queue: asyncio.Queue[Any] = asyncio.Queue()
        control_queue: asyncio.Queue[Any] = asyncio.Queue()

        async def _reader() -> None:
            """唯一读取 WebSocket 入向帧的协程。"""
            try:
                while True:
                    raw = await websocket.receive_text()
                    try:
                        frame = json.loads(raw)
                    except Exception as e:
                        await message_queue.put({"__invalid__": raw, "__err__": str(e)})
                        continue
                    ftype = frame.get("type") if isinstance(frame, dict) else None
                    if ftype in ("stop", "confirm_reply"):
                        await control_queue.put(frame)
                    else:
                        await message_queue.put(frame)
            except (WebSocketDisconnect, asyncio.CancelledError):
                # P2-7：客户端在等待人工确认时断开，立刻兑现「安全默认」语义。
                _reject_pending_confirms()
            except Exception:
                logger.error("WS reader 异常", exc_info=True)
                _reject_pending_confirms()
            finally:
                # 通知主循环与 dispatcher 结束
                await message_queue.put(None)
                await control_queue.put(None)

        async def _dispatcher() -> None:
            """分发控制帧：stop → 中断生成；confirm_reply → 兑现确认 Future。"""
            while True:
                frame = await control_queue.get()
                if frame is None:
                    break
                ftype = frame.get("type") if isinstance(frame, dict) else None
                if ftype == "confirm_reply":
                    payload = frame.get("data") or frame
                    rid = payload.get("request_id", "")
                    fut = pending_confirms.get(rid)
                    if fut is not None and not fut.done():
                        fut.set_result(bool(payload.get("approved", False)))
                        logger.info("收到人工确认答复: %s -> %s", rid, payload.get("approved"))
                elif ftype == "stop":
                    if current_loop is not None:
                        current_loop.stop()
                        logger.info("WS 运行中收到停止请求: %s", client_id)
                    try:
                        await websocket.send_json({"type": "stop_ack", "data": {"trace_id": trace_id}})
                    except Exception:
                        pass

        reader_task = asyncio.create_task(_reader())
        dispatcher_task = asyncio.create_task(_dispatcher())

        try:
            while True:
                frame = await message_queue.get()
                if frame is None:
                    # reader 已结束（客户端断开），退出主循环
                    break
                if isinstance(frame, dict) and "__invalid__" in frame:
                    await _send_error("INVALID_MESSAGE", "消息解析失败", frame.get("__err__"))
                    continue

                # 普通对话消息（stop / confirm_reply 已由 dispatcher 处理）
                try:
                    msg = WSMessage(**frame)
                except Exception as e:
                    await _send_error("INVALID_MESSAGE", "消息验证失败", str(e))
                    continue

                # E12：会话归属校验（认证启用时，不能访问他人会话）
                if auth_user_id:
                    try:
                        from harness.modules.session_manager.service import (
                            SessionService,
                        )

                        session_svc = services.get(SessionService)
                        owned = session_svc.get_session(msg.session_id, user_id=auth_user_id)
                    except Exception as e:  # noqa: BLE001
                        await _send_error("SESSION_SERVICE_UNAVAILABLE", "会话服务不可用", str(e))
                        continue
                    if owned is None:
                        await _send_error(
                            "SESSION_NOT_FOUND",
                            f"会话不存在或无权访问: {msg.session_id}",
                        )
                        continue

                # 校验并展开附件引用（仅归属本会话的附件才会被采纳）
                att_metas = _resolve_attachment_metas(services, msg.session_id, msg.attachments)
                if msg.attachments and not att_metas:
                    # P1-1：安全语义不变（不阻断对话、不采纳跨会话附件），
                    # 但必须告知前端——否则用户以为附件已发出，模型却什么都没收到。
                    logger.warning(
                        "WS 附件引用无效或不属于本会话: session=%s refs=%s",
                        msg.session_id,
                        msg.attachments,
                    )
                    try:
                        await websocket.send_json(
                            {
                                "type": "attachment_warning",
                                "data": {
                                    "message": "附件已失效（不属于当前会话），本次提问未包含该文件，请重新上传。",
                                    "trace_id": trace_id,
                                },
                            }
                        )
                    except Exception:
                        pass

                # 获取 provider
                try:
                    from harness.modules.model_manager.provider_registry import (
                        ProviderRegistry,
                    )

                    provider_registry = services.get(ProviderRegistry)
                    provider = provider_registry.get_provider(msg.provider_id)
                except Exception as e:
                    # 光说「获取 provider 失败」用户无从下手：绝大多数情况是
                    # 桌面模式用了全新的数据目录（provider 表为空 / 未配 Key）。
                    # 这里把真实原因并进 message，前端只显示 message。
                    from harness.modules.model_manager.provider_registry import (
                        ProviderConfigError,
                        ProviderNotFoundError,
                    )

                    if isinstance(e, ProviderNotFoundError):
                        hint = f"（{e}）。请到「设置 → 模型 Provider」确认该 Provider 存在，或改选其它 Provider"
                    elif isinstance(e, ProviderConfigError):
                        hint = (
                            f"（{e}）。请到「设置 → 模型 Provider」填入 API Key 并启用；"
                            "桌面模式的数据目录与浏览器模式相互独立，需分别配置"
                        )
                    else:
                        hint = f"（{e}）"
                    logger.warning("获取 provider 失败: %s", e)
                    await _send_error("PROVIDER_ERROR", f"获取 provider 失败{hint}", str(e))
                    continue

                # 获取 EventBus（通过 PluginLoader.events 属性）
                event_bus: Any = None
                try:
                    from harness.kernel.loader import PluginLoader

                    loader = services.get(PluginLoader)
                    event_bus = loader.events
                except Exception:
                    pass

                # P2-2：此处原本会在 AgentLoop 之前预构建一次上下文快照。
                # 该时刻本轮消息尚未落库，产出的必然是「0/4096」空快照，
                # 随后一定被 AgentLoop 之后的最终快照覆盖 —— 属于纯粹的浪费
                # （build 需要拉全量消息 + token 计数 + 可能的压缩持久化，
                # 成本随会话长度线性增长）。已移除，只保留最终快照作为唯一来源。

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

                # 重新回答：取原 user 消息内容（仅用于运行标题），不新建 user 消息
                user_message_for_loop = msg.content
                if msg.parent_message_id:
                    try:
                        from harness.modules.session_manager.service import SessionService

                        _ss = services.get(SessionService)
                        _parent = _ss.get_message(msg.parent_message_id)
                        if _parent is None or _parent.role != "user":
                            await _send_error(
                                "INVALID_MESSAGE",
                                "重新回答的目标不是 user 消息或不存在",
                            )
                            continue
                        user_message_for_loop = _parent.content
                    except Exception as e:
                        await _send_error("SESSION_SERVICE_UNAVAILABLE", "会话服务不可用", str(e))
                        continue

                # 工具事件实时回调（S18: 替代循环结束后批量发送）
                async def _on_tool_event(event: dict[str, Any]) -> None:
                    await websocket.send_json(event)

                # 停止消息 / 确认答复由上面的 _dispatcher 统一处理（P0-4 单 reader 架构），
                # 不再有并发 receive_text 竞争。

                # 每连接的流式代理（S17: token 直连 WS + EventBus 旁路广播）
                #
                # P0-1：不再 monkey patch 共享的 provider 单例，改为构造代理实例，
                # 真实 provider 保持不可变，避免并发连接互相嵌套包装与跨会话串流。
                async def _emit_chunk(chunk: dict[str, Any]) -> None:
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
                                "data": {"reasoning_content": chunk["reasoning_content"]},
                            }
                        )

                stream_provider = _StreamingProviderProxy(provider, msg.session_id, _emit_chunk)

                try:
                    result = await loop.run(
                        session_id=msg.session_id,
                        user_message=user_message_for_loop,
                        provider=stream_provider,
                        model=msg.model,
                        budget=msg.budget,
                        attachments=att_metas,
                        on_tool_event=_on_tool_event,
                        confirm_callback=_request_confirm,
                        user_id=auth_user_id,
                        parent_message_id=msg.parent_message_id or None,
                    )
                finally:
                    current_loop = None
                    # 未决的确认请求一律按拒绝处理，避免协程永久挂起
                    _reject_pending_confirms()

                # 发送最终上下文快照：
                # AgentLoop 已持久化用户消息与 assistant 回复，
                # 重建上下文得到包含完整问答的准确快照。
                # （P2-2：原先开头那次预构建的 0/4096 快照已移除，此处为唯一来源。）
                try:
                    from harness.modules.context_manager.service import (
                        ContextService,
                    )

                    context_service = services.get(ContextService)
                    context_service.build(
                        msg.session_id,
                        budget=msg.budget,
                        model=msg.model or "gpt-4o",
                    )
                    final_snapshot = context_service.get_snapshot(msg.session_id)
                    if final_snapshot:
                        await websocket.send_json({"type": "context_snapshot", "data": final_snapshot})
                except Exception:
                    pass  # 上下文服务可能未注册

                # 发送错误或完成
                if result.error:
                    await _send_error("AGENT_LOOP_ERROR", result.error)
                else:
                    # 自动生成会话标题：首次提问后用 AI 总结
                    await _auto_generate_title(
                        msg.session_id,
                        msg.content,
                        provider,
                        msg.model,
                        trace_id=getattr(result, "trace_id", "") or "",
                    )

                    await websocket.send_json(
                        {
                            "type": "done",
                            "data": {
                                "content": result.content,
                                "iterations": result.iterations,
                                "latency_ms": result.latency_ms,
                                "short_circuited": result.short_circuited,
                                "usage": result.usage,
                                # 用 agent run 自己的 trace_id（与 spans 表一致），
                                # 而不是 WS 连接的 uuid4，否则前端若拿它去查轨迹会对不上。
                                "trace_id": getattr(result, "trace_id", "") or trace_id,
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
        finally:
            # 取消 reader / dispatcher，避免悬挂协程（二者通常已自行结束）
            for _t in (reader_task, dispatcher_task):
                _t.cancel()
                try:
                    await _t
                except (asyncio.CancelledError, Exception):
                    pass
