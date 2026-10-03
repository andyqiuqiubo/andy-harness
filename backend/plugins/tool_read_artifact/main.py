"""read_artifact 工具插件 —— 回读被落盘的大工具输出。

大输出被 artifact-store 的 post_tool_call 钩子落盘后，上下文里只保留
路径与摘要。模型需要全文时调用此工具，支持 offset/limit 分页，
避免一次性把超大内容再次撑爆上下文。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin
from harness.kernel.services import ServiceRegistry
from harness.modules.artifact_store.service import ArtifactStore

logger = logging.getLogger("harness.tools.read_artifact")

# 单次返回字符上限（防止一次读取过大又撑爆上下文）
MAX_READ_CHARS = 20000


class ReadArtifactTool(ToolPlugin):
    """读取落盘工件的工具。"""

    def __init__(self, services: ServiceRegistry) -> None:
        self._services = services

    @property
    def tool_name(self) -> str:
        return "read_artifact"

    @property
    def risk_level(self) -> str:
        """只读，无副作用。"""
        return "read"

    @property
    def description(self) -> str:
        return (
            "读取此前被落盘（offload）的大工具输出全文。\n"
            "何时使用：上下文里出现「[输出已落盘] artifact_id: ...」时，\n"
            "若需要查看被省略的完整内容，调用本工具并传入该 artifact_id。\n"
            "大文件可用 offset/limit 分页读取；也可只传 artifact_id 取全文（有单次上限）。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "工件 id（形如 art_20260928..._xxxxxxxx）",
                },
                "offset": {
                    "type": "integer",
                    "description": "起始字符偏移，默认 0",
                },
                "limit": {
                    "type": "integer",
                    "description": "读取字符数上限，默认读取到结尾（受单次上限约束）",
                },
            },
            "required": ["artifact_id"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        """读取工件内容。"""
        artifact_id = str(args.get("artifact_id") or args.get("id") or "").strip()
        if not artifact_id:
            return "错误: 缺少 artifact_id 参数"

        if not self._services.has(ArtifactStore):
            return "错误: 工件服务不可用（artifact_store 插件未激活）"
        store: ArtifactStore = self._services.get(ArtifactStore)

        record = store.get(artifact_id)
        if record is None:
            return f"错误: 未找到工件 {artifact_id}"

        try:
            offset = int(args.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        limit_raw = args.get("limit")
        limit: int | None
        try:
            limit = int(limit_raw) if limit_raw not in (None, "") else None
        except (TypeError, ValueError):
            limit = None

        if limit is None:
            limit = MAX_READ_CHARS
        else:
            limit = min(limit, MAX_READ_CHARS)

        chunk = store.read(artifact_id, offset, limit)
        if chunk is None:
            return f"错误: 工件 {artifact_id} 的正文文件缺失"

        header = (
            f"[工件 {record.id}] 工具={record.tool_name} "
            f"原始 {record.char_count} 字符，本次读取 offset={offset} limit={limit}"
        )
        truncated = offset + len(chunk) < record.char_count
        footer = (
            f"\n…（还有 {record.char_count - offset - len(chunk)} 字符未读出，可增大 offset 继续读取）"
            if truncated
            else ""
        )
        return f"{header}\n{'-' * 40}\n{chunk}{footer}"


class ReadArtifactPlugin(BasePlugin):
    """read_artifact 工具插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册工具到 ToolRegistry。"""
        self._ctx = ctx

        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        tool_registry = ctx.services.get(ToolRegistry)

        tool_registry.register(ReadArtifactTool(ctx.services), owner=self.plugin_id)
        ctx.logger.info("read_artifact 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销工具。"""
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            tool_registry.unregister_all(self.plugin_id)
        except Exception:
            pass
        ctx.logger.info("read_artifact 工具已注销")
