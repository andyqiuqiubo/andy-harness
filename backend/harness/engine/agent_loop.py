"""AgentLoop —— 核心对话循环。

装配上下文 → 调模型 → 解析 tool_calls → 执行 → 回填，直至终答。
钩子管线全量接入：pre/post_context_build、pre/post_model_call、pre/post_tool_call、pre_message_persist。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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
    # 限定注入的 Skill 目录：None=全部启用；[]=不注入；[...] = 仅这些
    skill_allowlist: list[str] | None = None


@dataclass
class AgentLoopResult:
    """AgentLoop 执行结果。"""

    content: str = ""
    tool_calls_made: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    latency_ms: int = 0
    error: str | None = None
    short_circuited: bool = False
    usage: dict[str, Any] | None = None


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
        # 人工确认回调：(tool_name, args, risk, reason) -> 是否放行（None 表示接入层不支持确认）
        self._confirm_callback: (
            Callable[[str, dict[str, Any], str, str], Awaitable[bool]] | None
        ) = None

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
        confirm_callback: (
            Callable[[str, dict[str, Any], str, str], Awaitable[bool]] | None
        ) = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> AgentLoopResult:
        """执行一次完整的对话循环。

        Args:
            session_id: 会话 ID
            user_message: 用户输入
            provider: provider 实例（需有 chat 方法）
            model: 模型名称（不指定用配置默认值）
            budget: token 预算（不指定用配置默认值）
            on_tool_event: 工具事件回调（每个工具执行完成后实时调用）
            confirm_callback: 人工确认回调，签名
                `(tool_name, args, risk, reason) -> 是否放行`。
                未提供且策略要求确认时，按拒绝处理（安全默认）。

        Returns:
            AgentLoopResult
        """
        start_time = time.time()
        self._stopped = False
        self._on_tool_event = on_tool_event
        self._confirm_callback = confirm_callback
        result = AgentLoopResult()
        used_model = model or self._config.model
        used_budget = budget or self._config.budget
        iterations = 0
        trace_id = uuid.uuid4().hex[:16]

        # 暴露本轮运行环境（供 task/子代理等工具读取 provider/model）
        from harness.engine.runtime import AgentRuntime, reset_runtime, set_runtime

        _rt_token = set_runtime(
            AgentRuntime(
                provider=provider,
                model=used_model,
                session_id=session_id,
                services=self._services,
                hooks=self._hooks,
                tool_registry=self._tool_registry,
                budget=used_budget,
            )
        )

        try:
            # 追加用户消息
            await self._persist_message(
                session_id,
                role="user",
                content=user_message,
                attachments=attachments,
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
                response_content, response_tool_calls, call_usage = await self._call_model(
                    provider, current_messages, used_model, session_id, trace_id
                )
                # 累积所有模型调用的 token 用量（工具迭代可能调用多次）
                result.usage = self._merge_usage(result.usage, call_usage)

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
                    response_tool_calls, session_id, trace_id
                )

                result.tool_calls_made.extend(tool_results)
            else:
                # 达到最大迭代数：强制一次「无工具」收尾调用，
                # 让模型基于已获取的信息给出最终回答。
                # 否则会话将停在悬空的工具结果上、没有任何终答
                # （表现为任务/对话「执行成功」却看不到结果）。
                logger.warning(
                    "达到最大工具迭代数: %d，强制无工具收尾",
                    self._config.max_tool_iterations,
                )
                if not self._stopped:
                    context_msgs = await self._build_context(
                        session_id, used_budget, used_model
                    )
                    wrapup_msgs = list(context_msgs)
                    wrapup_msgs.append(
                        {
                            "role": "user",
                            "content": (
                                "（系统提示）已达到工具调用次数上限，"
                                "禁止再调用任何工具。请立即基于以上已获取的"
                                "全部信息，直接输出最终回答。"
                            ),
                        }
                    )
                    try:
                        wrapup_content, _ignored, wrapup_usage = (
                            await self._call_model(
                                provider,
                                wrapup_msgs,
                                used_model,
                                session_id,
                                trace_id,
                                allow_tools=False,
                            )
                        )
                        if wrapup_usage:
                            result.usage = self._merge_usage(
                                result.usage, wrapup_usage
                            )
                        if wrapup_content:
                            result.content = wrapup_content
                    except Exception as e:  # noqa: BLE001
                        # 收尾失败不视为整体失败（前 N 轮工具结果已落库）
                        logger.warning("强制收尾调用失败: %s", e)

            # 持久化最终 assistant 终答消息（不带 tool_calls）
            if result.content and not result.short_circuited:
                await self._persist_message(
                    session_id,
                    role="assistant",
                    content=result.content,
                    tokens=(
                        result.usage.get("total_tokens", 0)
                        if result.usage else 0
                    ),
                )

        except Exception as e:
            logger.error("AgentLoop 执行失败: %s", e)
            result.error = str(e)
            # 清理失败轮次残留的未完成消息：
            # 带有 tool_calls 但没有最终回答的 assistant 消息，
            # 以及对应的孤立 tool 消息。否则下次对话上下文会错乱。
            await self._cleanup_failed_round(session_id)
        finally:
            # 无论正常结束、异常还是被取消（CancelledError 属 BaseException，
            # 上面的 except 捕获不到）都要复位运行环境，避免 ContextVar 泄漏
            reset_runtime(_rt_token)

        result.iterations = iterations
        result.latency_ms = int((time.time() - start_time) * 1000)

        # 记录一次运行的 trace span（可观测性；无 tracing 订阅者时开销极小）
        await self._emit_span(
            trace_id=trace_id,
            parent_id=None,
            session_id=session_id,
            name="agent_run",
            kind="run",
            status="error" if result.error else "ok",
            duration_ms=result.latency_ms,
            iterations=result.iterations,
            total_tokens=(result.usage or {}).get("total_tokens", 0),
            model=used_model,
            error=result.error or "",
        )
        return result

    async def _emit_span(self, **fields: Any) -> None:
        """把一次 span 发布到事件总线（topic: trace.span）。

        优雅降级：EventBus 未注册或无 tracing 订阅者时静默跳过，
        绝不影响主对话流程。
        """
        try:
            from harness.kernel.eventbus import EventBus

            if not self._services.has(EventBus):
                return
            bus: EventBus = self._services.get(EventBus)
            await bus.publish("trace.span", fields)
        except Exception as e:  # noqa: BLE001
            logger.debug("发送 trace span 失败（已忽略）: %s", e)

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

        # 前置系统消息：用户自定义提示词 → 当前时间 → Skill 目录（L1）
        # 顺序即优先级，全部放在会话历史之前
        prefix_messages: list[dict[str, Any]] = []

        # 注入用户自定义系统提示词（来自会话级设置）
        if self._config.system_prompt:
            prefix_messages.append(
                {"role": "system", "content": self._config.system_prompt}
            )

        # 注入当前日期时间的系统提示（模型本身不知道当前日期）
        from datetime import datetime

        now = datetime.now()
        weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()]
        prefix_messages.append(
            {
                "role": "system",
                "content": f"当前时间: {now.strftime('%Y年%m月%d日 %H:%M')} {weekday_cn}。",
            }
        )

        # 注入 Skill 目录（L1：仅 name + description，按需加载）
        skill_catalog = self._render_skill_catalog()
        if skill_catalog:
            prefix_messages.append({"role": "system", "content": skill_catalog})

        # 注入长期记忆（跨会话的偏好 / 事实，最近若干条）
        memory_hint = self._render_memory_hint()
        if memory_hint:
            prefix_messages.append({"role": "system", "content": memory_hint})

        messages = prefix_messages + messages

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

    def _check_permission(self, tool_name: str) -> Any | None:
        """查询权限服务决策。

        服务未注册时返回 None（视为放行），保证不含权限插件时行为不变。
        """
        try:
            from harness.modules.permission_manager.service import PermissionService

            if not self._services.has(PermissionService):
                return None
            service: PermissionService = self._services.get(PermissionService)
            return service.decide(tool_name)
        except Exception as e:
            logger.debug("权限检查失败（已放行）: %s", e)
            return None

    async def _request_confirm(
        self, tool_name: str, args: dict[str, Any], risk: str, reason: str
    ) -> bool:
        """请求人工确认。

        接入层未提供回调时返回 False —— 需要确认却无人可问，按拒绝处理。
        """
        if self._confirm_callback is None:
            logger.warning(
                "工具 %s 需人工确认，但接入层未提供确认回调，已按拒绝处理",
                tool_name,
            )
            return False
        try:
            return bool(
                await self._confirm_callback(tool_name, args, risk, reason)
            )
        except Exception as e:
            logger.error("人工确认流程异常，已按拒绝处理: %s", e)
            return False

    def _render_skill_catalog(self) -> str:
        """渲染 Skill 目录（L1）作为系统提示。

        优雅降级：Skill 服务不可用时返回空串，不影响主对话流程。
        """
        try:
            from harness.modules.skill_manager.service import SkillService

            if not self._services.has(SkillService):
                return ""
            service: SkillService = self._services.get(SkillService)
            return service.render_catalog(names=self._config.skill_allowlist)
        except Exception as e:
            logger.debug("渲染 Skill 目录失败（已降级跳过）: %s", e)
            return ""

    def _render_memory_hint(self) -> str:
        """渲染长期记忆片段作为系统提示。

        优雅降级：记忆服务不可用或无记忆时返回空串。
        """
        try:
            from harness.modules.memory_manager.service import MemoryService

            if not self._services.has(MemoryService):
                return ""
            service: MemoryService = self._services.get(MemoryService)
            return service.render_hint()
        except Exception as e:
            logger.debug("渲染长期记忆失败（已降级跳过）: %s", e)
            return ""

    @staticmethod
    def _merge_usage(
        total: dict[str, Any] | None, new: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """合并两次模型调用的 usage（工具迭代时需累加）。"""
        if not new:
            return total
        if total is None:
            total = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "prompt_tokens_details": {},
            }
        total["prompt_tokens"] = total.get("prompt_tokens", 0) + new.get(
            "prompt_tokens", 0
        )
        total["completion_tokens"] = total.get("completion_tokens", 0) + new.get(
            "completion_tokens", 0
        )
        total["total_tokens"] = total.get("total_tokens", 0) + new.get(
            "total_tokens", 0
        )
        new_details = new.get("prompt_tokens_details") or {}
        total_details: dict[str, Any] = total.setdefault("prompt_tokens_details", {})
        for key in (
            "cached_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        ):
            if key in new_details:
                total_details[key] = total_details.get(key, 0) + new_details[key]
        return total

    async def _call_model(
        self,
        provider: Any,
        messages: list[dict[str, Any]],
        model: str,
        session_id: str = "",
        trace_id: str = "",
        allow_tools: bool = True,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
        """调用模型并解析响应（带重试）。

        Args:
            allow_tools: False 时不注入工具定义（用于撞迭代上限后的
                强制收尾调用，逼模型只能输出文字终答）。

        Returns:
            (content, tool_calls, usage)
        """
        # 准备请求
        tool_defs = (
            self._tool_registry.get_tool_definitions() if allow_tools else []
        )
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
                usage: dict[str, Any] | None = None
                # 流式 tool_calls 分片累积：DeepSeek/OpenAI API 会将一个
                # tool_call 的 arguments 拆成多个 SSE 块发送，必须按 index
                # 合并 name 和 arguments 片段，否则参数永远不完整。
                tool_calls_acc: dict[int, dict[str, Any]] = {}

                call_start = time.time()
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
                    # 最后一个流式块携带 usage（token 用量统计）
                    if "usage" in chunk and chunk["usage"]:
                        usage = chunk["usage"]

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

                await self._emit_span(
                    trace_id=trace_id,
                    parent_id=trace_id or None,
                    session_id=session_id,
                    name="model_call",
                    kind="model",
                    status="ok",
                    duration_ms=int((time.time() - call_start) * 1000),
                    prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                    completion_tokens=(usage or {}).get("completion_tokens", 0),
                    total_tokens=(usage or {}).get("total_tokens", 0),
                    output_preview=content[:200],
                    tool_calls=",".join(
                        tc.get("function", {}).get("name", "")
                        for tc in all_tool_calls
                    ),
                )

                return content, all_tool_calls, usage

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
        trace_id: str = "",
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

            # 权限校验：拒绝 / 需人工确认 / 放行
            # 注意：被拒绝时也必须以错误形式回填 tool 消息，
            # 否则会话末尾会留下"带 tool_calls 却无对应 tool 响应"的
            # assistant 消息，下一轮请求会被 API 以 400 拒绝。
            blocked_error: str | None = None
            permission_decision = self._check_permission(tool_ctx.tool_name)
            if permission_decision is not None and permission_decision.denied:
                blocked_error = f"权限拒绝: {permission_decision.reason}"
                logger.warning(
                    "工具调用被权限拒绝: %s — %s",
                    tool_ctx.tool_name,
                    permission_decision.reason,
                )
            elif permission_decision is not None and permission_decision.needs_confirm:
                approved = await self._request_confirm(
                    tool_ctx.tool_name,
                    tool_ctx.args,
                    permission_decision.risk,
                    permission_decision.reason,
                )
                if not approved:
                    blocked_error = "用户拒绝执行该工具调用"
                    logger.info("用户拒绝工具调用: %s", tool_ctx.tool_name)

            # 执行工具
            tool_start = time.time()
            tool_result = ""
            tool_error: str | None = None

            if blocked_error is not None:
                tool_error = blocked_error
            else:
                try:
                    tool = self._tool_registry.get(tool_ctx.tool_name)
                    # 需要会话上下文的工具：注入 session_id（不覆盖已有值）
                    if getattr(tool, "needs_session", False) and "session_id" not in (
                        tool_ctx.args or {}
                    ):
                        tool_ctx.args = {**tool_ctx.args, "session_id": session_id}
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

            await self._emit_span(
                trace_id=trace_id,
                parent_id=trace_id or None,
                session_id=session_id,
                name=f"tool:{tool_ctx.tool_name}",
                kind="tool",
                status="error" if tool_ctx.error else "ok",
                duration_ms=int((time.time() - tool_start) * 1000),
                input_preview=json.dumps(
                    tool_ctx.args, ensure_ascii=False, default=str
                )[:200],
                output_preview=(tool_ctx.result or "")[:200],
                error=tool_ctx.error or "",
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
            from harness.infra.database import Database
            from harness.modules.session_manager.service import SessionService

            session_service = self._services.get(SessionService)
            messages = session_service.list_messages(session_id)
            if not messages:
                return

            db = self._services.get(Database)

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
        tokens: int = 0,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """持久化消息（含 pre_message_persist 钩子）。"""
        from harness.modules.session_manager.service import SessionService

        record = MessageRecord(
            session_id=session_id,
            role=role,
            content=content,
            tool_calls=tool_calls or [],
            tool_call_id=tool_call_id,
            tokens=tokens,
            attachments=attachments,
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
            attachments=record.attachments,
        )
