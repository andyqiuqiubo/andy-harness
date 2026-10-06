"""REST API —— 插件开发者工作台（DevKit）：脚手架 + 热重载。

挂载在 ``/api/plugins/dev`` 前缀下；全部接口 ``require_admin``。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from harness.api.deps import require_admin
from harness.api.errors import APIError
from harness.modules.devkit.service import list_templates, reload_plugin, scaffold

logger = logging.getLogger("harness.api.devkit")

router = APIRouter(prefix="/api/plugins/dev", tags=["plugin-devkit"])

# 插件目录（backend/plugins）
_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "plugins"


def _loader() -> Any:
    from harness.main import _loader  # noqa: PLC0415

    return _loader


class ScaffoldRequest(BaseModel):
    """脚手架请求。"""

    template: str
    id: str
    name: str
    description: str = ""


@router.get("/templates", summary="列出可用脚手架模板")
async def get_templates(
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> list[dict[str, str]]:
    return list_templates()


@router.post("/scaffold", summary="按模板生成插件目录")
async def scaffold_plugin(
    req: ScaffoldRequest,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    try:
        result = scaffold(_PLUGINS_DIR, req.template, req.id, req.name, req.description)
    except ValueError as e:
        raise APIError("DEVKIT_INVALID", str(e), 400)
    return {
        **result,
        "hint": "插件目录已生成。修改 main.py 后点「重载」即可生效（无需重启后端）。",
    }


@router.post("/reload/{plugin_id}", summary="热重载插件（停用→卸载→清缓存→重载→激活）")
async def reload_plugin_route(
    plugin_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return await reload_plugin(_loader(), _PLUGINS_DIR, plugin_id)
    except ValueError as e:
        raise APIError("DEVKIT_INVALID", str(e), 400)
    except Exception as e:  # noqa: BLE001 - 加载/激活失败要把错误带给开发者
        raise APIError("DEVKIT_RELOAD_FAIL", f"重载失败: {e}", 400)
