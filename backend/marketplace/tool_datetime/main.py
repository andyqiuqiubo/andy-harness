"""日期时间工具插件 —— 当前时间 / 时间戳换算 / 日期加减。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.datetime")


def _get_tz(name: str | None):
    """解析时区名称，'local' / 空 → 本地时区，'utc' → UTC，其余用 zoneinfo。"""
    if not name or name.strip().lower() == "local":
        return datetime.now().astimezone().tzinfo
    key = name.strip().lower()
    if key == "utc":
        return UTC
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name.strip())
    except Exception:  # noqa: BLE001
        # 时区名无效时回退到本地时区，避免直接报错
        return datetime.now().astimezone().tzinfo


def _parse_base(value: str | None, tz) -> datetime:
    """解析基准时间字符串（ISO 或 YYYY-MM-DD HH:MM:SS），缺省取当前。"""
    if not value:
        return datetime.now(tz)
    text = value.strip().replace("T", " ")
    # 兼容纯日期
    fmt_candidates = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
    )
    for fmt in fmt_candidates:
        try:
            naive = datetime.strptime(text, fmt)
            return naive.replace(tzinfo=tz)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {value}")


class DatetimeTool(ToolPlugin):
    """日期时间工具。"""

    @property
    def tool_name(self) -> str:
        return "datetime"

    @property
    def risk_level(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return (
            "查询与换算日期时间。支持：① now —— 获取指定时区的当前时间"
            "（含星期、ISO 8601、Unix 时间戳）；② from_timestamp —— 将 Unix 时间戳"
            "按指定时区转成可读时间；③ add —— 在基准时间上加减 天/小时/分钟。"
            "时区参数默认本地时区，可传 Asia/Shanghai、UTC、America/New_York 等。"
            "用于『现在几点』『N 天后是几号』『时间戳对应北京时间』等问题。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "description": "操作类型：now / from_timestamp / add",
                    "enum": ["now", "from_timestamp", "add"],
                },
                "tz": {
                    "type": "string",
                    "description": "时区名（如 Asia/Shanghai、UTC、America/New_York），默认本地时区",
                },
                "timestamp": {
                    "type": "number",
                    "description": "from_timestamp 操作使用的 Unix 时间戳（秒）",
                },
                "base": {
                    "type": "string",
                    "description": "add 操作的基准时间，ISO 或 YYYY-MM-DD HH:MM:SS，缺省取当前",
                },
                "days": {"type": "number", "description": "add 操作加减的天数"},
                "hours": {"type": "number", "description": "add 操作加减的小时数"},
                "minutes": {"type": "number", "description": "add 操作加减的分钟数"},
            },
            "required": ["operation"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        operation = (args.get("operation") or "now").strip().lower()
        tz = _get_tz(args.get("tz"))
        weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

        try:
            if operation == "now":
                dt = datetime.now(tz)
                return self._format(dt, weekday_cn)
            if operation == "from_timestamp":
                ts = float(args.get("timestamp", 0))
                dt = datetime.fromtimestamp(ts, tz=tz)
                return self._format(dt, weekday_cn)
            if operation == "add":
                base = _parse_base(args.get("base"), tz)
                delta = timedelta(
                    days=float(args.get("days", 0) or 0),
                    hours=float(args.get("hours", 0) or 0),
                    minutes=float(args.get("minutes", 0) or 0),
                )
                dt = base + delta
                return self._format(dt, weekday_cn)
            return f"未知操作: {operation}（应为 now / from_timestamp / add）"
        except Exception as e:  # noqa: BLE001
            return f"日期时间处理失败: {e}"

    @staticmethod
    def _format(dt: datetime, weekday_cn: list[str]) -> str:
        ts = dt.timestamp()
        return (
            f"本地时间: {dt.strftime('%Y-%m-%d %H:%M:%S')} {weekday_cn[dt.weekday()]}\n"
            f"ISO 8601: {dt.isoformat()}\n"
            f"Unix 时间戳(秒): {ts:.0f}"
        )


class DatetimePlugin(BasePlugin):
    """日期时间插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        ctx.services.get(ToolRegistry).register(DatetimeTool(), owner=self.plugin_id)
        ctx.logger.info("Datetime 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        from harness.engine.tool_registry import ToolRegistry

        try:
            ctx.services.get(ToolRegistry).unregister_all(self.plugin_id)
        except Exception:  # noqa: BLE001
            pass
