"""AgentLoop —— 核心对话循环。

装配上下文 → 调模型 → 解析 tool_calls → 执行 → 回填，直至终答。
钩子管线全量接入：pre/post_context_build、pre/post_model_call、pre/post_tool_call、pre_message_persist。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

from harness.engine.hook_types import (
    BuildContext,
    MessageRecord,
    ModelRequest,
    ToolCallContext,
)
from harness.engine.tool_registry import ToolNotFoundError, ToolRegistry
from harness.kernel.contracts.hook import HookContext
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry

logger = logging.getLogger("harness.agent_loop")

# 默认单轮最大工具迭代数
DEFAULT_MAX_TOOL_ITERATIONS = 10

# 模型调用重试配置
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY = 1.0  # 秒


class ProviderProtocol(Protocol):
    """Provider 协议（用于类型提示）。"""

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> Any: ...


@dataclass
class AgentLoopConfig:
    """AgentLoop 配置。"""

    model: str = "deepseek-v4-flash"
    budget: int = 4096
    max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS
    temperature: float = 0.7
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY
    system_prompt: str = ""


@dataclass
class AgentLoopResult:
    """AgentLoop 执行结果。"""

    content: str = ""
    tool_calls_made: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    latency_ms: int = 0
    error: str | None = None
    short_circuited: bool = False


class AgentLoop:
    """Agent 核心对话循环。

    依赖:
    - ServiceRegistry: 获取 SessionService / ContextService / ProviderRegistry
    - HookManager: 钩子管线
    - ToolRegistry: 工具注册表

    流程:
    1. hooks.pre_context_build → ContextService.build
    2. hooks.post_context_build
    3. hooks.pre_model_call → Provider.chat（带重试）
    4. 解析 response（content + tool_calls）
    5. hooks.post_model_call
    6. 若有 tool_calls:
       a. hooks.pre_tool_call → ToolRegistry.execute → hooks.post_tool_call
       b. 回填 tool 结果到消息
       c. 回到步骤 3（直到无 tool_calls 或达到最大迭代数）
    7. hooks.pre_message_persist → SessionService.append_message
    8. 返回终答
    """

    def __init__(
        self,
        services: ServiceRegistry,
        hooks: HookManager,
        tool_registry: ToolRegistry | None = None,
        config: AgentLoopConfig | None = None,
    ) -> None:
        self._services = services
        self._hooks = hooks
        self._tool_registry = tool_registry or ToolRegistry()
        self._config = config or AgentLoopConfig()
        self._stopped = False
        self._on_tool_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    @property
    def tool_registry(self) -> ToolRegistry:
        """工具注册表。"""
        return self._tool_registry

    def stop(self) -> None:
        """请求停止生成。"""
        self._stopped = True
        logger.info("AgentLoop 收到停止请求")

    @property
    def is_stopped(self) -> bool:
        """是否已请求停止。"""
        return self._stopped

    async def run(
        self,
        session_id: str,
        user_message: str,
        provider: Any,
        model: str | None = None,
        budget: int | None = None,
        on_tool_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AgentLoopResult:
        """执行一次完整的对话循环。

        Args:
            session_id: 会话 ID
            user_message: 用户输入
            provider: provider 实例（需有 chat 方法）
            model: 模型名称（不指定用配置默认值）
            budget: token 预算（不指定用配置默认值）
            on_tool_event: 工具事件回调（每个工具执行完成后实时调用）

        Returns:
            AgentLoopResult
        """
        start_time = time.time()
        self._stopped = False
        self._on_tool_event = on_tool_event
        result = AgentLoopResult()
        used_model = model or self._config.model
        used_budget = budget or self._config.budget
        iterations = 0

        try:
            # 追加用户消息
            await self._persist_message(
                session_id, role="user", content=user_message
            )

            current_messages: list[dict[str, Any]] = []

            while iterations < self._config.max_tool_iterations:
                if self._stopped:
                    result.short_circuited = True
                    break

                # 1. 装配上下文
                context_msgs = await self._build_context(
                    session_id, used_budget, used_model
                )
                current_messages = context_msgs

                # 2. 调用模型（带重试）
                response_content, response_tool_calls = await self._call_model(
                    provider, current_messages, used_model, session_id
                )

                if self._stopped:
                    result.short_circuited = True
                    break

                result.content = response_content
                iterations += 1

                # 3. 若无 tool_calls，终答
                if not response_tool_calls:
                    break

                # 3.5 持久化带 tool_calls 的 assistant 消息
                # DeepSeek/OpenAI API 要求：tool 消息前必须有带 tool_calls
                # 字段的 assistant 消息，且 tool_call_id 要匹配，否则 422。
                await self._persist_message(
                    session_id,
                    role="assistant",
                    content=response_content or "",
                    tool_calls=response_tool_calls,
                )

                # 4. 执行工具调用
                tool_results = await self._execute_tool_calls(
                    response_tool_calls, session_id
                )

                result.tool_calls_made.extend(tool_results)
            else:
                # 达到最大迭代数
                logger.warning(
                    "达到最大工具迭代数: %d", self._config.max_tool_iterations
                )

            # 持久化最终 assistant 终答消息（不带 tool_calls）
            if result.content and not result.short_circuited:
                await self._persist_message(
                    session_id,
                    role="assistant",
                    content=result.content,
                )

        except Exception as e:
            logger.error("AgentLoop 执行失败: %s", e)
            result.error = str(e)
            # 清理失败轮次残留的未完成消息：
            # 带有 tool_calls 但没有最终回答的 assistant 消息，
            # 以及对应的孤立 tool 消息。否则下次对话上下文会错乱。
            await self._cleanup_failed_round(session_id)

        result.iterations = iterations
        result.latency_ms = int((time.time() - start_time) * 1000)
        return result

    async def _build_context(
        self, session_id: str, budget: int, model: str
    ) -> list[dict[str, Any]]:
        """装配上下文（含钩子）。"""
        from harness.modules.context_manager.service import ContextService

        # pre_context_build 钩子
        build_ctx = BuildContext(
            session_id=session_id, budget=budget, model=model
        )
        pre_result = await self._hooks.execute(
            "pre_context_build",
            HookContext(
                hook_name="pre_context_build",
                data=build_ctx,
                session_id=session_id,
            ),
        )
        if pre_result.short_circuit:
            raise RuntimeError(
                f"pre_context_build 钩子短路: {pre_result.error}"
            )
        if pre_result.data is not None and isinstance(pre_result.data, BuildContext):
            build_ctx = pre_result.data
            # 使用钩子可能修改的 budget/model
            budget = build_ctx.budget
            model = build_ctx.model

        # 直接调用 ContextService.build
        context_service = self._services.get(ContextService)
        messages = cast(
            "list[dict[str, Any]]",
            context_service.build(session_id, budget=budget, model=model),
        )

        # 注入当前日期时间的系统提示（模型本身不知道当前日期）
        from datetime import datetime

        now = datetime.now()
        weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()]
        date_prompt = {
            "role": "system",
            "content": f"当前时间: {now.strftime('%Y年%m月%d日 %H:%M')} {weekday_cn}。",
        }
        # 插到 system 消息之后的最前面
        messages.insert(0, date_prompt)

        # 注入用户自定义系统提示词（来自会话级设置）
        if self._config.system_prompt:
            messages.insert(0, {
                "role": "system",
                "content": self._config.system_prompt,
            })

        # post_context_build 钩子
        build_ctx.messages = messages
        post_result = await self._hooks.execute(
            "post_context_build",
            HookContext(
                hook_name="post_context_build",
                data=build_ctx,
                session_id=session_id,
            ),
        )
        if post_result.data is not None and isinstance(
            post_result.data, BuildContext
        ):
            messages = post_result.data.messages or messages

        return messages

    async def _call_model(
        self,
        provider: Any,
        messages: list[dict[str, Any]],
        model: str,
        session_id: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        """调用模型并解析响应（带重试）。

        Returns:
            (content, tool_calls)
        """
        # 准备请求
        tool_defs = self._tool_registry.get_tool_definitions()
        model_request = ModelRequest(
            messages=messages,
            model=model,
            params={
                "temperature": self._config.temperature,
                **({"tools": tool_defs, "tool_choice": "auto"} if tool_defs else {}),
            },
        )

        # pre_model_call 钩子
        pre_result = await self._hooks.execute(
            "pre_model_call",
            HookContext(
                hook_name="pre_model_call",
                data=model_request,
                session_id=session_id,
            ),
        )
        if pre_result.short_circuit:
            raise RuntimeError(
                f"pre_model_call 钩子短路: {pre_result.error}"
            )
        if pre_result.data is not None and isinstance(pre_result.data, ModelRequest):
            model_request = pre_result.data

        # 调用 provider（带重试）
        last_error: Exception | None = None
        for attempt in range(self._config.max_retries):
            try:
                content_parts: list[str] = []
                # 流式 tool_calls 分片累积：DeepSeek/OpenAI API 会将一个
                # tool_call 的 arguments 拆成多个 SSE 块发送，必须按 index
                # 合并 name 和 arguments 片段，否则参数永远不完整。
                tool_calls_acc: dict[int, dict[str, Any]] = {}

                async for chunk in provider.chat(
                    messages=model_request.messages,
                    model=model_request.model,
                    stream=True,
                    **model_request.params,
                ):
                    if self._stopped:
                        break
                    if "delta" in chunk and chunk["delta"]:
                        content_parts.append(chunk["delta"])
                    if "tool_calls" in chunk:
                        for tc_frag in chunk["tool_calls"]:
                            idx = tc_frag.get("index", 0)
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            acc = tool_calls_acc[idx]
                            if tc_frag.get("id"):
                                acc["id"] = tc_frag["id"]
                            fn = tc_frag.get("function", {})
                            if fn.get("name"):
                                acc["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                acc["function"]["arguments"] += fn["arguments"]

                content = "".join(content_parts)
                all_tool_calls = [
                    tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())
                ]
                model_request.response = content
                model_request.tool_calls = all_tool_calls

                # post_model_call 钩子
                post_result = await self._hooks.execute(
                    "post_model_call",
                    HookContext(
                        hook_name="post_model_call",
                        data=model_request,
                        session_id=session_id,
                    ),
                )
                if post_result.data is not None and isinstance(
                    post_result.data, ModelRequest
                ):
                    content = post_result.data.response or content
                    all_tool_calls = post_result.data.tool_calls or all_tool_calls

                return content, all_tool_calls

            except Exception as e:
                last_error = e
                logger.warning(
                    "模型调用失败 (尝试 %d/%d): %s",
                    attempt + 1,
                    self._config.max_retries,
                    e,
                )
                if attempt < self._config.max_retries - 1:
                    delay = self._config.retry_base_delay * (2**attempt)
                    logger.info("等待 %.1f 秒后重试...", delay)
                    await asyncio.sleep(delay)

        # 所有重试都失败
        raise RuntimeError(
            f"模型调用失败（已重试 {self._config.max_retries} 次）: {last_error}"
        )

    async def _execute_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        session_id: str,
    ) -> list[dict[str, Any]]:
        """执行工具调用列表。"""
        results: list[dict[str, Any]] = []

        for tc in tool_calls:
            if self._stopped:
                break

            # 解析 tool_call 结构
            function = tc.get("function", tc)
            tool_name = function.get("name", "")
            args_str = function.get("arguments", "{}")

            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {}

            # pre_tool_call 钩子
            tool_ctx = ToolCallContext(tool_name=tool_name, args=args)
            pre_result = await self._hooks.execute(
                "pre_tool_call",
                HookContext(
                    hook_name="pre_tool_call",
                    data=tool_ctx,
                    session_id=session_id,
                ),
            )
            if pre_result.short_circuit:
                logger.warning(
                    "pre_tool_call 钩子短路: %s — %s",
                    tool_name,
                    pre_result.error,
                )
                results.append(
                    {
                        "tool_name": tool_name,
                        "args": args,
                        "result": "",
                        "error": pre_result.error or "短路",
                    }
                )
                continue

            if pre_result.data is not None and isinstance(
                pre_result.data, ToolCallContext
            ):
                tool_ctx = pre_result.data

            # 执行工具
            tool_result = ""
            tool_error: str | None = None

            try:
                tool = self._tool_registry.get(tool_ctx.tool_name)
                tool_result = await tool.execute(tool_ctx.args)
            except ToolNotFoundError:
                tool_error = f"工具未找到: {tool_ctx.tool_name}"
                logger.warning(tool_error)
            except Exception as e:
                tool_error = f"工具执行异常: {e}"
                logger.error(tool_error)

            tool_ctx.result = tool_result
            tool_ctx.error = tool_error

            # post_tool_call 钩子
            post_result = await self._hooks.execute(
                "post_tool_call",
                HookContext(
                    hook_name="post_tool_call",
                    data=tool_ctx,
                    session_id=session_id,
                ),
            )
            if post_result.data is not None and isinstance(
                post_result.data, ToolCallContext
            ):
                tool_ctx = post_result.data

            results.append(
                {
                    "tool_name": tool_ctx.tool_name,
                    "args": tool_ctx.args,
                    "result": tool_ctx.result,
                    "error": tool_ctx.error,
                }
            )

            # 实时推送工具事件回调
            if self._on_tool_event is not None:
                await self._on_tool_event(
                    {
                        "type": "tool_event",
                        "data": {
                            "tool_name": tool_ctx.tool_name,
                            "args": tool_ctx.args,
                            "result": tool_ctx.result,
                            "error": tool_ctx.error,
                        },
                    }
                )

            # 回填 tool 结果到会话消息（带 tool_call_id，DeepSeek API 要求）
            tool_call_id = tc.get("id", "")
            await self._persist_message(
                session_id,
                role="tool",
                content=tool_ctx.result or tool_ctx.error or "",
                tool_call_id=tool_call_id,
            )

        return results

    async def _cleanup_failed_round(self, session_id: str) -> None:
        """清理失败轮次残留的未完成消息。

        当 AgentLoop 异常中断时，可能已持久化了带 tool_calls 的
        assistant 消息和 tool 消息，但没有最终回答。这些残留消息会导致
        下次对话上下文错乱（AI 误以为要继续执行工具）。

        策略：删除会话末尾连续的 tool 消息和带 tool_calls 的
        assistant 消息（从最后一条往前删，直到遇到正常的
        user/assistant 消息）。
        """
        try:
            from harness.modules.session_manager.service import SessionService
            from harness.infra.repository import MessageRepository
            from harness.infra.database import Database

            session_service = self._services.get(SessionService)
            messages = session_service.list_messages(session_id)
            if not messages:
                return

            db = self._services.get(Database)
            repo = MessageRepository(db)

            # 从末尾往前删除残留消息
            deleted = 0
            for msg in reversed(messages):
                is_tool_msg = msg.role == "tool"
                has_tool_calls = bool(msg.tool_calls)
                # 删除孤立的 tool 消息和带 tool_calls 的 assistant 消息
                if is_tool_msg or has_tool_calls:
                    try:
                        db.execute(
                            "DELETE FROM messages WHERE id = ?", (msg.id,)
                        )
                        deleted += 1
                        logger.info("清理残留消息: id=%s role=%s", msg.id, msg.role)
                    except Exception as e:
                        logger.warning("删除消息失败: %s", e)
                        break
                else:
                    # 遇到正常的 user/assistant 消息，停止清理
                    break

            if deleted > 0:
                logger.info("失败轮次清理完成，共删除 %d 条残留消息", deleted)
        except Exception as e:
            logger.warning("清理失败轮次消息时出错: %s", e)

    async def _persist_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        """持久化消息（含 pre_message_persist 钩子）。"""
        from harness.modules.session_manager.service import SessionService

        record = MessageRecord(
            session_id=session_id,
            role=role,
            content=content,
            tool_calls=tool_calls or [],
            tool_call_id=tool_call_id,
        )

        # pre_message_persist 钩子
        pre_result = await self._hooks.execute(
            "pre_message_persist",
            HookContext(
                hook_name="pre_message_persist",
                data=record,
                session_id=session_id,
            ),
        )
        if pre_result.data is not None and isinstance(
            pre_result.data, MessageRecord
        ):
            record = pre_result.data
        if pre_result.short_circuit:
            logger.info("pre_message_persist 钩子短路，消息未持久化")
            return

        # 持久化
        session_service = self._services.get(SessionService)
        session_service.append_message(
            session_id=record.session_id,
            role=record.role,
            content=record.content,
            tool_calls=record.tool_calls,
            tool_call_id=record.tool_call_id,
            tokens=record.tokens,
            latency_ms=record.latency_ms,
        )
