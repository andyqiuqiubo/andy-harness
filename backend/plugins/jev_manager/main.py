"""Jev Manager —— 封装 TypeSafe AI Jev 结构化决策 API。

Jev 是 TypeSafe AI 推出的结构化决策模型，输出可直接被代码使用的
带概率的判断结果。支持三种基础原语：
- Choice（选择型）：从固定选项中选一个，返回概率分布和置信度
- Score（评分型）：在自定义刻度上打等级分，返回概率分布和置信度
- Noul（是非型）：判断命题是否成立，返回 0~1 概率值

三种原语可混合并行提问，独立计算、同时返回。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest

logger = logging.getLogger("harness.jev_manager")


class JevManager:
    """Jev 决策服务，封装三种原语的 API 调用。"""

    def __init__(
        self, api_key: str = "", base_url: str = "https://api.typesafe.ai/v1"
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    @property
    def is_configured(self) -> bool:
        """是否已配置 API Key。"""
        return bool(self.api_key)

    async def decide(
        self,
        scene: str,
        queries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """统一入口：一次提交多个判断请求，并行返回结果。

        Args:
            scene: 场景描述
            queries: 判断请求列表，每个含 type(choice/score/noul) 及对应参数

        Returns:
            与 queries 等长的结果列表
        """
        if not self.is_configured:
            raise ValueError("Jev API Key 未配置，请先在设置中配置 jev_manager 的 api_key")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {"scene": scene, "queries": queries}

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/jev/decide",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", [])

    async def choice(
        self,
        scene: str,
        question: str,
        options: list[str],
    ) -> dict[str, Any]:
        """选择型：从固定选项中选一个。

        Returns:
            {"selected": str, "probabilities": dict, "confidence": float}
        """
        results = await self.decide(
            scene,
            [{"type": "choice", "question": question, "options": options}],
        )
        return results[0] if results else {}

    async def score(
        self,
        scene: str,
        question: str,
        scale: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """评分型：在自定义刻度上打分。

        Args:
            scale: 有序等级列表，如 [{"level": 0, "desc": "冷静"}, ...]

        Returns:
            {"score": float, "probabilities": dict, "confidence": float}
        """
        results = await self.decide(
            scene,
            [{"type": "score", "question": question, "scale": scale}],
        )
        return results[0] if results else {}

    async def noul(self, scene: str, proposition: str) -> dict[str, Any]:
        """是非型：判断命题是否成立。

        Returns:
            {"probability": float}  # 0~1
        """
        results = await self.decide(
            scene,
            [{"type": "noul", "proposition": proposition}],
        )
        return results[0] if results else {}


class JevManagerPlugin(BasePlugin):
    """Jev Manager 服务插件。

    激活时注册 JevManager 到 ServiceRegistry。
    没有 API Key 也能激活（记录警告），Tool 插件调用时会返回友好错误。
    """

    manifest: PluginManifest
    _ctx: PluginContext | None = None
    _manager: JevManager | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._manager = None

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        import os

        api_key = ctx.config.get("api_key", "") or os.getenv("JEV_API_KEY", "")
        base_url = ctx.config.get(
            "base_url", "https://api.typesafe.ai/v1"
        )
        self._manager = JevManager(api_key=api_key, base_url=base_url)
        ctx.services.register(JevManager, self._manager, owner=self.plugin_id)

        if api_key:
            ctx.logger.info("Jev Manager 已激活（API Key 已配置）")
        else:
            ctx.logger.warning(
                "Jev Manager 已激活，但未配置 API Key。"
                "请在设置页面配置 jev_manager 的 api_key，"
                "或设置环境变量 JEV_API_KEY。"
                "未配置时调用 Jev 工具将返回友好错误提示。"
            )

    async def deactivate(self, ctx: PluginContext) -> None:
        try:
            ctx.services.unregister(JevManager, owner=self.plugin_id)
        except Exception:
            pass
        self._manager = None
        ctx.logger.info("Jev Manager 已停用")
