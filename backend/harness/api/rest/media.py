"""REST API —— 图像 / 视频生成（media_gen）。

前端会话页选中 CogView-3-Flash / CogVideoX-Flash 等生成类模型时走这组接口。
上游为异步任务，前端提交后短轮询 ``GET /api/media/tasks/{id}?poll=1``，
完成后从 ``/api/media/tasks/{id}/content`` 取回已落盘的产物。
生成中用户可点「停止生成」：``POST /api/media/tasks/{id}/stop`` 置
CANCELLED（智谱无取消 API，本端即刻断开轮询，见 service.stop_task）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from harness.api.errors import APIError
from harness.modules.media_gen.service import (
    COGVIDEO_FLASH_SIZES,
    COGVIEW_SIZES,
    MediaGenError,
    MediaGenService,
)

logger = logging.getLogger("harness.api.media")

router = APIRouter(prefix="/api/media", tags=["media"])

_SERVICE: MediaGenService | None = None


def get_media_service() -> MediaGenService:
    """进程内单例（插件激活时可通过它注入 API Key）。"""
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = MediaGenService()
    return _SERVICE


class GenerationRequest(BaseModel):
    """创建生成任务请求。"""

    kind: str = Field(description="image | video")
    model: str = Field(default="cogvideox-flash")
    prompt: str = ""
    image: str = Field(default="", description="本地路径或 URL，图生视频时传入")
    size: str | None = None
    fps: int = 30
    quality: str = "quality"
    # 视频时长：None 表示按模型默认（cogvideox-flash=5，cogvideox-2/3=10）
    # 官方通用说明为 5/10 秒，但免费档 flash 实测仅 5 秒，越界值被静默忽略
    duration: int | None = None
    with_audio: bool = False
    watermark: bool = False
    session_id: str = ""
    provider_id: str = "zhipu"
    attempts: int = Field(default=4, ge=1, le=10)
    # 最长等待（默认 1 小时；**0 = 不限等待**，一直等智谱给出终态）
    max_wait_seconds: int | None = Field(default=None, ge=0, le=7 * 24 * 3600)
    # 参考图片元信息 [{id, filename, mime}]，落库供刷新后恢复参考图
    attachments: list[dict[str, Any]] | None = None


def setup_media_routes(registry: Any = None) -> MediaGenService:
    """装配媒体生成服务：单例注册进服务注册表，供 provider 插件注入 API Key。"""
    svc = get_media_service()
    if registry is not None:
        try:
            if not registry.has(MediaGenService):
                registry.register(MediaGenService, svc, owner="core")
        except Exception as e:  # noqa: BLE001
            logger.warning("MediaGenService 注册失败（不影响路由）: %s", e)
    return svc


@router.post("/generations", summary="创建图像/视频生成任务")
async def create_generation(req: GenerationRequest) -> dict[str, Any]:
    svc = get_media_service()
    try:
        task = await svc.create_task(
            kind=req.kind, model=req.model, prompt=req.prompt, image=req.image,
            size=req.size, fps=req.fps, quality=req.quality, duration=req.duration,
            with_audio=req.with_audio, watermark=req.watermark,
            session_id=req.session_id, provider_id=req.provider_id,
            attempts=req.attempts,
            max_wait_seconds=req.max_wait_seconds,
            attachments=req.attachments,
        )
    except MediaGenError as e:
        raise APIError(status_code=400, code="MEDIA_GEN_ERROR", message=str(e)) from e
    return task


@router.get("/tasks", summary="列出生成任务")
async def list_tasks(session_id: str = "", limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
    return get_media_service().list_tasks(session_id=session_id, limit=limit)


@router.get("/sizes", summary="查询各模型支持的分辨率")
async def list_sizes() -> dict[str, Any]:
    return {"image": COGVIEW_SIZES, "video": COGVIDEO_FLASH_SIZES, "fps": [30, 60]}


@router.get("/tasks/{task_id}", summary="查询任务状态")
async def get_task(task_id: str, poll: bool = Query(default=False)) -> dict[str, Any]:
    try:
        return await get_media_service().get_task(task_id, poll=poll)
    except MediaGenError as e:
        raise APIError(status_code=404, code="MEDIA_TASK_NOT_FOUND", message=str(e)) from e


@router.post("/tasks/{task_id}/stop", summary="停止（取消）进行中的生成任务")
async def stop_generation(task_id: str) -> dict[str, Any]:
    """用户主动停止：活跃任务 → CANCELLED（幂等）；智谱无取消 API，本端断开轮询。"""
    try:
        return get_media_service().stop_task(task_id)
    except MediaGenError as e:
        raise APIError(status_code=404, code="MEDIA_TASK_NOT_FOUND", message=str(e)) from e


@router.get("/tasks/{task_id}/content", summary="下载生成产物")
async def get_task_content(task_id: str) -> Response:
    svc = get_media_service()
    try:
        data, mime = svc.read_file(task_id)
    except MediaGenError as e:
        raise APIError(status_code=404, code="MEDIA_CONTENT_NOT_FOUND", message=str(e)) from e
    # Content-Disposition 让浏览器落到正确扩展名（图片 jpg/png、视频 mp4），
    # 与前端 <a download> 双保险；inline 展示（<img>/<video>）不受影响。
    ext_map = {"mp4": "mp4", "jpeg": "jpg", "jpg": "jpg", "png": "png"}
    ext = ext_map.get(mime.rsplit("/", 1)[-1] if mime else "", "bin")
    filename = f"andy-harness_{task_id[:8]}.{ext}"
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
