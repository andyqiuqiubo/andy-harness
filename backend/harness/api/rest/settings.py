"""REST API —— 系统设置路由。

支持 GET / PUT /api/settings，设置持久化到 JSON 文件。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger("harness.api.settings")

router = APIRouter(prefix="/api/settings", tags=["settings"])

# 设置文件路径（与数据库同目录）
_SETTINGS_FILE = (
    Path(__file__).resolve().parent.parent.parent / "data" / "settings.json"
)

# 默认设置
_DEFAULT_SETTINGS: dict[str, Any] = {
    "theme": "light",
    "language": "zh-CN",
    "default_model": "deepseek-chat",
    "default_budget": 4096,
}


class SettingsUpdate(BaseModel):
    """更新系统设置请求。"""

    theme: str | None = None
    language: str | None = None
    default_model: str | None = None
    default_budget: int | None = None


def _load_settings() -> dict[str, Any]:
    """从 JSON 文件加载设置。"""
    if _SETTINGS_FILE.exists():
        try:
            return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("加载设置文件失败: %s", e)
    return dict(_DEFAULT_SETTINGS)


def _save_settings(settings: dict[str, Any]) -> None:
    """保存设置到 JSON 文件。"""
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def setup_settings_routes() -> None:
    """注册设置路由。"""

    @router.get("", summary="获取系统设置")
    async def get_settings() -> dict[str, Any]:
        return _load_settings()

    @router.put("", summary="更新系统设置")
    async def update_settings(req: SettingsUpdate) -> dict[str, Any]:
        settings = _load_settings()
        if req.theme is not None:
            settings["theme"] = req.theme
        if req.language is not None:
            settings["language"] = req.language
        if req.default_model is not None:
            settings["default_model"] = req.default_model
        if req.default_budget is not None:
            settings["default_budget"] = req.default_budget
        _save_settings(settings)
        return settings
