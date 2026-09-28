"""附件上传的允许类型与数量/大小上限。

约束目标：单条消息携带的附件整体可控，避免视觉/文档 token 成本失控，
同时覆盖常见的文档与图片格式。
"""

from __future__ import annotations

import os
from pathlib import Path

# 允许的文档后缀（文本可读，抽取为文本后内联进 prompt）
DOCUMENT_EXTENSIONS: set[str] = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml", ".log",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".htm", ".xml",
    ".ini", ".toml", ".cfg", ".tex", ".rst", ".sh", ".bat", ".ps1",
    ".env.example", ".gitignore",
}

# 允许的图像后缀
IMAGE_EXTENSIONS: set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
}

# 单条消息的上限
MAX_DOCUMENTS_PER_MESSAGE: int = 5
MAX_IMAGES_PER_MESSAGE: int = 4
MAX_FILES_PER_MESSAGE: int = 8

# 单文件大小上限（字节）
MAX_DOCUMENT_SIZE: int = 200 * 1024          # 200 KB
MAX_IMAGE_SIZE: int = int(1.5 * 1024 * 1024)  # 1.5 MB

# 图像最大边长：超过则用 Pillow 缩放，控制视觉 token 成本
MAX_IMAGE_DIMENSION: int = 1280

# 附件落盘根目录（可用环境变量覆盖；测试隔离时指向临时目录）
DEFAULT_ATTACHMENTS_DIR: str = str(
    Path(__file__).parent.parent.parent.parent / "data" / "attachments"
)


def attachments_dir() -> str:
    """返回附件根目录（确保存在）。"""
    base = os.environ.get("HARNESS_ATTACHMENTS_DIR") or DEFAULT_ATTACHMENTS_DIR
    Path(base).mkdir(parents=True, exist_ok=True)
    return base


def ext_of(filename: str) -> str:
    """返回小写的后缀（含点）。"""
    return os.path.splitext(filename)[1].lower()
