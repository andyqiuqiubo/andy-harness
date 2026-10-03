"""哈希与编码工具插件 —— md5/sha1/sha256/sha512 + base64。"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.hash_encode")


class HashEncodeTool(ToolPlugin):
    """哈希与编码工具。"""

    @property
    def tool_name(self) -> str:
        return "hash_encode"

    @property
    def risk_level(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return (
            "文本哈希与编码。支持：md5 / sha1 / sha256 / sha512 摘要（十六进制），"
            "以及 base64-encode / base64-decode / base64url-encode / base64url-decode。"
            "用于『给字符串算 sha256』『base64 编码/解码』等。注意：这是单向哈希与"
            "可逆编码工具，不是加密，请勿用于需要保密的密钥存储说明。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "description": "操作：md5 / sha1 / sha256 / sha512 / "
                    "base64-encode / base64-decode / base64url-encode / "
                    "base64url-decode",
                },
                "text": {
                    "type": "string",
                    "description": "待处理文本（base64-decode 时为 base64 字符串）",
                },
            },
            "required": ["operation", "text"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        operation = (args.get("operation") or "").strip().lower()
        text = args.get("text", "")
        if text is None:
            text = ""

        try:
            if operation in ("md5", "sha1", "sha256", "sha512"):
                h = hashlib.new(operation)
                h.update(text.encode("utf-8"))
                return f"{operation}: {h.hexdigest()}"
            if operation == "base64-encode":
                return base64.b64encode(text.encode("utf-8")).decode("ascii")
            if operation == "base64-decode":
                return base64.b64decode(text.encode("ascii")).decode("utf-8")
            if operation == "base64url-encode":
                return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
            if operation == "base64url-decode":
                return base64.urlsafe_b64decode(text.encode("ascii")).decode("utf-8")
            return (
                "未知操作: " + operation + "（应为 md5/sha1/sha256/sha512/"
                "base64-encode/base64-decode/base64url-encode/base64url-decode）"
            )
        except Exception as e:  # noqa: BLE001
            return f"处理失败: {e}"


class HashEncodePlugin(BasePlugin):
    """哈希与编码插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        ctx.services.get(ToolRegistry).register(HashEncodeTool(), owner=self.plugin_id)
        ctx.logger.info("HashEncode 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        from harness.engine.tool_registry import ToolRegistry

        try:
            ctx.services.get(ToolRegistry).unregister_all(self.plugin_id)
        except Exception:  # noqa: BLE001
            pass
