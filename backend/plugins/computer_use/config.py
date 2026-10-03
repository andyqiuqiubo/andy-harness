"""Computer Use 配置层。

设计原则：所有可变参数（API Key、Base URL、模型名、最大轮数、各类超时、
截图质量、安全策略等）均**不硬编码**，按以下优先级注入：

    1. 插件配置（plugin.json 的 config_schema + 用户在设置页保存的值）
    2. 环境变量（见 _ENV 映射，便于容器/CI 注入，也便于不落盘密钥）
    3. 代码中的默认值（与 plugin.json 默认保持一致）

这样既能通过 UI 配置，也能通过环境变量在部署时统一管控。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 环境变量名映射（配置键 -> 环境变量名）
_ENV: dict[str, str] = {
    "api_key": "COMPUTER_USE_API_KEY",
    "base_url": "COMPUTER_USE_BASE_URL",
    "model": "COMPUTER_USE_MODEL",
    "max_iterations": "COMPUTER_USE_MAX_ITERATIONS",
    "max_llm_calls": "COMPUTER_USE_MAX_LLM_CALLS",
    "plan_first": "COMPUTER_USE_PLAN_FIRST",
    "stagnation_limit": "COMPUTER_USE_STAGNATION_LIMIT",
    "overall_timeout": "COMPUTER_USE_OVERALL_TIMEOUT",
    "step_timeout": "COMPUTER_USE_STEP_TIMEOUT",
    "http_timeout": "COMPUTER_USE_HTTP_TIMEOUT",
    "max_retries": "COMPUTER_USE_RETRIES",
    "max_image_dimension": "COMPUTER_USE_MAX_IMAGE_DIM",
    "jpeg_quality": "COMPUTER_USE_JPEG_QUALITY",
    "max_output_chars": "COMPUTER_USE_MAX_OUTPUT",
    "approval_mode": "COMPUTER_USE_APPROVAL_MODE",
    "allow_desktop": "COMPUTER_USE_ALLOW_DESKTOP",
    "allow_commands": "COMPUTER_USE_ALLOW_COMMANDS",
    "allow_paths": "COMPUTER_USE_ALLOW_PATHS",
    "deny_commands": "COMPUTER_USE_DENY_COMMANDS",
    "deny_paths": "COMPUTER_USE_DENY_PATHS",
    "working_dir": "COMPUTER_USE_WORKING_DIR",
}

# 默认工作目录：backend/data/computer_use_workspace（与项目数据隔离，避免污染真实库）
_DEFAULT_WORKING_DIR = Path(__file__).resolve().parents[2] / "data" / "computer_use_workspace"


@dataclass
class ComputerUseConfig:
    """Computer Use 运行时配置（全部来自配置/环境变量，无硬编码魔法值）。"""

    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"  # 对应 DeepSeek V4.1 Flash
    max_iterations: int = 15  # 历史字段：单次任务的动作循环次数，现由 max_llm_calls 兜底
    max_llm_calls: int = 15  # 模型调用次数硬预算（每次 complete 都计费，超此即中止）
    plan_first: bool = True  # 计划先行：执行前先用一次调用产出步骤清单
    stagnation_limit: int = 4  # 连续 N 轮执行完全相同的操作 -> 判定卡死提前中止
    overall_timeout: float = 300.0
    step_timeout: float = 15.0
    http_timeout: float = 60.0
    max_retries: int = 2
    max_image_dimension: int = 1280
    jpeg_quality: int = 70
    max_output_chars: int = 4000
    approval_mode: str = "whitelist"  # "whitelist" | "interactive"
    allow_desktop: bool = True
    allow_commands: set[str] = field(default_factory=set)
    allow_paths: list[str] = field(default_factory=list)
    deny_commands: list[str] = field(default_factory=list)
    deny_paths: list[str] = field(default_factory=list)
    working_dir: str = ""


def _coerce_bool(value: Any) -> bool:
    """将配置/环境变量值安全转为布尔。"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _split_list(value: Any) -> list[str]:
    """逗号分隔字符串 -> 去空白非空项列表。"""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if not value:
        return []
    return [v.strip() for v in str(value).split(",") if v.strip()]


def _resolve(key: str, cfg: Any, default: Any) -> Any:
    """按 配置 -> 环境变量 -> 默认值 三级解析单个键。"""
    val = cfg.get(key, None) if cfg is not None else None
    if val in (None, ""):
        env_name = _ENV.get(key)
        if env_name:
            val = os.getenv(env_name, None)
    if val in (None, ""):
        return default
    return val


def load_computer_use_config(cfg: Any) -> ComputerUseConfig:
    """从插件配置（PluginConfig）构建强类型 ComputerUseConfig。

    API Key 优先取插件配置，其次回退到通用环境变量 DEEPSEEK_API_KEY。
    若两者皆无，运行时（main.create_agent）会再从项目已配置的 DeepSeek
    provider（「设置页」中录入、存于数据库的密钥）兜底复用，避免重复录入。
    """
    api_key = _resolve("api_key", cfg, "")
    if not api_key:
        api_key = os.getenv("DEEPSEEK_API_KEY", "")

    working_dir = _resolve("working_dir", cfg, str(_DEFAULT_WORKING_DIR))

    # max_llm_calls 是模型调用次数的硬预算；未单独配置时回退到 max_iterations，
    # 保证旧配置（只设过 max_iterations）仍然有合理上限。
    max_iter = _coerce_int(_resolve("max_iterations", cfg, 15), 15)
    max_llm = _coerce_int(_resolve("max_llm_calls", cfg, max_iter), max_iter)

    return ComputerUseConfig(
        api_key=api_key,
        base_url=_resolve("base_url", cfg, "https://api.deepseek.com"),
        model=_resolve("model", cfg, "deepseek-v4-flash"),
        max_iterations=max_iter,
        max_llm_calls=max_llm,
        plan_first=_coerce_bool(_resolve("plan_first", cfg, True)),
        stagnation_limit=_coerce_int(_resolve("stagnation_limit", cfg, 4), 4),
        overall_timeout=_coerce_float(_resolve("overall_timeout", cfg, 300.0), 300.0),
        step_timeout=_coerce_float(_resolve("step_timeout", cfg, 15.0), 15.0),
        http_timeout=_coerce_float(_resolve("http_timeout", cfg, 60.0), 60.0),
        max_retries=_coerce_int(_resolve("max_retries", cfg, 2), 2),
        max_image_dimension=_coerce_int(_resolve("max_image_dimension", cfg, 1280), 1280),
        jpeg_quality=_coerce_int(_resolve("jpeg_quality", cfg, 70), 70),
        max_output_chars=_coerce_int(_resolve("max_output_chars", cfg, 4000), 4000),
        approval_mode=_resolve("approval_mode", cfg, "whitelist"),
        allow_desktop=_coerce_bool(_resolve("allow_desktop", cfg, True)),
        allow_commands=set(c.lower() for c in _split_list(_resolve("allow_commands", cfg, ""))),
        allow_paths=_split_list(_resolve("allow_paths", cfg, "")),
        deny_commands=_split_list(_resolve("deny_commands", cfg, "")),
        deny_paths=[p.replace("\\", "/").lower() for p in _split_list(_resolve("deny_paths", cfg, ""))],
        working_dir=working_dir,
    )
