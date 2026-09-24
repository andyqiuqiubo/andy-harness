"""Web Search 工具插件 —— 基于百度搜索引擎。"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote_plus

import httpx

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.web_search")


class WebSearchTool(ToolPlugin):
    """百度搜索工具。"""

    @property
    def tool_name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "在百度上搜索关键词，返回搜索结果摘要。"
            "当你需要查找最新信息、实时数据、新闻、天气、汇率等"
            "你不确定或知识截止后发生的事件时使用此工具。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词",
                },
                "num": {
                    "type": "integer",
                    "description": "返回结果数量（默认5，最大10）",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        """执行百度搜索。"""
        query = args.get("query", "").strip()
        if not query:
            return "错误: 未提供搜索关键词"

        num = min(args.get("num", 5), 10)
        results = await self._search_baidu(query, num)

        if not results:
            return f"未找到与 '{query}' 相关的搜索结果。"

        lines = [f"百度搜索 '{query}' 的结果：\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}")
            if r["snippet"]:
                lines.append(f"   摘要: {r['snippet']}")
            if r["url"]:
                lines.append(f"   链接: {r['url']}")
            lines.append("")

        return "\n".join(lines)

    async def _search_baidu(
        self, query: str, num: int = 5
    ) -> list[dict[str, str]]:
        """通过百度搜索获取结果。

        使用百度搜索页面解析结果。
        """
        results: list[dict[str, str]] = []
        url = f"https://www.baidu.com/s?wd={quote_plus(query)}&rn={num}"

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=headers, follow_redirects=True)
                html = resp.text

            # 解析搜索结果
            # 百度搜索结果块在 class="result" 的 div 中
            # 标题在 <h3> 标签内，摘要在 class="c-abstract" 中
            result_blocks = re.findall(
                r'<div[^>]*class="[^"]*result[^"]*"[^>]*>(.*?)</div>\s*(?=<div|</div>)',
                html,
                re.DOTALL,
            )

            for block in result_blocks[:num]:
                title = ""
                snippet = ""
                link = ""

                # 提取标题
                title_match = re.search(
                    r'<h3[^>]*>(?:<a[^>]*>)?(.*?)(?:</a>)?</h3>', block, re.DOTALL
                )
                if title_match:
                    title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()

                # 提取链接
                link_match = re.search(r'href="(https?://[^"]+)"', block)
                if link_match:
                    link = link_match.group(1)

                # 提取摘要
                snippet_match = re.search(
                    r'class="[^"]*c-abstract[^"]*"[^>]*>(.*?)</(?:span|div)>',
                    block,
                    re.DOTALL,
                )
                if snippet_match:
                    snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip()

                if title:
                    results.append(
                        {"title": title, "snippet": snippet, "url": link}
                    )

            # 如果正则解析失败，尝试备用解析方式
            if not results:
                # 简单提取所有 <h3> 中的文本作为标题
                h3_matches = re.findall(
                    r"<h3[^>]*>(?:<a[^>]*>)?(.*?)(?:</a>)?</h3>",
                    html,
                    re.DOTALL,
                )
                for match in h3_matches[:num]:
                    clean = re.sub(r"<[^>]+>", "", match).strip()
                    if clean and len(clean) > 2:
                        results.append(
                            {"title": clean, "snippet": "", "url": ""}
                        )

        except Exception as e:
            logger.error("百度搜索失败: %s", e)
            results.append(
                {
                    "title": f"搜索失败: {e}",
                    "snippet": "",
                    "url": "",
                }
            )

        return results


class WebSearchPlugin(BasePlugin):
    """Web Search 工具插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册工具到 ToolRegistry。"""
        self._ctx = ctx

        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(
                ToolRegistry, ToolRegistry(), owner=self.plugin_id
            )
        tool_registry = ctx.services.get(ToolRegistry)

        tool_registry.register(WebSearchTool(), owner=self.plugin_id)
        ctx.logger.info("Web Search 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销工具。"""
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            tool_registry.unregister_all(self.plugin_id)
        except Exception:
            pass
        ctx.logger.info("Web Search 工具已注销")
