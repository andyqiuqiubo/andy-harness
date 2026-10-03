"""附件业务逻辑：类型/大小校验、落盘、以及把附件渲染为多模态内容。

设计要点：
- 文档：作为文本抽取后内联进 prompt（模型直接可读）。
- 图像：读取后（必要时用 Pillow 缩放）以 base64 data URL 形式作为 image_url
  内容块发送给 OpenAI 兼容的视觉接口。
- 多模态 content 仅在消息确实带附件时构造为 list；否则保持纯文本字符串，
  兼容纯文本模型与既有逻辑。
"""

from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path
from typing import Any

from harness.modules.attachment.limits import (
    MAX_DOCUMENT_SIZE,
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_SIZE,
    attachments_dir,
    ext_of,
)


class AttachmentError(Exception):
    """附件校验失败（类型不支持 / 超出大小或数量上限）。"""


# 常见后缀 -> MIME
_MIME_BY_EXT: dict[str, str] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".log": "text/plain",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".jsx": "text/javascript",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "application/xml",
    ".ini": "text/plain",
    ".toml": "text/plain",
    ".cfg": "text/plain",
    ".tex": "text/plain",
    ".rst": "text/plain",
    ".sh": "text/x-sh",
    ".bat": "text/plain",
    ".ps1": "text/plain",
    ".env.example": "text/plain",
    ".gitignore": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def classify(filename: str, size: int) -> tuple[str, str]:
    """判定文件种类与 MIME，并在不支持或超限时抛出 AttachmentError。"""
    from harness.modules.attachment.limits import (
        DOCUMENT_EXTENSIONS,
        IMAGE_EXTENSIONS,
    )

    ext = ext_of(filename)
    if ext in DOCUMENT_EXTENSIONS:
        kind = "document"
        if size > MAX_DOCUMENT_SIZE:
            raise AttachmentError(f"文档 {filename} 过大（{size} 字节，上限 {MAX_DOCUMENT_SIZE} 字节）")
    elif ext in IMAGE_EXTENSIONS:
        kind = "image"
        if size > MAX_IMAGE_SIZE:
            raise AttachmentError(f"图片 {filename} 过大（{size} 字节，上限 {MAX_IMAGE_SIZE} 字节）")
    else:
        raise AttachmentError(f"不支持的文件类型: {filename}（后缀 {ext or '无'}）")
    return kind, _MIME_BY_EXT.get(ext, "application/octet-stream")


def _safe_stem(filename: str) -> str:
    """仅保留文件名中的安全字符，避免路径穿越与歧义。"""
    stem = os.path.basename(filename)
    # 去掉可能干扰的字符，保留字母数字、点、下划线、连字符
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in stem)
    return cleaned or "file"


def store_file(
    session_id: str,
    data: bytes,
    filename: str,
    kind: str,
    mime: str,
    att_id: str | None = None,
) -> str:
    """把附件字节落盘，返回存储路径。"""
    att_id = att_id or uuid.uuid4().hex
    base = Path(attachments_dir()) / session_id
    base.mkdir(parents=True, exist_ok=True)
    # 文件名约定：<att_id>__<safe_stem>，避免重名覆盖
    storage_path = base / f"{att_id}__{_safe_stem(filename)}"
    storage_path.write_bytes(data)
    return str(storage_path)


def _resize_image(data: bytes, mime: str) -> bytes:
    """用 Pillow 将图像缩放到最大边长以内（仅对常见格式生效）。"""
    try:
        from io import BytesIO

        from PIL import Image

        img: Any = Image.open(BytesIO(data))
        img = img.convert("RGB") if kind_needs_rgb(img) else img
        if max(img.width, img.height) <= MAX_IMAGE_DIMENSION:
            return data
        ratio = MAX_IMAGE_DIMENSION / max(img.width, img.height)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
        out = BytesIO()
        out_format = "PNG" if mime == "image/png" else "JPEG"
        img.save(out, format=out_format, quality=85)
        return out.getvalue()
    except Exception:
        # 缩放失败则原样返回，不阻断主流程
        return data


def kind_needs_rgb(img: Any) -> bool:  # noqa: ANN401
    """PNG 保留透明通道（不强制 RGB）；其余统一转 RGB 以便 JPEG 压缩。"""
    try:
        return img.mode not in ("RGBA", "LA", "P")
    except Exception:
        return True


def render_content_parts(
    text: str,
    attachment_metas: list[dict[str, Any]],
    attachment_repo: Any,
) -> Any:
    """把用户文本 + 附件渲染为发送给模型的 content。

    返回：
    - 无附件时返回原文本字符串（向后兼容）。
    - 有附件时返回 content 列表（文本块在前，随后是文档/图像块）。
    """
    cleaned = (text or "").strip()
    parts: list[dict[str, Any]] = []
    if cleaned:
        parts.append({"type": "text", "text": cleaned})

    for meta in attachment_metas:
        att = attachment_repo.get(meta["id"]) if attachment_repo else None
        if not att:
            continue
        try:
            raw = Path(att["storage_path"]).read_bytes()
        except Exception:
            continue
        if meta.get("kind") == "image":
            data = _resize_image(raw, att["mime"])
            b64 = base64.b64encode(data).decode("ascii")
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{att['mime']};base64,{b64}"},
                }
            )
        elif meta.get("kind") == "document":
            try:
                doc_text = raw.decode("utf-8", errors="replace")
            except Exception:
                doc_text = ""
            header = f"[文档 {meta.get('filename', 'unknown')}]\n"
            # 文档可能较长，按上限截断，避免单次消息 token 爆炸
            if len(doc_text) > MAX_DOCUMENT_SIZE * 4:
                doc_text = doc_text[: MAX_DOCUMENT_SIZE * 4] + "\n...[已截断]"
            parts.append({"type": "text", "text": header + doc_text})

    if not parts:
        return cleaned or ""
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    return parts
