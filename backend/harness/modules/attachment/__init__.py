"""附件模块：上传校验、落盘与多模态渲染。"""

from harness.modules.attachment.limits import (
    DOCUMENT_EXTENSIONS,
    IMAGE_EXTENSIONS,
    MAX_DOCUMENTS_PER_MESSAGE,
    MAX_FILES_PER_MESSAGE,
    MAX_IMAGES_PER_MESSAGE,
)
from harness.modules.attachment.service import (
    AttachmentError,
    classify,
    render_content_parts,
    store_file,
)

__all__ = [
    "AttachmentError",
    "DOCUMENT_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "MAX_DOCUMENTS_PER_MESSAGE",
    "MAX_FILES_PER_MESSAGE",
    "MAX_IMAGES_PER_MESSAGE",
    "classify",
    "render_content_parts",
    "store_file",
]
