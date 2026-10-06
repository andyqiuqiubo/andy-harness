"""REST API —— 工作流编排器（图编排版本）。

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
from harness.modules.workflows.service import WorkflowService

logger = logging.getLogger("harness.api.workflows")

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


class WorkflowGraphCreate(BaseModel):
    """创建工作流请求（图编排）。"""

    name: str
    description: str = ""
    graph: dict[str, Any] = Field(default_factory=lambda: {"nodes": [], "edges": []})


class WorkflowGraphUpdate(BaseModel):
    """更新工作流请求。"""

    name: str
    description: str = ""
    graph: dict[str, Any] = Field(default_factory=lambda: {"nodes": [], "edges": []})


class WorkflowRunCreate(BaseModel):
    """运行工作流请求。"""

    inputs: dict[str, Any] = Field(default_factory=dict)


def _service() -> WorkflowService:
    from harness.kernel.services import ServiceRegistry
    from harness.main import _services  # noqa: PLC0415

    registry: ServiceRegistry = _services
    return WorkflowService(Database(), registry)


@router.get("", summary="列出工作流")
async def list_workflows(
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> list[dict[str, Any]]:
    return _service().list_workflows()


@router.post("", summary="创建工作流")
async def create_workflow(
    req: WorkflowGraphCreate,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return _service().create_workflow(req.name, req.description, req.graph)
    except ValueError as e:
        raise APIError("WORKFLOW_INVALID", str(e), 400)


@router.get("/runs", summary="运行历史（可按工作流过滤）")
async def list_runs(
    workflow_id: str | None = Query(default=None),
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> list[dict[str, Any]]:
    return _service().list_runs(workflow_id)


@router.get("/{workflow_id}", summary="工作流详情")
async def get_workflow(
    workflow_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    wf = _service().get_workflow(workflow_id)
    if wf is None:
        raise APIError("WORKFLOW_NOT_FOUND", f"工作流不存在: {workflow_id}", 404)
    return wf


@router.put("/{workflow_id}", summary="更新工作流（保存画布图）")
async def update_workflow(
    workflow_id: str,
    req: WorkflowGraphUpdate,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return _service().update_workflow(workflow_id, req.name, req.description, req.graph)
    except KeyError as e:
        raise APIError("WORKFLOW_NOT_FOUND", str(e), 404)
    except ValueError as e:
        raise APIError("WORKFLOW_INVALID", str(e), 400)


@router.delete("/{workflow_id}", summary="删除工作流（连同运行历史）")
async def delete_workflow(
    workflow_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, str]:
    if not _service().delete_workflow(workflow_id):
        raise APIError("WORKFLOW_NOT_FOUND", f"工作流不存在: {workflow_id}", 404)
    return {"status": "deleted"}


@router.post("/{workflow_id}/run", summary="运行工作流（同步执行，返回节点级结果）")
async def create_run(
    workflow_id: str,
    req: WorkflowRunCreate,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return await _service().run_workflow(workflow_id, req.inputs)
    except KeyError as e:
        raise APIError("WORKFLOW_NOT_FOUND", str(e), 404)
    except ValueError as e:
        raise APIError("WORKFLOW_INVALID", str(e), 400)


@router.get("/runs/{run_id}", summary="运行详情（步骤状态与输出）")
async def get_run(
    run_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    run = _service().get_run(run_id)
    if run is None:
        raise APIError("WORKFLOW_RUN_NOT_FOUND", f"运行不存在: {run_id}", 404)
    return run
