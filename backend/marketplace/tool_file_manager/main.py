"""文件管理工具插件 —— 受控目录内的安全文件读写。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.file_manager")

# 单次读取最多返回的字符数（防止巨型文件撑爆上下文）
_MAX_READ_CHARS = 20000


class FileManagerTool(ToolPlugin):
    """受控目录内的文件管理工具。"""

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir.resolve()

    @property
    def tool_name(self) -> str:
        return "file_manager"

    @property
    def risk_level(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return (
            "在受控根目录内安全管理文件。操作：list(列目录) / read(读文本) / "
            "write(写文本，自动建父目录) / mkdir(建文件夹) / delete(删文件或空文件夹)。"
            "所有路径都相对 base_dir 解析，且会被限制在 base_dir 之内（拦截 ../ 越权）。"
            "用于『列出工作目录』『读一下某个文件』『新建一个笔记文件』等。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "description": "操作：list / read / write / mkdir / delete",
                    "enum": ["list", "read", "write", "mkdir", "delete"],
                },
                "path": {
                    "type": "string",
                    "description": "目标路径（相对 base_dir，如 notes/foo.txt）",
                },
                "content": {
                    "type": "string",
                    "description": "write 操作写入的文本内容",
                },
            },
            "required": ["operation", "path"],
        }

    def _resolve(self, rel: str) -> Path:
        """把相对路径安全解析到 base_dir 内，越权则抛错。"""
        if not rel:
            raise ValueError("path 不能为空")
        target = (self._base / rel).resolve()
        # 必须是 base_dir 自身或在其之下
        if target != self._base and self._base not in target.parents:
            raise ValueError(f"路径越权，超出受控目录: {rel}")
        return target

    async def execute(self, args: dict[str, Any]) -> str:
        operation = (args.get("operation") or "").strip().lower()
        rel = (args.get("path") or "").strip()
        try:
            if operation == "list":
                target = self._resolve(rel or ".")
                if not target.is_dir():
                    return f"不是目录: {rel}"
                items = []
                for p in sorted(target.iterdir()):
                    if p.is_dir():
                        items.append(f"[目录] {p.name}/")
                    else:
                        try:
                            size = p.stat().st_size
                        except OSError:
                            size = 0
                        items.append(f"[文件] {p.name} ({size} 字节)")
                if not items:
                    return f"{(rel or '.')} 为空目录"
                return f"目录 {target}：\n" + "\n".join(items)

            if operation == "read":
                target = self._resolve(rel)
                if not target.is_file():
                    return f"文件不存在: {rel}"
                text = target.read_text(encoding="utf-8", errors="replace")
                if len(text) > _MAX_READ_CHARS:
                    text = text[:_MAX_READ_CHARS] + f"\n\n... [已截断，剩余 {len(text) - _MAX_READ_CHARS} 字符]"
                return text

            if operation == "write":
                target = self._resolve(rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(args.get("content", "") or "", encoding="utf-8")
                return f"已写入 {target}（{len(args.get('content', '') or '')} 字符）"

            if operation == "mkdir":
                target = self._resolve(rel)
                target.mkdir(parents=True, exist_ok=True)
                return f"已创建目录 {target}"

            if operation == "delete":
                target = self._resolve(rel)
                if target.is_dir():
                    try:
                        target.rmdir()  # 仅删空目录
                    except OSError:
                        return f"目录非空或删除失败: {rel}（仅支持删除空目录）"
                    return f"已删除目录 {target}"
                if target.is_file():
                    target.unlink()
                    return f"已删除文件 {target}"
                return f"路径不存在: {rel}"

            return f"未知操作: {operation}（应为 list/read/write/mkdir/delete）"
        except Exception as e:  # noqa: BLE001
            return f"文件操作失败: {e}"


class FileManagerPlugin(BasePlugin):
    """文件管理插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        from harness.engine.tool_registry import ToolRegistry

        raw = ctx.config.get("base_dir", "") if ctx.config else ""
        base = Path(raw).resolve() if raw else Path.cwd()
        base.mkdir(parents=True, exist_ok=True)

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        ctx.services.get(ToolRegistry).register(FileManagerTool(base), owner=self.plugin_id)
        ctx.logger.info("FileManager 工具已注册，base_dir=%s", base)

    async def deactivate(self, ctx: PluginContext) -> None:
        from harness.engine.tool_registry import ToolRegistry

        try:
            ctx.services.get(ToolRegistry).unregister_all(self.plugin_id)
        except Exception:  # noqa: BLE001
            pass
