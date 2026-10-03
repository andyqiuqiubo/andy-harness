"""REST API —— 会话附件：上传（multipart）与按 mime 回传（图片缩略图）。"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Request, UploadFile
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
    _safe_stem,
    classify,
    store_file,
)

logger = logging.getLogger("harness.api.rest.attachments")

router = APIRouter(prefix="/api/sessions", tags=["attachments"])


def setup_attachment_routes(registry: ServiceRegistry) -> None:
    """注册附件路由（依赖注入 ServiceRegistry）。"""

    def _session_service() -> Any:
        from harness.modules.session_manager.service import SessionService

        try:
            return registry.get(SessionService)
        except Exception:
            raise APIError("SESSION_SERVICE_UNAVAILABLE", "会话服务不可用", 503)

    def _owner_user_id(request: Request) -> str:
        """E12：从认证中间件注入的 request.state.user 取用户 id。

        未启用认证时 request.state.user 不存在，返回 ""（不过滤，行为不变）。
        """
        user = getattr(request.state, "user", None)
        if not isinstance(user, dict):
            return ""
        return str(user.get("id") or "")

    @router.post("/{session_id}/attachments", summary="上传附件（文档 / 图片）")
    async def upload_attachments(
        session_id: str,
        files: list[UploadFile] = File(...),
        request: Request = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """批量上传附件。

        校验类型与大小，落盘并登记元信息，返回对外公开元信息
        （id/kind/filename/mime/size，不含存储路径）。
        """
        # E12：启用认证时，仅允许上传到「自己归属」的会话（否则 404）。
        if not _session_service().get_session(session_id, user_id=_owner_user_id(request)):
            raise APIError("SESSION_NOT_FOUND", "会话不存在或无权访问", 404)
        if not files:
            raise APIError("NO_FILES", "未提供文件", 400)
        # P1-4：数量必须在读取任何文件内容**之前**校验。
        # 旧实现先 `await f.read()` 把全部文件读进内存，再判 `len(pending) > MAX`，
        # 于是单次 POST 上千个文件就能在服务端堆出数百 MB 后才返回 400。
        if len(files) > MAX_FILES_PER_MESSAGE:
            raise APIError(
                "ATTACHMENT_LIMIT",
                f"单次最多上传 {MAX_FILES_PER_MESSAGE} 个文件",
                400,
            )

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
        # P1-4：落盘 + 登记不再是「尽力而为」——任一步失败都要回滚，
        # 否则 `store_file` 成功但 `repo.create` 失败会留下孤儿文件，
        # 中途抛错也会让已登记的部分没有对应回滚。
        written_paths: list[Path] = []
        created_ids: list[str] = []
        results: list[dict[str, Any]] = []
        try:
            for filename, kind, mime, data in pending:
                att_id = uuid.uuid4().hex
                path = store_file(session_id, data, filename, kind, mime, att_id)
                written_paths.append(Path(path))
                meta = repo.create(att_id, session_id, kind, filename, mime, len(data), path)
                created_ids.append(att_id)
                results.append(meta)
        except Exception as e:
            logger.error("保存附件失败，开始回滚: %s", e)
            try:
                repo.delete_by_ids(created_ids)
            except Exception:
                pass
            for file_path in written_paths:
                try:
                    if file_path.exists():
                        file_path.unlink()
                except Exception:
                    pass
            raise APIError("ATTACHMENT_STORE_FAILED", "附件保存失败", 500)
        return {"attachments": results}

    @router.get(
        "/{session_id}/attachments/{att_id}",
        summary="按 mime 回传附件（图片缩略图等）",
    )
    async def serve_attachment(
        session_id: str,
        att_id: str,
        request: Request = None,  # type: ignore[assignment]
    ) -> FileResponse:
        repo = AttachmentRepository(registry.get(Database))
        att = repo.get(att_id)
        if not att or att["session_id"] != session_id:
            raise APIError("ATTACHMENT_NOT_FOUND", "附件不存在", 404)
        # E12：启用认证时，附件归属的会话必须属于当前用户（两跳：att→session→user）。
        # 未启用认证时 user_id="" 不做过滤，行为与历史一致。
        owner = _owner_user_id(request)
        if owner and not _session_service().get_session(att["session_id"], user_id=owner):
            raise APIError("ATTACHMENT_NOT_FOUND", "附件不存在或无权访问", 404)
        # P2-8：用净化后的文件名做 Content-Disposition，避免原始名中的
        # 非 ASCII / 特殊字符在不同浏览器下解码不一致（storage_path 才是落盘路径，
        # 该值不参与任何路径解析，故不是路径穿越问题）。
        return FileResponse(
            att["storage_path"],
            media_type=att["mime"],
            filename=_safe_stem(att["filename"]),
        )
