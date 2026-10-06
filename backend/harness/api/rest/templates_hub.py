"""REST API —— 模板分享中心。

管理面接口（``require_admin``），未启用认证（单用户）时自动放行。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from harness.api.deps import require_admin
from harness.api.errors import APIError
from harness.infra.database import Database
from harness.modules.session_manager.service import SessionService
from harness.modules.templates_hub.service import TemplateHubService

logger = logging.getLogger("harness.api.templates")

router = APIRouter(prefix="/api/templates", tags=["template-hub"])


class TemplateCreate(BaseModel):
    """创建模板请求。"""

    type: str
    name: str
    description: str = ""
    vars_schema: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    source_session_id: str | None = None


class TemplateFromSession(BaseModel):
    """从已有会话打包为模板。"""

    session_id: str
    name: str
    description: str = ""
    variables: list[str] = Field(default_factory=list)


class TemplateInstantiate(BaseModel):
    """实例化模板请求。"""

    vars: dict[str, Any] = Field(default_factory=dict)


def _service() -> TemplateHubService:
    return TemplateHubService(Database(), _session_service())


def _session_service() -> Any:
    from harness.main import _services  # noqa: PLC0415

    try:
        return _services.get(SessionService)
    except Exception:  # noqa: BLE001
        return None


@router.get("", summary="列出模板（可按类型过滤）")
async def list_templates(
    type: str | None = Query(default=None),
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> list[dict[str, Any]]:
    return _service().list_templates(type)


@router.post("", summary="创建模板（直接给 payload，或导入分享包）")
async def create_template(
    req: TemplateCreate,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return _service().create_template(
            req.type, req.name, req.description, req.vars_schema, req.payload, req.source_session_id
        )
    except ValueError as e:
        raise APIError("TEMPLATE_INVALID", str(e), 400)


@router.post("/from-session", summary="从已有会话打包为模板")
async def create_from_session(
    req: TemplateFromSession,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return _service().create_from_session(req.session_id, req.name, req.description, req.variables)
    except ValueError as e:
        raise APIError("TEMPLATE_INVALID", str(e), 400)


@router.get("/{template_id}", summary="模板详情（含 payload）")
async def get_template(
    template_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    tpl = _service().get_template(template_id)
    if tpl is None:
        raise APIError("TEMPLATE_NOT_FOUND", f"模板不存在: {template_id}", 404)
    return tpl


@router.delete("/{template_id}", summary="删除模板")
async def delete_template(
    template_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, str]:
    if not _service().delete_template(template_id):
        raise APIError("TEMPLATE_NOT_FOUND", f"模板不存在: {template_id}", 404)
    return {"status": "deleted"}


@router.post("/{template_id}/instantiate", summary="实例化模板（渲染变量并落地为新会话/工作流）")
async def instantiate_template(
    template_id: str,
    req: TemplateInstantiate,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return _service().instantiate(template_id, req.vars)
    except KeyError as e:
        raise APIError("TEMPLATE_NOT_FOUND", str(e), 404)
    except ValueError as e:
        raise APIError("TEMPLATE_INVALID", str(e), 400)
