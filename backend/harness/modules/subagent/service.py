"""子代理服务 —— 在独立上下文中运行隔离子任务，只回传摘要。

要点：
- 子代理在**独立的临时会话**里跑（用完即删），因此不会污染主会话上下文；
- 复用现有 `AgentLoop`（含上下文压缩、钩子、权限、offload 等全部能力）；
- 子代理的工具集默认排除 `task` 自身，避免无限递归派生；
- 主代理只拿到子代理的**最终回答**，拿不到子代理的中间消息。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from harness.engine.agent_loop import AgentLoop, AgentLoopConfig
from harness.engine.runtime import get_runtime
from harness.engine.tool_registry import ToolRegistry
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry

# 子代理不可用的工具（避免递归派生 / 干扰主会话规划）
# E10：parallel / pipeline 同样会派生代理，一并排除防无限递归。
_EXCLUDED_TOOLS = {"task", "parallel", "pipeline"}

DEFAULT_MAX_ITERATIONS = 6


@dataclass
class SubagentResult:
    """子代理执行结果（只含摘要与统计）。"""

    content: str = ""
    iterations: int = 0
    tool_calls: int = 0
    usage: dict[str, Any] | None = None
    error: str | None = None


class SubagentService(ABC):
    """子代理服务接口。"""

    @abstractmethod
    async def run_task(
        self,
        prompt: str,
        *,
        provider: Any = None,
        model: str | None = None,
        allow_tools: list[str] | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
    ) -> SubagentResult:
        """运行一个隔离子任务并返回摘要。"""


class SubagentServiceImpl(SubagentService):
    """基于现有 AgentLoop 的子代理实现。"""

    def __init__(
        self,
        services: ServiceRegistry,
        hooks: HookManager | None = None,
    ) -> None:
        self._services = services
        self._hooks = hooks or HookManager()

    @property
    def services(self) -> ServiceRegistry:
        return self._services

    def _build_registry(self, allow_tools: list[str] | None, base: ToolRegistry | None) -> ToolRegistry:
        """构建子代理的受限工具集。"""
        registry = ToolRegistry()
        source = base
        if source is None:
            try:
                source = self._services.get(ToolRegistry)
            except Exception:
                source = None
        if source is None:
            return registry

        allowed = set(allow_tools) if allow_tools else None
        for tool in source.all_tools():
            name = getattr(tool, "tool_name", "")
            if not name or name in _EXCLUDED_TOOLS:
                continue
            if allowed is not None and name not in allowed:
                continue
            registry.register(tool, owner="subagent")
        return registry

    async def run_task(
        self,
        prompt: str,
        *,
        provider: Any = None,
        model: str | None = None,
        allow_tools: list[str] | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
    ) -> SubagentResult:
        runtime = get_runtime()

        used_provider = provider or (runtime.provider if runtime else None)
        if used_provider is None:
            return SubagentResult(error="无法确定模型 provider（不在 AgentLoop 运行环境中）")

        services = (runtime.services if runtime else None) or self._services
        hooks = (runtime.hooks if runtime else None) or self._hooks
        used_model = model or (runtime.model if runtime else None) or "gpt-4o"
        budget = runtime.budget if runtime else 4096

        tool_registry = self._build_registry(allow_tools, runtime.tool_registry if runtime else None)

        # 独立临时会话（用完即删），保证主上下文干净
        from harness.modules.session_manager.service import SessionService

        try:
            session_service = services.get(SessionService)
        except Exception:
            return SubagentResult(error="会话服务不可用，无法创建子代理上下文")

        # E12：子代理会话继承父会话归属用户，保证数据隔离与记忆按用户。
        owner_id = ""
        parent_id = runtime.session_id if runtime else ""
        if parent_id:
            parent_session = session_service.get_session(parent_id)
            if parent_session is not None:
                owner_id = getattr(parent_session, "user_id", "") or ""

        child_title = f"[子代理] {prompt.strip()[:40] or 'task'}"
        child = session_service.create_session(title=child_title, user_id=owner_id)

        config = AgentLoopConfig(
            model=used_model,
            max_tool_iterations=max(1, min(max_iterations, 12)),
            budget=budget,
        )
        loop = AgentLoop(
            services=services,
            hooks=hooks,
            tool_registry=tool_registry,
            config=config,
        )
        try:
            result = await loop.run(
                session_id=child.id,
                user_message=prompt,
                provider=used_provider,
                model=used_model,
                budget=budget,
                user_id=owner_id,
            )
            return SubagentResult(
                content=result.content or "",
                iterations=result.iterations,
                tool_calls=len(result.tool_calls_made),
                usage=result.usage,
                error=result.error,
            )
        finally:
            # 清理子代理会话，不留痕迹
            try:
                session_service.delete_session(child.id)
            except Exception:
                pass
