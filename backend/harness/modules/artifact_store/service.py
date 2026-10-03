"""工件存储服务 —— 把超阈值的工具输出落盘，返回可回读的工件记录。

设计目标：
- **零新增依赖**：仅用标准库 + 项目既有 SQLite。
- **落盘位置可控**：默认 `<项目>/workspace/artifacts/`，可用环境变量
  `HARNESS_ARTIFACTS_DIR` 覆盖；阈值可用 `HARNESS_OFFLOAD_THRESHOLD` 覆盖。
- **可检索/可回读**：工件元数据写入 `artifacts` 表，正文落在文件里；
  `read_artifact` 工具支持 offset/limit 分页读取，避免再次撑爆上下文。
"""

from __future__ import annotations

import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.infra.database import Database

# 默认阈值（字符数）：约 1.5K~2K token，超过即落盘
DEFAULT_THRESHOLD = 6000

# 默认保留上限（条数）：超过则清理最旧的工件，避免磁盘无限增长（0 = 不限制）
DEFAULT_ARTIFACTS_MAX = 500

# 默认落盘目录：<项目根>/workspace/artifacts
# 本文件位于 backend/harness/modules/artifact_store/service.py
DEFAULT_ARTIFACTS_DIR = Path(__file__).resolve().parents[3] / "workspace" / "artifacts"


def artifacts_dir() -> Path:
    """工件落盘目录（env 优先）。"""
    override = os.environ.get("HARNESS_ARTIFACTS_DIR")
    if override:
        return Path(override)
    return DEFAULT_ARTIFACTS_DIR


def offload_threshold() -> int:
    """落盘阈值（字符数）。"""
    raw = os.environ.get("HARNESS_OFFLOAD_THRESHOLD")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_THRESHOLD


def artifacts_max() -> int:
    """工件保留条数上限（0 = 不限制）。"""
    raw = os.environ.get("HARNESS_ARTIFACTS_MAX")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_ARTIFACTS_MAX


def summarize(content: str, head: int = 240) -> str:
    """生成单行摘要：压缩空白并截取首部。"""
    text = " ".join(content.split())
    if len(text) <= head:
        return text
    return text[:head] + " …（已截断）"


def _now_iso() -> str:
    """UTC 时间戳（带显式偏移），前端据此转本地时间展示。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class ArtifactRecord:
    """一个落盘工件的元数据。"""

    id: str
    session_id: str
    tool_name: str
    path: str
    size_bytes: int
    char_count: int
    summary: str
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> ArtifactRecord:
        return cls(
            id=row["id"],
            session_id=row["session_id"],
            tool_name=row["tool_name"],
            path=row["path"],
            size_bytes=row["size_bytes"],
            char_count=row["char_count"],
            summary=row["summary"],
            created_at=row["created_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "char_count": self.char_count,
            "summary": self.summary,
            "created_at": self.created_at,
        }


class ArtifactStore(ABC):
    """工件存储接口。"""

    @abstractmethod
    def should_offload(self, content: str) -> bool:
        """内容是否达到落盘阈值。"""

    @abstractmethod
    def offload(self, session_id: str, tool_name: str, content: str) -> ArtifactRecord:
        """落盘内容并返回工件记录。"""

    @abstractmethod
    def get(self, artifact_id: str) -> ArtifactRecord | None:
        """按 id 获取工件元数据。"""

    @abstractmethod
    def read(self, artifact_id: str, offset: int = 0, limit: int | None = None) -> str | None:
        """读取工件正文（支持 offset/limit 分页）；不存在返回 None。"""

    @abstractmethod
    def list_artifacts(
        self,
        session_id: str | None = None,
        limit: int = 100,
        user_id: str | None = None,
    ) -> list[ArtifactRecord]:
        """列出工件（可按会话过滤，按时间倒序）。

        user_id 非空时做两跳归属过滤（按 session 的 user_id 收敛），
        仅多用户部署（启用认证）下生效，单用户部署传空串不过滤。
        """

    @abstractmethod
    def delete(self, artifact_id: str) -> bool:
        """删除工件（文件 + 元数据）。"""


class ArtifactStoreImpl(ArtifactStore):
    """基于 SQLite 元数据 + 本地文件的工件存储实现。"""

    def __init__(self, db: Database, base_dir: str | Path | None = None) -> None:
        self._db = db
        self._base_dir = Path(base_dir) if base_dir else artifacts_dir()
        self._base_dir.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> str:
        return str(self._base_dir)

    def should_offload(self, content: str) -> bool:
        threshold = offload_threshold()
        return threshold > 0 and len(content) > threshold

    def offload(self, session_id: str, tool_name: str, content: str) -> ArtifactRecord:
        aid = f"art_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
        path = self._base_dir / f"{aid}.txt"
        path.write_text(content, encoding="utf-8")

        record = ArtifactRecord(
            id=aid,
            session_id=session_id or "",
            tool_name=tool_name or "",
            path=str(path),
            size_bytes=len(content.encode("utf-8")),
            char_count=len(content),
            summary=summarize(content),
            created_at=_now_iso(),
        )
        self._db.execute(
            "INSERT INTO artifacts "
            "(id, session_id, tool_name, path, size_bytes, char_count, summary, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.session_id,
                record.tool_name,
                record.path,
                record.size_bytes,
                record.char_count,
                record.summary,
                record.created_at,
            ),
        )
        self._prune()
        return record

    def _prune(self) -> None:
        """按保留上限清理最旧的工件（文件 + 元数据），避免磁盘无限增长。

        以 rowid（插入顺序）为准，避免同一秒内创建的记录排序不稳定。
        """
        maxn = artifacts_max()
        if maxn <= 0:
            return
        rows = self._db.query(
            "SELECT id, path FROM artifacts WHERE rowid IN ("
            "SELECT rowid FROM artifacts ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
            (maxn,),
        )
        for row in rows:
            try:
                Path(row["path"]).unlink(missing_ok=True)
            except OSError:
                pass
            self._db.execute("DELETE FROM artifacts WHERE id = ?", (row["id"],))

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        row = self._db.query_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        return ArtifactRecord.from_row(row) if row is not None else None

    def read(self, artifact_id: str, offset: int = 0, limit: int | None = None) -> str | None:
        record = self.get(artifact_id)
        if record is None:
            return None
        path = Path(record.path)
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        offset = max(0, offset)
        if offset == 0 and limit is None:
            return text
        end = None if limit is None else offset + max(0, limit)
        return text[offset:end]

    def list_artifacts(
        self,
        session_id: str | None = None,
        limit: int = 100,
        user_id: str | None = None,
    ) -> list[ArtifactRecord]:
        if user_id:
            # E12：仅返回归属当前用户的会话下的工件（两跳：artifacts.session_id → sessions.user_id）。
            # 用 JOIN 把无主 / 他人会话下的工件排除（单用户模式 user_id 为 None，不触发）。
            rows = self._db.query(
                "SELECT a.* FROM artifacts a "
                "JOIN sessions s ON a.session_id = s.id "
                "WHERE s.user_id = ? "
                "ORDER BY a.created_at DESC, a.id DESC LIMIT ?",
                (user_id, limit),
            )
        elif session_id:
            rows = self._db.query(
                "SELECT * FROM artifacts WHERE session_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (session_id, limit),
            )
        else:
            rows = self._db.query(
                "SELECT * FROM artifacts ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            )
        return [ArtifactRecord.from_row(r) for r in rows]

    def delete(self, artifact_id: str) -> bool:
        record = self.get(artifact_id)
        if record is None:
            return False
        try:
            Path(record.path).unlink(missing_ok=True)
        except OSError:
            pass
        self._db.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
        return True
