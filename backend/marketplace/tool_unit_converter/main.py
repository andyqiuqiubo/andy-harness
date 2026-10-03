"""单位换算工具插件 —— 长度/重量/温度/面积/体积/速度/数据。"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.unit_converter")

# 以「基准单位」为 1 的各单位换算因子（factor: 1 单位 = factor 基准单位）
_FACTORS: dict[str, dict[str, float]] = {
    "length": {
        "m": 1.0,
        "km": 1000.0,
        "cm": 0.01,
        "mm": 0.001,
        "mi": 1609.344,
        "yd": 0.9144,
        "ft": 0.3048,
        "in": 0.0254,
    },
    "mass": {
        "kg": 1.0,
        "g": 0.001,
        "mg": 1e-6,
        "t": 1000.0,
        "lb": 0.45359237,
        "oz": 0.028349523125,
    },
    "area": {
        "m2": 1.0,
        "km2": 1e6,
        "cm2": 1e-4,
        "ha": 10000.0,
        "acre": 4046.8564224,
        "ft2": 0.09290304,
    },
    "volume": {
        "L": 1.0,
        "mL": 0.001,
        "m3": 1000.0,
        "gal": 3.785411784,
        "ft3": 28.316846592,
    },
    "speed": {
        "m/s": 1.0,
        "km/h": 0.2777777778,
        "mph": 0.44704,
        "knot": 0.5144444444,
    },
}

# 温度需要特殊公式处理（不在因子表里）
_TEMP_UNITS = {"C", "F", "K"}


def _convert_temperature(value: float, src: str, dst: str) -> float:
    # 先统一到摄氏度
    if src == "C":
        c = value
    elif src == "F":
        c = (value - 32) * 5 / 9
    elif src == "K":
        c = value - 273.15
    else:
        raise ValueError(f"不支持的温度单位: {src}")
    # 从摄氏度转出
    if dst == "C":
        return c
    if dst == "F":
        return c * 9 / 5 + 32
    if dst == "K":
        return c + 273.15
    raise ValueError(f"不支持的温度单位: {dst}")


def _convert_data(value: float, src: str, dst: str) -> float:
    # 数据单位换算（默认二进制 1024）
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    if src not in units or dst not in units:
        raise ValueError(f"不支持的数据单位: {src} / {dst}")
    in_bytes = value * units[src]
    return in_bytes / units[dst]


class UnitConverterTool(ToolPlugin):
    """单位换算工具。"""

    @property
    def tool_name(self) -> str:
        return "unit_converter"

    @property
    def risk_level(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return (
            "常见物理量单位换算。支持类别：length(长度) / mass(重量) / temperature(温度) / "
            "area(面积) / volume(体积) / speed(速度) / data(数据)。"
            "温度用 C/F/K，数据用 B/KB/MB/GB/TB（二进制）。"
            "用于『5 英里是多少公里』『100 华氏度是多少摄氏』『1 TB 是多少 GB』等。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "换算类别：length / mass / temperature / area / volume / speed / data",
                },
                "value": {"type": "number", "description": "待换算的数值"},
                "from": {"type": "string", "description": "源单位，如 km / kg / C / GB"},
                "to": {"type": "string", "description": "目标单位，如 mi / lb / F / MB"},
            },
            "required": ["category", "value", "from", "to"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        category = (args.get("category") or "").strip().lower()
        try:
            value = float(args.get("value", 0))
        except (TypeError, ValueError):
            return "错误：value 必须是数字"
        src = (args.get("from") or "").strip()
        dst = (args.get("to") or "").strip()

        try:
            if category == "temperature":
                if src not in _TEMP_UNITS or dst not in _TEMP_UNITS:
                    return f"温度仅支持 C/F/K，收到: {src} → {dst}"
                result = _convert_temperature(value, src, dst)
            elif category == "data":
                result = _convert_data(value, src, dst)
            elif category in _FACTORS:
                table = _FACTORS[category]
                if src not in table or dst not in table:
                    return f"单位不支持（{category}）：from={src} to={dst}；可选: {', '.join(sorted(table))}"
                result = value * table[src] / table[dst]
            else:
                return f"未知类别: {category}（应为 length / mass / temperature / area / volume / speed / data）"

            # 浮点整洁显示
            if abs(result - round(result)) < 1e-9:
                result_repr = str(int(round(result)))
            else:
                result_repr = f"{result:.6g}"
            return f"{value} {src} = {result_repr} {dst}"
        except Exception as e:  # noqa: BLE001
            return f"换算失败: {e}"


class UnitConverterPlugin(BasePlugin):
    """单位换算插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        ctx.services.get(ToolRegistry).register(UnitConverterTool(), owner=self.plugin_id)
        ctx.logger.info("UnitConverter 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        from harness.engine.tool_registry import ToolRegistry

        try:
            ctx.services.get(ToolRegistry).unregister_all(self.plugin_id)
        except Exception:  # noqa: BLE001
            pass
