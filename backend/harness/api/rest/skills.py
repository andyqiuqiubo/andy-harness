"""REST API —— Skill 管理路由。

提供 Skill 列表 / 详情 / 重新扫描 / 启用停用。
Skill 服务不可用时返回空列表而非 503，保证前端可用（优雅降级）。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from harness.kernel.services import ServiceRegistry
from harness.modules.package_installer.service import PackageInstaller
from harness.modules.skill_manager.service import (
    SKILL_FILENAME,
    SkillService,
    parse_frontmatter,
)

logger = logging.getLogger("harness.api.skills")

router = APIRouter(prefix="/api/skills", tags=["skills"])

_services: ServiceRegistry | None = None

# 技能市场目录（内置可安装技能包）：<backend>/skill_marketplace/<name>/SKILL.md
_SKILL_MARKETPLACE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "skill_marketplace"


def _user_skills_dir() -> Path:
    """用户级 skills 目录（default_skill_roots 会扫描它）。"""
    return Path.home() / ".andy-harness" / "skills"


class SkillToggleRequest(BaseModel):
    """启用/停用 Skill 请求。"""

    enabled: bool = Field(description="是否启用")


class ExternalInstallRequest(BaseModel):
    """从 zip / git 安装 Skill 请求。"""

    source: str
    kind: str = "zip"  # "zip" | "git"
    ref: str | None = None  # git 分支 / tag / sha
    subdir: str | None = None  # 包嵌套在子目录时指定


def _slugify(name: str) -> str:
    """把 Skill 名称规范成安全的目录 / 包标识。"""
    import re

    s = re.sub(r"[^\w]+", "-", name.strip().lower())
    return re.sub(r"-+", "-", s).strip("-")


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


# ── 技能市场 ──────────────────────────────────────────
def _iter_marketplace_packages() -> list[Path]:
    """列出技能市场包目录（每个含 SKILL.md）。"""
    if not _SKILL_MARKETPLACE_DIR.is_dir():
        return []
    return sorted(
        (p for p in _SKILL_MARKETPLACE_DIR.iterdir() if (p / SKILL_FILENAME).is_file()),
        key=lambda p: p.name,
    )


@router.get("/marketplace", summary="列出技能市场")
async def skill_marketplace() -> list[dict[str, Any]]:
    """列出技能市场中可安装的 Skill（含已安装标记）。"""
    user_dir = _user_skills_dir()
    items: list[dict[str, Any]] = []
    for pkg in _iter_marketplace_packages():
        try:
            text = (pkg / SKILL_FILENAME).read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        meta_raw, body = parse_frontmatter(text)
        name = meta_raw.get("name", pkg.name)
        items.append(
            {
                "name": name,
                "package_id": pkg.name,
                "description": meta_raw.get("description", ""),
                "version": meta_raw.get("version", ""),
                "license": meta_raw.get("license", ""),
                "long_description": body,
                "installed": (user_dir / pkg.name).is_dir(),
            }
        )
    return items


@router.post("/marketplace/{package_id}/install", summary="从技能市场安装 Skill")
async def install_skill_marketplace(package_id: str) -> dict[str, Any]:
    """把技能市场包复制到用户级 skills 目录并重新扫描。"""
    service = _get_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Skill 服务不可用")

    pkg_dir = _SKILL_MARKETPLACE_DIR / package_id
    if not (pkg_dir / SKILL_FILENAME).is_file():
        raise HTTPException(status_code=404, detail=f"技能市场中不存在: {package_id}")

    dest = _user_skills_dir() / package_id
    if dest.exists():
        raise HTTPException(status_code=409, detail=f"该 Skill 已安装: {package_id}")

    try:
        shutil.copytree(pkg_dir, dest)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"复制失败: {e}") from e

    count = service.reload()
    logger.info("已从技能市场安装 Skill: %s", package_id)
    return {"status": "installed", "package_id": package_id, "count": count}


@router.post("/install-external", summary="从 zip / git 安装 Skill")
async def install_external_skill(req: ExternalInstallRequest) -> dict[str, Any]:
    """从外部 zip / git 仓库安装第三方 Skill 包（生态分发，E6）。"""
    service = _get_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Skill 服务不可用")

    installer = PackageInstaller()
    res = installer.materialize(req.source, req.kind, ref=req.ref, subdir=req.subdir)
    if not res.success:
        raise HTTPException(status_code=400, detail=res.error)

    pkg_dir = Path(res.path)
    skill_file = pkg_dir / SKILL_FILENAME
    if not skill_file.is_file():
        raise HTTPException(status_code=400, detail="包根目录缺少 SKILL.md")

    meta_raw, _ = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    name = meta_raw.get("name") or pkg_dir.name
    package_id = _slugify(name) or pkg_dir.name

    dest = _user_skills_dir() / package_id
    if dest.exists():
        raise HTTPException(status_code=409, detail=f"该 Skill 已安装: {package_id}")

    try:
        shutil.copytree(pkg_dir, dest)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"复制失败: {e}") from e

    count = service.reload()
    logger.info("已从外部安装 Skill: %s (%s)", package_id, req.kind)
    return {"status": "installed", "package_id": package_id, "count": count}


@router.delete("/marketplace/{package_id}", summary="卸载技能市场的 Skill")
async def uninstall_skill_marketplace(package_id: str) -> dict[str, Any]:
    """删除用户级 skills 目录下对应的 Skill 包并重新扫描。"""
    service = _get_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Skill 服务不可用")

    dest = _user_skills_dir() / package_id
    if not dest.is_dir():
        raise HTTPException(status_code=404, detail=f"该 Skill 未安装: {package_id}")

    try:
        shutil.rmtree(dest)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"删除失败: {e}") from e

    count = service.reload()
    logger.info("已卸载技能市场 Skill: %s", package_id)
    return {"status": "uninstalled", "package_id": package_id, "count": count}


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
