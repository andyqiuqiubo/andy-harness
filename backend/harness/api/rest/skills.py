"""REST API —— Skill 管理路由。

提供 Skill 列表 / 详情 / 重新扫描 / 启用停用。
Skill 服务不可用时返回空列表而非 503，保证前端可用（优雅降级）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from harness.kernel.services import ServiceRegistry
from harness.modules.skill_manager.service import SkillService

logger = logging.getLogger("harness.api.skills")

router = APIRouter(prefix="/api/skills", tags=["skills"])

_services: ServiceRegistry | None = None


class SkillToggleRequest(BaseModel):
    """启用/停用 Skill 请求。"""

    enabled: bool = Field(description="是否启用")


def setup_skill_routes(services: ServiceRegistry) -> None:
    """注册 Skill 路由（注入服务注册表）。"""
    global _services
    _services = services


def _get_service() -> SkillService | None:
    """获取 SkillService，不可用返回 None。"""
    if _services is None or not _services.has(SkillService):
        return None
    service: SkillService = _services.get(SkillService)
    return service


@router.get("", summary="列出所有 Skill")
async def list_skills() -> dict[str, Any]:
    """列出所有已发现的 Skill（含启用状态与随附资源）。"""
    service = _get_service()
    if service is None:
        return {"skills": [], "count": 0, "available": False}
    skills = [m.to_dict() for m in service.list_skills()]
    return {"skills": skills, "count": len(skills), "available": True}


@router.post("/reload", summary="重新扫描 Skill")
async def reload_skills() -> dict[str, Any]:
    """重新扫描磁盘上的 SKILL.md（新增/修改 Skill 后调用）。"""
    service = _get_service()
    if service is None:
        return {"count": 0, "available": False}
    count = service.reload()
    return {"count": count, "available": True}


@router.get("/catalog", summary="获取注入 system prompt 的 Skill 目录（L1）")
async def get_catalog() -> dict[str, Any]:
    """返回实际注入模型上下文的目录文本，便于核对 token 开销。"""
    service = _get_service()
    if service is None:
        return {"catalog": "", "available": False}
    return {"catalog": service.render_catalog(), "available": True}


@router.get("/{name}", summary="获取单个 Skill 详情")
async def get_skill(name: str) -> dict[str, Any]:
    """获取 Skill 元数据 + 正文。未找到返回 404。"""
    from fastapi import HTTPException

    service = _get_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Skill 服务不可用")
    meta = service.get_skill(name)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Skill 未找到: {name}")
    return {
        "meta": meta.to_dict(),
        "body": service.load_body(name),
        "resources": service.list_resources(name),
    }


@router.patch("/{name}", summary="启用/停用 Skill")
async def toggle_skill(name: str, req: SkillToggleRequest) -> dict[str, Any]:
    """启用或停用 Skill（持久化）。"""
    from fastapi import HTTPException

    service = _get_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Skill 服务不可用")
    if not service.set_enabled(name, req.enabled):
        raise HTTPException(status_code=404, detail=f"Skill 未找到: {name}")
    meta = service.get_skill(name)
    return {"name": name, "enabled": bool(meta and meta.enabled)}


@router.get("/{name}/resource", summary="读取 Skill 随附资源")
async def read_skill_resource(name: str, path: str) -> dict[str, Any]:
    """读取 Skill 的 scripts/ references/ assets/ 资源内容。"""
    from fastapi import HTTPException

    service = _get_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Skill 服务不可用")
    if service.get_skill(name) is None:
        raise HTTPException(status_code=404, detail=f"Skill 未找到: {name}")
    return {"name": name, "path": path, "content": service.read_resource(name, path)}
