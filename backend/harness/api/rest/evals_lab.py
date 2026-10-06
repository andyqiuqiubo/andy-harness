"""REST API —— 评测实验室（数据集 + 对比运行）。

与 ``permission_audit.py`` 同构：管理面接口挂 ``Depends(require_admin)``，
认证未启用（单用户）时自动放行。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from harness.api.deps import require_admin
from harness.api.errors import APIError
from harness.infra.database import Database
from harness.modules.evals_lab.service import EvalLabService

logger = logging.getLogger("harness.api.evals_lab")

router = APIRouter(prefix="/api/evals", tags=["evals-lab"])


class DatasetCreate(BaseModel):
    """创建数据集请求。"""

    name: str
    description: str = ""
    cases: list[dict[str, Any]] = Field(default_factory=list)


class ComboSpec(BaseModel):
    """一个对比组合：provider × 模型 × 系统提示词。"""

    provider_id: str = "deepseek"
    model: str = ""
    system_prompt: str = ""


class RunCreate(BaseModel):
    """创建对比运行请求。"""

    dataset_id: str
    combos: list[ComboSpec] = Field(min_length=1)


def _service() -> EvalLabService:
    from harness.engine.tool_registry import ToolRegistry
    from harness.main import _services  # noqa: PLC0415

    tool_registry: ToolRegistry | None = None
    try:
        tool_registry = _services.get(ToolRegistry)
    except Exception:  # noqa: BLE001
        tool_registry = None
    return EvalLabService(Database(), tool_registry)


def _not_found(detail: str) -> APIError:
    return APIError("EVAL_NOT_FOUND", detail, 404)


@router.get("/datasets", summary="列出评测数据集")
async def list_datasets(
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> list[dict[str, Any]]:
    return _service().list_datasets()


@router.post("/datasets", summary="创建评测数据集")
async def create_dataset(
    req: DatasetCreate,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    if not req.name.strip():
        raise APIError("EVAL_INVALID", "数据集名称不能为空", 400)
    if not req.cases:
        raise APIError("EVAL_INVALID", "数据集至少需要一条用例", 400)
    try:
        return _service().create_dataset(req.name.strip(), req.description, req.cases)
    except Exception as e:  # noqa: BLE001
        raise APIError("EVAL_INVALID", f"用例格式无效: {e}", 400)


@router.get("/datasets/{dataset_id}", summary="获取数据集详情")
async def get_dataset(
    dataset_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    ds = _service().get_dataset(dataset_id)
    if ds is None:
        raise _not_found(f"数据集不存在: {dataset_id}")
    return ds


@router.delete("/datasets/{dataset_id}", summary="删除数据集")
async def delete_dataset(
    dataset_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, str]:
    if not _service().delete_dataset(dataset_id):
        raise _not_found(f"数据集不存在: {dataset_id}")
    return {"status": "deleted"}


@router.get("/runs", summary="列出评测运行（可按数据集过滤）")
async def list_runs(
    dataset_id: str | None = Query(default=None),
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> list[dict[str, Any]]:
    return _service().list_runs(dataset_id)


@router.post("/runs", summary="创建对比运行（后台执行，轮询获取进度）")
async def create_run(
    req: RunCreate,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return _service().create_run(req.dataset_id, [c.model_dump() for c in req.combos])
    except KeyError as e:
        raise _not_found(str(e))
    except ValueError as e:
        raise APIError("EVAL_INVALID", str(e), 400)


@router.get("/runs/{run_id}", summary="获取运行详情（含逐用例结果）")
async def get_run(
    run_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    run = _service().get_run(run_id)
    if run is None:
        raise _not_found(f"运行不存在: {run_id}")
    return run


@router.delete("/runs/{run_id}", summary="删除单条评测运行记录")
async def delete_run(
    run_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, str]:
    if not _service().delete_run(run_id):
        raise _not_found(f"运行不存在: {run_id}")
    return {"status": "deleted"}
