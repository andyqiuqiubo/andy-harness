"""Computer Use 插件入口。

职责：
- 以 ToolPlugin 形式向主 Agent 暴露 `computer_use` 工具（主 Agent 可把
  「打开记事本输入 hello」这类任务委派给它）。
- 注册 ComputerUseService（持有配置/安全策略/桌面控制器/审批状态）。
- 在 interactive 模式下，提供人工授权 REST 接口：
  POST /api/computer-use/approve?run_id=xxx  （授权某次任务）
  GET  /api/computer-use/pending            （查看待审批任务）
  人工授权后，调用 computer_use(resume_run_id=xxx) 继续。

所有可变参数通过 plugin.json config_schema + 环境变量注入（见 config.py），
不在代码中硬编码密钥或模型名。
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable
from typing import Any

from harness.engine.tool_registry import ToolRegistry
from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

from .agent import ComputerUseAgent
from .client import DeepSeekClient
from .config import ComputerUseConfig, load_computer_use_config
from .desktop import RealDesktopController
from .safety import SafetyPolicy

logger = logging.getLogger("harness.computer_use")


class ComputerUseService:
    """插件级服务：配置、安全策略、桌面控制器、审批状态。"""

    def __init__(
        self,
        config: ComputerUseConfig,
        credential_resolver: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        self.config = config
        # 运行时凭据兜底：从项目 ProviderRegistry 取 deepseek provider 的
        # (api_key, base_url)，解决「密钥存于数据库、进程内无 DEEPSEEK_API_KEY
        # 环境变量」导致 computer_use 一直报「未配置 API Key」的问题。
        self.credential_resolver = credential_resolver
        self.safety = SafetyPolicy(config)
        self.desktop = RealDesktopController()
        # 待审批任务：run_id -> {signature, reason, created_at}
        self.pending: dict[str, dict[str, Any]] = {}
        # 已授权任务集合
        self.approved_runs: set[str] = set()
        # run_id -> 原始目标（用于 resume 续跑）
        self.goals: dict[str, str] = {}

        # 确保工作目录存在（与真实数据隔离的沙箱工作区）
        try:
            os.makedirs(self.config.working_dir, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("Computer Use 工作目录创建失败: %s", e)

    def create_agent(self, run_id: str, run_approved: bool) -> ComputerUseAgent:
        """构造一次任务的 Agent 实例（懒加载真实模型客户端）。"""
        api_key = self.config.api_key
        base_url = self.config.base_url
        # 运行时兜底：复用项目「设置页」中已配置的 DeepSeek provider 凭据
        # （密钥存于数据库，进程内没有 DEEPSEEK_API_KEY 这类环境变量）。
        if (not api_key or base_url == "https://api.deepseek.com") and self.credential_resolver:
            pkey, purl = self.credential_resolver()
            if not api_key and pkey:
                api_key = pkey
            if base_url == "https://api.deepseek.com" and purl:
                base_url = purl
        if not api_key:
            raise ValueError(
                "未配置 Computer Use 的 API Key：请任选其一设置——\n"
                "1) 插件配置或环境变量 COMPUTER_USE_API_KEY；\n"
                "2) 环境变量 DEEPSEEK_API_KEY；\n"
                "3) 在「设置页」先配置好 DeepSeek provider 的 API Key（将自动复用）。"
            )
        client = DeepSeekClient(
            api_key=api_key,
            base_url=base_url,
            model=self.config.model,
            timeout=self.config.http_timeout,
            max_retries=self.config.max_retries,
        )
        return ComputerUseAgent(
            config=self.config,
            safety=self.safety,
            desktop=self.desktop,
            client=client,
            service=self,
            run_approved=run_approved,
        )

    # ── 审批状态管理（供 REST 接口 / 工具续跑使用） ──
    def mark_pending(self, run_id: str, signature: str, reason: str) -> None:
        """记录一个待人工授权的任务。"""
        self.pending[run_id] = {
            "signature": signature,
            "reason": reason,
            "created_at": time.time(),
        }

    def approve_run(self, run_id: str) -> bool:
        """人工授权某次任务；返回是否成功（run 是否存在无关，仅记录授权）。"""
        self.approved_runs.add(run_id)
        self.pending.pop(run_id, None)
        logger.info("Computer Use 任务已获人工授权: run_id=%s", run_id)
        return True

    def list_pending(self) -> list[dict[str, Any]]:
        """列出所有待审批任务。"""
        return [{"run_id": k, **v} for k, v in self.pending.items()]


class ComputerUseTool(ToolPlugin):
    """暴露给主 Agent 的 computer_use 工具。"""

    def __init__(self, service: ComputerUseService) -> None:
        self._service = service

    @property
    def tool_name(self) -> str:
        return "computer_use"

    @property
    def risk_level(self) -> str:
        # 该工具可操作真实桌面/执行命令，属于最高风险等级
        return "dangerous"

    @property
    def description(self) -> str:
        return (
            "Computer Use（计算机操作）能力：让模型自主操作本机桌面来完成任务，"
            "例如「打开记事本并输入 hello」「在桌面上截个图」「在工作目录里新建一个文件」。"
            "该工具会自主截图观察、移动鼠标、输入键盘、读写受控目录文件、执行受控命令，"
            "内置安全围栏（白名单或人工授权、危险命令/受限目录拦截、超时与截断）。\n"
            "【使用时机·省钱纪律】本工具内部会多次调用 LLM（规划+逐步执行+总结），"
            "成本高、耗时长。**纯文件读写、计算、代码执行类任务请直接用 code_runner "
            "等轻量工具完成，不要调用本工具**；本工具只用于确实需要 GUI 交互"
            "（鼠标点击、键盘操作界面程序、截屏观察）的场景。**同一任务不要把本工具"
            "与 code_runner 并行调用或先后重复执行**——二选一，避免两条路重复干活"
            "（重复创建/修改同一文件、重复消耗 token）。\n"
            "参数 goal 为任务目标（中文）；resume_run_id 用于 interactive 模式下"
            "人工授权后续跑（留空表示新任务）。\n"
            "【转述纪律·务必遵守】本工具返回的是『本机桌面操作的真实执行结果』，"
            "其中可能以状态头声明『本机桌面自动化已实际执行（鼠标/键盘/程序已真实操作）』。"
            "请如实、完整地把返回内容转述给用户，**绝对不要**自行推断或声称"
            "『我/本工具无法操控本机 Chrome』『桌面自动化不可用』『图形会话/命令通道受限』"
            "『无法执行 start chrome』等结论——除非返回文本中明确含有『[桌面不可用]』或"
            "『未配置 API Key』字样。若本工具已实际打开/操作了程序，**请勿改用联网搜索"
            "替代，也不要再主动给出 Playwright/Selenium 脚本方案**，直接转述其执行结果即可。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "任务目标（中文，尽量明确可执行）",
                },
                "resume_run_id": {
                    "type": "string",
                    "description": "可选：interactive 模式下人工授权后的续跑任务 ID",
                },
            },
            "required": ["goal"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        goal = (args.get("goal") or "").strip()
        resume = (args.get("resume_run_id") or "").strip()

        if resume:
            # 续跑：复用历史目标，并依据是否已授权决定放行
            goal = self._service.goals.get(resume) or goal
            if not goal:
                return f"未找到 run_id={resume} 的历史目标，无法续跑。"
            run_id = resume
            run_approved = run_id in self._service.approved_runs
        else:
            run_id = uuid.uuid4().hex[:12]
            self._service.goals[run_id] = goal
            run_approved = False

        if not goal:
            return "缺少参数 goal（任务目标）。"

        try:
            agent = self._service.create_agent(run_id, run_approved)
        except ValueError as e:
            return str(e)

        try:
            summary = await agent.run(goal, run_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("computer_use 运行异常")
            summary = f"任务执行异常：{e}"
        return summary


def _make_credential_resolver(
    ctx: PluginContext,
) -> Callable[[], tuple[str, str]]:
    """构造凭据解析器：从项目 ProviderRegistry 取 deepseek provider 的
    (api_key, base_url)，作为 COMPUTER_USE_API_KEY / DEEPSEEK_API_KEY 之外的
    最终兜底。项目密钥存于数据库（加密字段），进程内没有对应环境变量，因此
    必须在运行时而非激活时解析，以避免插件激活顺序影响。
    """

    def resolver() -> tuple[str, str]:
        try:
            from harness.modules.model_manager.provider_registry import (
                ProviderRegistry,
            )
        except Exception:  # noqa: BLE001
            return ("", "")
        if not ctx.services.has(ProviderRegistry):
            return ("", "")
        try:
            # include_disabled=True：即使 provider 被停用也允许复用其密钥
            provider = ctx.services.get(ProviderRegistry).get_provider("deepseek", include_disabled=True)
        except Exception:  # noqa: BLE001
            return ("", "")
        # 仅读取，绝不写入，避免影响共享 provider 单例
        return (
            getattr(provider, "api_key", "") or "",
            getattr(provider, "base_url", "") or "",
        )

    return resolver


def _register_routes(app: Any, service: ComputerUseService) -> None:
    """在 FastAPI 应用上注册人工授权相关路由（幂等）。"""
    existing = {getattr(r, "path", "") for r in app.routes}
    if "/api/computer-use/approve" in existing:
        return  # 已注册（如 uvicorn --reload 重复 activate）

    from fastapi import Query

    async def approve(run_id: str = Query(..., description="待授权任务 ID")) -> dict[str, Any]:
        ok = service.approve_run(run_id)
        return {"ok": ok, "run_id": run_id, "status": "approved"}

    async def pending() -> dict[str, Any]:
        return {"pending": service.list_pending()}

    app.add_api_route(
        "/api/computer-use/approve",
        approve,
        methods=["POST"],
        tags=["computer-use"],
    )
    app.add_api_route(
        "/api/computer-use/pending",
        pending,
        methods=["GET"],
        tags=["computer-use"],
    )
    logger.info("Computer Use 审批路由已注册")


class ComputerUsePlugin(BasePlugin):
    """Computer Use 插件。"""

    manifest: PluginManifest

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._service: ComputerUseService | None = None

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        config = load_computer_use_config(ctx.config)
        # 注入凭据解析器（运行时从 ProviderRegistry 兜底取 DeepSeek 密钥）
        resolver = _make_credential_resolver(ctx)
        self._service = ComputerUseService(config, credential_resolver=resolver)

        # 注册服务（供其它插件/工具复用）
        ctx.services.register(ComputerUseService, self._service, owner=self.plugin_id)

        # 注册 computer_use 工具到全局工具注册表
        tool = ComputerUseTool(self._service)
        ctx.services.get(ToolRegistry).register(tool, owner=self.plugin_id)

        # 注册人工授权路由（若 FastAPI app 可获取）
        try:
            from harness.main import app as fastapi_app

            _register_routes(fastapi_app, self._service)
        except Exception as e:  # noqa: BLE001
            logger.warning("Computer Use 审批路由注册失败（不影响白名单模式）: %s", e)

        # 桌面可用性提示
        if not self._service.desktop.available:
            logger.warning(
                "Computer Use：真实桌面控制不可用（缺 pyautogui/mss 或无显示器），"
                "鼠标/键盘/截图将返回不可用提示；文件/命令能力仍可用。"
            )

        ctx.logger.info("Computer Use 插件已激活（模型=%s，模式=%s）", config.model, config.approval_mode)

    async def deactivate(self, ctx: PluginContext) -> None:
        try:
            ctx.services.get(ToolRegistry).unregister_all(self.plugin_id)
        except Exception:
            pass
        ctx.logger.info("Computer Use 插件已停用")
