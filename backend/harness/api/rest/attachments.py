"""REST API —— 会话附件：上传（multipart）与按 mime 回传（图片缩略图）。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import FileResponse

from harness.api.errors import APIError
from harness.infra.database import Database
from harness.infra.repository import AttachmentRepository
from harness.kernel.services import ServiceRegistry
from harness.modules.attachment.limits import (
    MAX_DOCUMENTS_PER_MESSAGE,
    MAX_FILES_PER_MESSAGE,
    MAX_IMAGES_PER_MESSAGE,
)
from harness.modules.attachment.service import (
    AttachmentError,
    classify,
    store_file,
)

router = APIRouter(prefix="/api/sessions", tags=["attachments"])


def setup_attachment_routes(registry: ServiceRegistry) -> None:
    """注册附件路由（依赖注入 ServiceRegistry）。"""

    def _session_service() -> Any:
        from harness.modules.session_manager.service import SessionService

        try:
            return registry.get(SessionService)
        except Exception:
            raise APIError("SESSION_SERVICE_UNAVAILABLE", "会话服务不可用", 503)

    @router.post("/{session_id}/attachments", summary="上传附件（文档 / 图片）")
    async def upload_attachments(
        session_id: str,
        files: list[UploadFile] = File(...),
    ) -> dict[str, Any]:
        """批量上传附件。

        校验类型与大小，落盘并登记元信息，返回对外公开元信息
        （id/kind/filename/mime/size，不含存储路径）。
        """
        if not _session_service().get_session(session_id):
            raise APIError("SESSION_NOT_FOUND", "会话不存在", 404)
        if not files:
            raise APIError("NO_FILES", "未提供文件", 400)

        # 先全部读取并校验，任一不合法则整体拒绝（避免产生孤儿文件）
        pending: list[tuple[str, str, str, bytes]] = []
        doc_count = img_count = 0
        for f in files:
            filename = f.filename or "unknown"
            data = await f.read()
            try:
                kind, mime = classify(filename, len(data))
            except AttachmentError as e:
                raise APIError("ATTACHMENT_INVALID", str(e), 400)
            if kind == "document":
                doc_count += 1
            else:
                img_count += 1
            pending.append((filename, kind, mime, data))

        if (
            doc_count > MAX_DOCUMENTS_PER_MESSAGE
            or img_count > MAX_IMAGES_PER_MESSAGE
            or len(pending) > MAX_FILES_PER_MESSAGE
        ):
            raise APIError(
                "ATTACHMENT_LIMIT",
                (
                    f"超出单条消息附件上限：文档≤{MAX_DOCUMENTS_PER_MESSAGE}，"
                    f"图片≤{MAX_IMAGES_PER_MESSAGE}，总计≤{MAX_FILES_PER_MESSAGE}"
                ),
                400,
            )

        repo = AttachmentRepository(registry.get(Database))
        results: list[dict[str, Any]] = []
        for filename, kind, mime, data in pending:
            att_id = uuid.uuid4().hex
            path = store_file(session_id, data, filename, kind, mime, att_id)
            meta = repo.create(
                att_id, session_id, kind, filename, mime, len(data), path
            )
            results.append(meta)
        return {"attachments": results}

    @router.get(
        "/{session_id}/attachments/{att_id}",
        summary="按 mime 回传附件（图片缩略图等）",
    )
    async def serve_attachment(session_id: str, att_id: str) -> FileResponse:
        repo = AttachmentRepository(registry.get(Database))
        att = repo.get(att_id)
        if not att or att["session_id"] != session_id:
            raise APIError("ATTACHMENT_NOT_FOUND", "附件不存在", 404)
        return FileResponse(
            att["storage_path"], media_type=att["mime"], filename=att["filename"]
        )
