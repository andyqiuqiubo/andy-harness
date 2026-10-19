"""REST API —— 桌面操作录制与回放（andy-repeat）。

挂载 ``/api/desktop-repeat``。管理面（require_admin）；认证未启用（单用户）自动放行。
Service 由 andy_repeat 插件注册进内核 ServiceRegistry；未激活时各端点返回 503 并附降级说明。

SSE 端点复用 reverse_engineering 的 ``_require_admin_sse`` 范式（EventSource 不能带
自定义头，认证启用时经 ``?token=`` 传令牌）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from harness.api.deps import require_admin
from harness.api.errors import APIError
from harness.modules.desktop_repeat.config import RecordConfig
from harness.modules.desktop_repeat.errors import RepeatError
from harness.modules.desktop_repeat.service import DesktopRepeatService

logger = logging.getLogger("harness.api.desktop_repeat")

router = APIRouter(prefix="/api/desktop-repeat", tags=["desktop-repeat"])


class RecordRequest(BaseModel):
    label: str = ""
    frame_tier: str = ""
    countdown_s: int = -1
    mask_windows: list[str] = Field(default_factory=list)
    mask_keys: list[str] = Field(default_factory=list)
    auto_pause_mask: list[str] = Field(default_factory=list)


class CompileRequest(BaseModel):
    use_llm: bool = False
    save: bool = False
    name: str = ""
    description: str = ""


class DryRunRequest(BaseModel):
    workflow_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class RunRequest(BaseModel):
    workflow_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    unattended: bool = False
    allow_blind_coord: bool = False
    expected_graph_sha256: str | None = None


class ResumeRequest(BaseModel):
    decision: str  # retry | skip | abort


class StopRequest(BaseModel):
    discard: bool = False


class SkillRequest(BaseModel):
    slug: str = ""


class MarkerRequest(BaseModel):
    label: str = ""
    note: str = ""


def _svc() -> DesktopRepeatService:
    from harness.main import _services

    if _services is None or not _services.has(DesktopRepeatService):
        raise APIError("REPEAT_UNAVAILABLE",
                       "andy-repeat 插件未激活或桌面依赖缺失（pip install 'harness[repeat]'）。",
                       503)
    svc: DesktopRepeatService = _services.get(DesktopRepeatService)
    return svc


def _translate(e: RepeatError) -> APIError:
    return APIError(e.code, e.message, 400 if e.recoverable else 422, detail=e.detail)


def _require_admin_sse(request: Request) -> dict[str, Any] | None:
    """SSE 管理员鉴权：认证启用时支持 ?token=（EventSource 无法带自定义头）。"""
    user: dict[str, Any] | None = getattr(request.state, "user", None)
    if user is not None:
        if not isinstance(user, dict) or not user.get("is_admin"):
            raise APIError("FORBIDDEN", "需要管理员权限", 403)
        return user

    from harness.main import _services
    from harness.modules.auth_manager.service import AuthService

    auth_service = _services.get(AuthService) if _services is not None and _services.has(AuthService) else None
    if auth_service is None or not getattr(auth_service, "enabled", False):
        return None  # 认证未启用 → 放行

    token = request.query_params.get("token", "")
    if not token:
        raise APIError("UNAUTHORIZED", "未认证或登录已过期，请重新登录", 401)
    authed = auth_service.user_from_token(token)
    if authed is None:
        raise APIError("UNAUTHORIZED", "登录已失效，请重新登录", 401)
    if not getattr(authed, "is_admin", False):
        raise APIError("FORBIDDEN", "需要管理员权限", 403)
    request.state.user = authed.to_dict()
    out: dict[str, Any] = request.state.user
    return out


@router.get("/status", summary="能力体检", dependencies=[Depends(require_admin)])
async def status() -> dict[str, Any]:
    try:
        return _svc().status()
    except APIError:
        return {"available": False, "reason": "plugin_inactive",
                "active_session": None, "running_runs": [], "config": {}}


@router.post("/sessions", summary="开始录制")
async def start_recording(req: RecordRequest, _a: Any = Depends(require_admin)) -> dict[str, Any]:
    rec = RecordConfig(label=req.label, frame_tier=req.frame_tier, countdown_s=req.countdown_s,
                       mask_windows=req.mask_windows, mask_keys=req.mask_keys,
                       auto_pause_mask=req.auto_pause_mask)
    try:
        return await _svc().start_recording(rec)
    except RepeatError as e:
        raise _translate(e)


@router.post("/sessions/pause", summary="暂停录制")
async def pause_recording(_a: Any = Depends(require_admin)) -> dict[str, Any]:
    try:
        return await _svc().pause_recording()
    except RepeatError as e:
        raise _translate(e)


@router.post("/sessions/resume", summary="恢复录制")
async def resume_recording(_a: Any = Depends(require_admin)) -> dict[str, Any]:
    try:
        return await _svc().resume_recording()
    except RepeatError as e:
        raise _translate(e)


@router.post("/sessions/marker", summary="录制打点")
async def marker(req: MarkerRequest, _a: Any = Depends(require_admin)) -> dict[str, Any]:
    try:
        return await _svc().marker(req.label, req.note)
    except RepeatError as e:
        raise _translate(e)


@router.post("/sessions/stop", summary="停止录制（当前会话）")
async def stop_current(req: StopRequest, _a: Any = Depends(require_admin)) -> dict[str, Any]:
    try:
        return await _svc().stop_recording(None, discard=req.discard)
    except RepeatError as e:
        raise _translate(e)


@router.post("/sessions/{sid}/stop", summary="停止指定会话")
async def stop_recording(sid: str, req: StopRequest,
                         _a: Any = Depends(require_admin)) -> dict[str, Any]:
    try:
        return await _svc().stop_recording(sid, discard=req.discard)
    except RepeatError as e:
        raise _translate(e)


@router.get("/sessions", summary="会话列表")
async def list_sessions(limit: int = Query(50, ge=1, le=200),
                        offset: int = Query(0, ge=0),
                        _a: Any = Depends(require_admin)) -> dict[str, Any]:
    return _svc().list_sessions(limit=limit, offset=offset)


@router.get("/sessions/{sid}", summary="会话详情")
async def get_session(sid: str, _a: Any = Depends(require_admin)) -> dict[str, Any]:
    try:
        return _svc().get_session(sid)
    except RepeatError as e:
        raise _translate(e)


@router.delete("/sessions/{sid}", summary="删除会话")
async def delete_session(sid: str, _a: Any = Depends(require_admin)) -> dict[str, Any]:
    try:
        ok = _svc().delete_session(sid)
    except RepeatError as e:
        raise _translate(e)
    if not ok:
        raise APIError("NOT_FOUND", f"录制会话不存在: {sid}", 404)
    return {"ok": True}


@router.post("/sessions/{sid}/compile", summary="编译为步骤")
async def compile(sid: str, req: CompileRequest,
                  _a: Any = Depends(require_admin)) -> dict[str, Any]:
    svc = _svc()
    try:
        result = await svc.compile_session(sid, use_llm=req.use_llm)
        out = result.to_dict()
        out["draft_graph"] = result.draft_graph
        if req.save:
            wf = await svc.save_as_workflow(sid, req.name or "录制流程", req.description)
            out["workflow_id"] = wf.get("id")
        return out
    except RepeatError as e:
        raise _translate(e)


@router.get("/sessions/{sid}/vision", summary="视觉核对结果")
async def vision_status(sid: str,
                        _a: Any = Depends(require_admin)) -> dict[str, Any]:
    """读取 bundle/vision.json（未分析过时 available=False）。"""
    return _svc().vision_status(sid)


@router.post("/sessions/{sid}/vision", summary="触发视觉核对（旁路，异步）")
async def vision_run(sid: str,
                     _a: Any = Depends(require_admin)) -> dict[str, Any]:
    svc = _svc()
    try:
        doc = svc.get_session(sid)
        if str(doc.get("status", "")) not in ("ready", "sealed", "compiled"):
            raise APIError("RP-6001", "会话未封版，无法视觉核对", 400)
    except RepeatError as e:
        raise _translate(e)

    async def _job() -> None:
        try:
            await svc.vision_analyze(sid)
        except Exception as e:  # noqa: BLE001
            logger.info("vision: 手动触发失败 sid=%s: %s", sid, e)

    asyncio.create_task(_job())
    return {"session_id": sid, "started": True}


@router.post("/dry-run", summary="干跑体检")
async def dry_run(req: DryRunRequest, _a: Any = Depends(require_admin)) -> dict[str, Any]:
    try:
        return await _svc().dry_run(req.workflow_id, req.params)
    except RepeatError as e:
        raise _translate(e)


@router.post("/runs", summary="启动回放")
async def start_run(req: RunRequest, _a: Any = Depends(require_admin)) -> dict[str, Any]:
    try:
        return await _svc().start_run(req.workflow_id, req.params,
                                      unattended=req.unattended,
                                      allow_blind_coord=req.allow_blind_coord,
                                      expected_graph_sha256=req.expected_graph_sha256)
    except RepeatError as e:
        raise _translate(e)


@router.get("/runs", summary="运行历史")
async def list_runs(workflow_id: str | None = None, limit: int = Query(50, ge=1, le=200),
                    _a: Any = Depends(require_admin)) -> list[dict[str, Any]]:
    return _svc().list_runs(workflow_id, limit)


@router.get("/runs/{run_id}", summary="运行详情")
async def get_run(run_id: str, _a: Any = Depends(require_admin)) -> dict[str, Any]:
    st = _svc().get_run(run_id)
    if str(st.get("status", "")) == "not_found":
        raise APIError("NOT_FOUND", f"运行不存在: {run_id}", 404)
    return st


@router.post("/runs/{run_id}/resume", summary="挂起续跑裁决")
async def resume_run(run_id: str, req: ResumeRequest,
                     _a: Any = Depends(require_admin)) -> dict[str, Any]:
    try:
        return await _svc().resume_run(run_id, req.decision)
    except RepeatError as e:
        raise _translate(e)


@router.post("/runs/{run_id}/abort", summary="中止运行")
async def abort_run(run_id: str, reason: str = "user",
                    _a: Any = Depends(require_admin)) -> dict[str, Any]:
    try:
        return await _svc().abort_run(run_id, reason)
    except RepeatError as e:
        raise _translate(e)


@router.get("/runs/{run_id}/events", summary="实时事件流（SSE）")
async def run_events(run_id: str, request: Request,
                     _u: Any = Depends(_require_admin_sse)) -> StreamingResponse:
    import asyncio
    import json as _json

    from harness.modules.desktop_repeat.bus import repeat_progress_store

    svc = _svc()

    # 未知 run 必须在建立 SSE 之前拒绝：否则 EventSource 会一直挂在心跳上
    # （前端表现为"进度面板永远等待"，且连接无法自然收束）。
    if str(svc.get_run(run_id).get("status", "")) == "not_found":
        raise APIError("NOT_FOUND", f"运行不存在: {run_id}", 404)

    async def _stream() -> AsyncIterator[str]:
        # 心跳：15s 一次防代理断流（§10.3）
        if not repeat_progress_store.has_events(run_id):
            st = svc.get_run(run_id)
            if st.get("status") in ("succeeded", "failed", "aborted"):
                # 总线已空（重启后）：从 DB 合成终态，流立即收束
                ev = {"type": "run_done" if st["status"] == "succeeded" else "run_error",
                      "run_id": run_id, "record": st}
                yield "data: " + _json.dumps(ev, ensure_ascii=False) + "\n\n"
                return
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
        done = asyncio.Event()

        async def _pump() -> None:
            try:
                async for ev in svc.subscribe(run_id):
                    await queue.put(ev)
            finally:
                done.set()

        task = asyncio.create_task(_pump())
        try:
            # 带超时的取：超时则发心跳注释行
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield "data: " + _json.dumps(ev, ensure_ascii=False) + "\n\n"
                if ev.get("type") in ("run_done", "run_error"):
                    return
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/workflows/{wf_id}/skill", summary="导出技能 SKILL.md")
async def export_skill(wf_id: str, req: SkillRequest,
                       _a: Any = Depends(require_admin)) -> dict[str, Any]:
    try:
        return await _svc().export_skill(wf_id, slug=req.slug or None)
    except RepeatError as e:
        raise _translate(e)
