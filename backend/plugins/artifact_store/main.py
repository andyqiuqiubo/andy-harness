"""artifact-store 插件 —— 大工具输出落盘（post_tool_call 钩子）。

激活时：
1. 注册 `ArtifactStore` 服务（工件文件 + SQLite 元数据）；
2. 注册 `post_tool_call` 钩子：工具结果超过阈值时落盘，并把上下文里的
   结果替换为 `[输出已落盘] 路径 + 摘要 + artifact_id`，避免撑爆 token 预算。
"""

from __future__ import annotations

from harness.infra.database import Database
from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.hook import HookContext, HookResult
from harness.modules.artifact_store.service import (
    ArtifactStore,
    ArtifactStoreImpl,
    offload_threshold,
)

# 这些工具的结果本身用于"回读"，不再二次落盘，避免嵌套与死循环
_SKIP_TOOLS = {"read_artifact"}


class ArtifactStorePlugin(BasePlugin):
    """工件存储插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None
    _service: ArtifactStoreImpl | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._service = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册服务与 post_tool_call 钩子。"""
        self._ctx = ctx
        try:
            db = ctx.services.get(Database)
        except Exception:
            db = Database()
        self._service = ArtifactStoreImpl(db)
        ctx.services.register(ArtifactStore, self._service, owner=self.plugin_id)
        ctx.hooks.register(
            "post_tool_call", self._on_post_tool_call, owner=self.plugin_id
        )
        ctx.logger.info(
            "artifact-store 已激活（目录=%s，阈值=%d 字符）",
            self._service.base_dir,
            offload_threshold(),
        )

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销服务与钩子。"""
        self._service = None
        ctx.logger.info("artifact-store 已停用")

    async def _on_post_tool_call(self, hctx: HookContext) -> HookResult:
        """post_tool_call 钩子：超阈值结果落盘。"""
        from harness.engine.hook_types import ToolCallContext

        data = hctx.data
        if not isinstance(data, ToolCallContext):
            return HookResult(data=data)

        # 仅处理成功且有内容、且不是"回读"类工具
        if data.error or not data.result or data.tool_name in _SKIP_TOOLS:
            return HookResult(data=data)
        if self._service is None or not self._service.should_offload(data.result):
            return HookResult(data=data)

        record = self._service.offload(hctx.session_id, data.tool_name, data.result)
        data.result = (
            f"[输出已落盘] 工具 '{record.tool_name}' 的原始输出共 "
            f"{record.char_count} 字符，已保存为工件以免占用上下文。\n"
            f"artifact_id: {record.id}\n"
            f"文件路径: {record.path}\n"
            f"内容摘要: {record.summary}\n"
            f"如需查看完整内容，请调用 read_artifact 工具"
            f"（artifact_id=\"{record.id}\"，可用 offset/limit 分页）。"
        )
        if self._ctx is not None:
            self._ctx.logger.info(
                "工具输出已落盘: tool=%s chars=%d id=%s",
                record.tool_name,
                record.char_count,
                record.id,
            )
        return HookResult(data=data, metadata={"offloaded": record.id})
