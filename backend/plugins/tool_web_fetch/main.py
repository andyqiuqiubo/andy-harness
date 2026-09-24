"""Web Fetch 工具插件 —— 抓取指定网页正文内容。

常与 web_search 配合使用：先搜索获取链接，再抓取具体网页获取详细内容。
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.web_fetch")


class WebFetchTool(ToolPlugin):
    """网页内容抓取工具。"""

    @property
    def tool_name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "抓取指定 URL 网页的正文内容。"
            "当你需要获取某个网页的详细内容（如新闻全文、文章、文档等）时使用。"
            "通常先使用 web_search 搜索，找到相关链接后再用此工具获取详细内容。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要抓取的网页 URL（必须包含 http:// 或 https://）",
                },
                "max_length": {
                    "type": "integer",
                    "description": "返回正文的最大字符数（默认 4000）",
                    "default": 4000,
                },
            },
            "required": ["url"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        """抓取网页内容。"""
        url = args.get("url", "").strip()
        if not url:
            return "错误: 未提供 URL"

        # 验证 URL
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return "错误: URL 必须以 http:// 或 https:// 开头"
        if not parsed.netloc:
            return "错误: 无效的 URL"

        max_length = min(args.get("max_length", 4000), 8000)

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
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                html = resp.text
                final_url = str(resp.url)

            # 提取标题
            title = ""
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
            if title_match:
                title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title_match.group(1))).strip()

            # 移除脚本、样式、注释等干扰内容
            cleaned = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
            cleaned = re.sub(r"<style[^>]*>.*?</style>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
            cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.DOTALL)
            cleaned = re.sub(r"<nav[^>]*>.*?</nav>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
            cleaned = re.sub(r"<footer[^>]*>.*?</footer>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
            cleaned = re.sub(r"<header[^>]*>.*?</header>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)

            # 提取正文：优先 article / main 标签，否则取 body
            article_match = re.search(
                r"<(?:article|main)[^>]*>(.*?)</(?:article|main)>",
                cleaned,
                re.DOTALL | re.IGNORECASE,
            )
            content_html = article_match.group(1) if article_match else cleaned

            # 尝试提取 <p> 段落
            paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", content_html, re.DOTALL | re.IGNORECASE)
            if paragraphs:
                text_parts = []
                for p in paragraphs:
                    text = re.sub(r"<[^>]+>", "", p).strip()
                    text = re.sub(r"\s+", " ", text)
                    if len(text) > 10:  # 过滤过短的片段
                        text_parts.append(text)
                text = "\n".join(text_parts)
            else:
                # 回退：移除所有 HTML 标签
                text = re.sub(r"<[^>]+>", " ", content_html)
                text = re.sub(r"\s+", " ", text).strip()

            if not text:
                return f"无法从 {url} 提取正文内容（可能是纯 JS 渲染页面）。"

            # 截断到最大长度
            if len(text) > max_length:
                text = text[:max_length] + "...[内容已截断]"

            lines = [f"网页内容: {final_url}"]
            if title:
                lines.append(f"标题: {title}")
            lines.append("")
            lines.append(text)
            return "\n".join(lines)

        except httpx.TimeoutException:
            return f"错误: 抓取 {url} 超时"
        except httpx.ConnectError as e:
            return f"错误: 无法连接到 {url}: {e}"
        except Exception as e:
            logger.error("网页抓取失败: %s", e)
            return f"错误: 抓取 {url} 失败: {e}"


class WebFetchPlugin(BasePlugin):
    """Web Fetch 工具插件。"""

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

        tool_registry.register(WebFetchTool(), owner=self.plugin_id)
        ctx.logger.info("Web Fetch 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销工具。"""
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            tool_registry.unregister_all(self.plugin_id)
        except Exception:
            pass
        ctx.logger.info("Web Fetch 工具已注销")
