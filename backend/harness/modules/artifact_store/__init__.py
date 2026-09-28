"""artifact_store —— 工件存储（大工具输出落盘）。

对齐 Deep Agents 的虚拟文件系统、OpenAI Agents SDK 的 offload large
tool outputs：超过阈值的工具结果不进入上下文，而是落盘为工件文件，
仅回传路径 + 摘要，模型可按需用 `read_artifact` 工具回读全文。
"""

from .service import (
    ArtifactRecord,
    ArtifactStore,
    ArtifactStoreImpl,
    offload_threshold,
    summarize,
)

__all__ = [
    "ArtifactRecord",
    "ArtifactStore",
    "ArtifactStoreImpl",
    "offload_threshold",
    "summarize",
]
