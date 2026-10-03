"""Todo 服务 —— 会话级任务清单（规划与进度追踪）。

行业依据：
- Deep Agents：`write_todos` 规划工具
- Claude Code：TodoWrite + plan mode
- Microsoft Agent Framework：todo tracking / plan-execute
- OpenHands：task tracker

价值：长任务拆成可见的待办项，用户能看到进度，模型也不易跑偏。
采用**覆盖式写入**（每轮提交完整列表），与业界做法一致，语义简单幂等。
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("harness.todos")

# 状态常量
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"

VALID_STATUSES = (STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_COMPLETED)


@dataclass
class TodoItem:
    """待办项。"""

    id: str
    content: str
    status: str = STATUS_PENDING
    position: int = 0

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status,
            "position": self.position,
        }


class TodoService(ABC):
    """Todo 服务契约。"""

    @abstractmethod
    def list_todos(self, session_id: str) -> list[TodoItem]:
        """列出某会话的待办项。"""

    @abstractmethod
    def replace_todos(self, session_id: str, items: list[dict[str, Any]]) -> list[TodoItem]:
        """整体覆盖某会话的待办列表（返回保存后的列表）。"""

    @abstractmethod
    def clear_todos(self, session_id: str) -> int:
        """清空某会话的待办列表，返回删除条数。"""

    @abstractmethod
    def summary(self, session_id: str) -> dict[str, int]:
        """统计各状态数量。"""


class TodoServiceImpl(TodoService):
    """Todo 服务默认实现（SQLite 持久化）。"""

    def __init__(self, db: Any) -> None:
        self._db = db

    def list_todos(self, session_id: str) -> list[TodoItem]:
        """列出待办项（按 position 排序）。"""
        rows = self._db.query(
            "SELECT id, content, status, position FROM todos WHERE session_id = ? ORDER BY position, created_at",
            (session_id,),
        )
        return [
            TodoItem(
                id=str(row["id"]),
                content=str(row["content"]),
                status=str(row["status"]),
                position=int(row["position"]),
            )
            for row in rows
        ]

    def replace_todos(self, session_id: str, items: list[dict[str, Any]]) -> list[TodoItem]:
        """整体覆盖：先清空再按序写入。"""
        self._db.execute("DELETE FROM todos WHERE session_id = ?", (session_id,))

        saved: list[TodoItem] = []
        for idx, raw in enumerate(items):
            content = str(raw.get("content", "")).strip()
            if not content:
                continue
            status = str(raw.get("status", STATUS_PENDING)).strip()
            if status not in VALID_STATUSES:
                status = STATUS_PENDING
            item_id = str(raw.get("id", "")).strip() or str(uuid.uuid4())

            self._db.execute(
                "INSERT INTO todos (id, session_id, content, status, position) VALUES (?, ?, ?, ?, ?)",
                (item_id, session_id, content, status, idx),
            )
            saved.append(TodoItem(item_id, content, status, idx))

        logger.info("Todo 列表已更新: session=%s 共 %d 项", session_id, len(saved))
        return saved

    def clear_todos(self, session_id: str) -> int:
        """清空待办列表。"""
        rows = self._db.query("SELECT COUNT(*) AS c FROM todos WHERE session_id = ?", (session_id,))
        count = int(rows[0]["c"]) if rows else 0
        self._db.execute("DELETE FROM todos WHERE session_id = ?", (session_id,))
        return count

    def summary(self, session_id: str) -> dict[str, int]:
        """统计各状态数量。"""
        result: dict[str, int] = {
            STATUS_PENDING: 0,
            STATUS_IN_PROGRESS: 0,
            STATUS_COMPLETED: 0,
        }
        for item in self.list_todos(session_id):
            result[item.status] = result.get(item.status, 0) + 1
        return result
