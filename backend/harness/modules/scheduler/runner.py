"""定时任务的执行器 —— 在隔离会话里、用受限工具集跑一段提示词。

设计要点：
- **工具最小化**：只开放任务里勾选的工具（内置工具 + 指定 MCP 服务器的工具 + 指定 Skill），
  未勾选的一律不进入注册表，避免无人值守时误用危险工具。
- **预先授权**：定时任务是用户显式配置的，因此运行期对「已勾选工具」自动放行确认
  （未勾选的工具根本不在注册表里，不受影响）。
- **隔离会话**：每次运行新建会话（标题带任务名与时间），便于事后查看完整过程。
- **Skill 限定**：通过 `AgentLoopConfig.skill_allowlist` 只把勾选的 Skill 目录注入上下文，
  并用受限 `use_skill` 包装器挡住未勾选的 Skill。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from harness.engine.agent_loop import AgentLoop, AgentLoopConfig
from harness.engine.tool_registry import ToolRegistry
from harness.kernel.contracts.tool import ToolPlugin
from harness.kernel.hooks import HookManager
from harness.modules.scheduler.service import (
    ScheduledTask,
    SchedulerService,
    _now_local,
    _to_iso,
)

logger = logging.getLogger("harness.scheduler.runner")

DEFAULT_MAX_ITERATIONS = 8
DEFAULT_TIMEOUT_SECONDS = 300
_MCP_PREFIX = "mcp__"


@dataclass
class RunOutcome:
    status: str  # ok | error | skipped
    session_id: str = ""
    summary: str = ""
    error: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "session_id": self.session_id,
            "summary": self.summary,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


class ScopedUseSkillTool(ToolPlugin):
    """把 use_skill 包一层，只允许加载任务里勾选的 Skill。"""

    def __init__(self, inner: Any, allowed: list[str]) -> None:
        self._inner = inner
        self._allowed = {str(n) for n in allowed if str(n).strip()}

    @property
    def tool_name(self) -> str:
        return str(getattr(self._inner, "tool_name", "use_skill"))

    @property
    def description(self) -> str:
        return str(getattr(self._inner, "description", ""))

    @property
    def parameters_schema(self) -> dict[str, Any]:
        schema = getattr(self._inner, "parameters_schema", None)
        return schema if isinstance(schema, dict) else {}

    @property
    def risk_level(self) -> str:
        return str(getattr(self._inner, "risk_level", "read"))

    @property
    def needs_session(self) -> bool:
        return bool(getattr(self._inner, "needs_session", False))

    async def execute(self, args: dict[str, Any]) -> str:
        name = str(args.get("name") or "").strip()
        if name and name not in self._allowed:
            allowed = "、".join(sorted(self._allowed)) or "（无）"
            return f"错误: 本定时任务只允许使用这些 Skill：{allowed}。不允许加载 '{name}'。"
        result = await self._inner.execute(args)
        return result if isinstance(result, str) else str(result)


class TaskRunner:
    """执行一个定时任务。"""

    def __init__(self, services: Any) -> None:
        self._services = services

    # ── 依赖解析（延迟） ──────────────────────────────

    @property
    def _providers(self) -> Any:
        try:
            from harness.modules.model_manager.provider_registry import ProviderRegistry

            return self._services.get(ProviderRegistry)
        except Exception:  # noqa: BLE001
            return None

    @property
    def _sessions(self) -> Any:
        from harness.modules.session_manager.service import SessionService

        return self._services.get(SessionService)

    @property
    def _global_registry(self) -> ToolRegistry:
        try:
            registry = self._services.get(ToolRegistry)
        except Exception:  # noqa: BLE001
            return ToolRegistry()
        return registry if isinstance(registry, ToolRegistry) else ToolRegistry()

    def _resolve_provider(self, task: ScheduledTask) -> tuple[Any, str] | None:
        registry = self._providers
        if registry is None:
            return None
        try:
            candidates = [p for p in registry.list_providers() if p.get("enabled", True) and p.get("has_api_key")]
        except Exception as e:  # noqa: BLE001
            logger.warning("列举 provider 失败: %s", e)
            return None
        if not candidates:
            return None

        target = None
        if task.provider_id:
            target = next((p for p in candidates if p.get("id") == task.provider_id), None)
        if target is None:
            target = next(
                (p for p in candidates if "deepseek" in str(p.get("id", ""))),
                candidates[0],
            )
        try:
            provider = registry.get_provider(target["id"])
        except Exception as e:  # noqa: BLE001
            logger.warning("获取 provider 失败: %s", e)
            return None
        models = target.get("models") or []
        model = task.model or (models[0] if models else "deepseek-v4-flash")
        return provider, model

    # ── 受限工具集 ────────────────────────────────────

    def build_registry(self, task: ScheduledTask) -> tuple[ToolRegistry, list[str]]:
        """按任务配置构建工具集，返回 (注册表, 允许的工具名)。"""
        registry = ToolRegistry()
        allowed: list[str] = []
        selected_tools = {str(t) for t in task.tools}
        selected_servers = {str(s) for s in task.mcp_servers}

        for tool in self._global_registry.all_tools():
            name = str(getattr(tool, "tool_name", "") or "")
            if not name:
                continue

            if name.startswith(_MCP_PREFIX):
                parts = name.split("__", 2)
                server = parts[1] if len(parts) > 2 else ""
                if server in selected_servers:
                    registry.register(tool, owner="schedule")
                    allowed.append(name)
                continue

            if name == "use_skill":
                if task.skills:
                    registry.register(ScopedUseSkillTool(tool, task.skills), owner="schedule")
                    allowed.append(name)
                continue

            if name in selected_tools:
                registry.register(tool, owner="schedule")
                allowed.append(name)

        return registry, allowed

    def _system_prompt(self, task: ScheduledTask) -> str:
        lines = [
            "你是被「定时任务」自动触发的助手，用户当前不在场，请直接完成任务并给出结果。",
            f"任务名称：{task.name}",
        ]
        if task.description:
            lines.append(f"任务说明：{task.description}")
        lines.append(f"当前时间：{_to_iso(_now_local())}")
        if task.mcp_servers:
            lines.append(f"可用 MCP 数据源：{'、'.join(task.mcp_servers)}")
        lines.append("完成后请给出**简明的结果摘要**（会记录到任务的运行历史中供用户查看）。")
        return "\n".join(lines)

    # ── 执行 ──────────────────────────────────────────

    async def run_task(self, task: ScheduledTask, trigger: str = "schedule") -> RunOutcome:
        scheduler: SchedulerService = self._services.get(SchedulerService)
        run_id = scheduler.start_run(task.id, trigger)
        started = time.time()
        outcome = RunOutcome(status="error")

        try:
            resolved = self._resolve_provider(task)
            if resolved is None:
                outcome.status = "skipped"
                outcome.error = "没有可用的大模型 provider（需已启用且配置 API Key）"
            else:
                provider, model = resolved
                registry, allowed = self.build_registry(task)
                session_title = f"⏱ {task.name} · {_to_iso(_now_local())[:16].replace('T', ' ')}"
                session = self._sessions.create_session(title=session_title)
                outcome.session_id = session.id

                config = AgentLoopConfig(
                    model=model,
                    max_tool_iterations=max(1, min(int(task.max_iterations or 8), 20)),
                    system_prompt=self._system_prompt(task),
                    skill_allowlist=list(task.skills),
                )
                hook_manager = self._services.get(HookManager) if self._services.has(HookManager) else HookManager()
                loop = AgentLoop(
                    services=self._services,
                    hooks=hook_manager,
                    tool_registry=registry,
                    config=config,
                )

                async def _auto_approve(tool_name: str, args: dict[str, Any], risk: str, reason: str) -> bool:
                    # 预授权：注册表里只有任务勾选的工具
                    return tool_name in allowed

                timeout = max(30, int(task.timeout_seconds or DEFAULT_TIMEOUT_SECONDS))
                try:
                    result = await asyncio.wait_for(
                        loop.run(
                            session_id=session.id,
                            user_message=task.prompt,
                            provider=provider,
                            model=model,
                            confirm_callback=_auto_approve,
                        ),
                        timeout=timeout,
                    )
                except TimeoutError:
                    outcome.status = "error"
                    outcome.error = f"运行超时（>{timeout}s）"
                else:
                    if result.error:
                        outcome.status = "error"
                        outcome.error = result.error
                    else:
                        outcome.status = "ok"
                    outcome.summary = (result.content or "").strip()
                    if outcome.status == "ok" and not outcome.summary:
                        # 正常结束却无终答（如收尾调用也未能产出文本）
                        outcome.summary = (
                            f"运行完成（{result.iterations} 次迭代）但未生成最终回答，请查看会话中的工具执行记录。"
                        )
        except Exception as e:  # noqa: BLE001
            logger.exception("定时任务执行异常: %s", task.name)
            outcome.status = "error"
            outcome.error = f"{type(e).__name__}: {e}"
        finally:
            outcome.duration_ms = int((time.time() - started) * 1000)
            try:
                scheduler.finish_run(
                    run_id,
                    status=outcome.status,
                    duration_ms=outcome.duration_ms,
                    session_id=outcome.session_id,
                    summary=outcome.summary,
                    error=outcome.error,
                )
                scheduler.record_task_result(task, status=outcome.status, error=outcome.error)
            except Exception as e:  # noqa: BLE001
                logger.warning("记录任务运行结果失败: %s", e)

        return outcome
