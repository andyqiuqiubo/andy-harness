"""模板分享中心（Template Hub）—— 可复用资产的统一打包 / 分享 / 实例化（P3）。

资产类型（type）：
- ``session``：完整会话包（复用 session export 结构，消息正文支持 ``{{变量}}`` 占位）；
- ``prompt``：提示词模板（payload = {"messages": [...]}，实例化即新会话的首问）；
- ``workflow``：工作流模板（实例化写入 workflows 表，交由工作流模块执行）。

分享包格式（version 1）与 REST payload 对齐，第三方可直接生成导入。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from harness.infra.database import Database
from harness.kernel.exceptions import ServiceUnavailable

logger = logging.getLogger("harness.templates")

ALLOWED_TYPES = ("session", "prompt", "workflow")
_VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_\u4e00-\u9fff]+)\s*\}\}")


def render_obj(obj: Any, variables: dict[str, Any]) -> Any:
    """递归把结构里的 ``{{变量}}`` 占位符替换为变量值（缺失的占位符原样保留）。"""
    if isinstance(obj, str):

        def _sub(m: re.Match[str]) -> str:
            key = m.group(1)
            val = variables.get(key, m.group(0))
            return str(val)

        return _VAR_RE.sub(_sub, obj)
    if isinstance(obj, list):
        return [render_obj(x, variables) for x in obj]
    if isinstance(obj, dict):
        return {k: render_obj(v, variables) for k, v in obj.items()}
    return obj


def extract_vars(payload: Any) -> list[str]:
    """从 payload 里提取全部占位符变量名（去重保序）。"""
    found: list[str] = []

    def _walk(o: Any) -> None:
        if isinstance(o, str):
            for m in _VAR_RE.finditer(o):
                if m.group(1) not in found:
                    found.append(m.group(1))
        elif isinstance(o, list):
            for x in o:
                _walk(x)
        elif isinstance(o, dict):
            for v in o.values():
                _walk(v)

    _walk(payload)
    return found


class TemplateHubService:
    """模板中心服务。"""

    def __init__(self, db: Database, session_service: Any = None) -> None:
        self._db = db
        self._session_service = session_service

    # ── CRUD ────────────────────────────────────────
    def list_templates(self, type_filter: str | None = None) -> list[dict[str, Any]]:
        if type_filter:
            rows = self._db.query(
                "SELECT id, type, name, description, vars_schema_json, payload_json, source_session_id, created_at "
                "FROM templates WHERE type = ? ORDER BY created_at DESC",
                (type_filter,),
            )
        else:
            rows = self._db.query(
                "SELECT id, type, name, description, vars_schema_json, payload_json, source_session_id, created_at "
                "FROM templates ORDER BY created_at DESC"
            )
        out = []
        for r in rows:
            d = dict(r)
            schema = json.loads(d.pop("vars_schema_json") or "{}")
            payload = json.loads(d.pop("payload_json") or "{}")
            d["vars"] = sorted(set(list((schema.get("properties") or {}).keys()) + extract_vars(payload)))
            out.append(d)
        return out

    def create_template(
        self,
        type: str,
        name: str,
        description: str = "",
        vars_schema: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        source_session_id: str | None = None,
    ) -> dict[str, Any]:
        if type not in ALLOWED_TYPES:
            raise ValueError(f"不支持的模板类型: {type}，可选 {list(ALLOWED_TYPES)}")
        if not name.strip():
            raise ValueError("模板名称不能为空")
        tpl_id = uuid.uuid4().hex[:12]
        now = datetime.now().isoformat(timespec="seconds")
        self._db.execute(
            "INSERT INTO templates (id, type, name, description, vars_schema_json, "
            "payload_json, source_session_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tpl_id,
                type,
                name.strip(),
                description,
                json.dumps(vars_schema or {}, ensure_ascii=False),
                json.dumps(payload or {}, ensure_ascii=False),
                source_session_id,
                now,
            ),
        )
        return self.get_template(tpl_id)  # type: ignore[return-value]

    def get_template(self, template_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT id, type, name, description, vars_schema_json, payload_json, source_session_id, created_at "
            "FROM templates WHERE id = ?",
            (template_id,),
        )
        if row is None:
            return None
        d = dict(row)
        d["vars_schema"] = json.loads(d.pop("vars_schema_json") or "{}")
        d["payload"] = json.loads(d.pop("payload_json") or "{}")
        d["vars"] = sorted(set(list((d["vars_schema"].get("properties") or {}).keys()) + extract_vars(d["payload"])))
        return d

    def delete_template(self, template_id: str) -> bool:
        cur = self._db.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        return cur.rowcount > 0

    # ── 从会话创建 ──────────────────────────────────
    def create_from_session(
        self, session_id: str, name: str, description: str = "", variables: list[str] | None = None
    ) -> dict[str, Any]:
        """导出会话并存为 session 模板（variables 里的词会被包装成 {{var}} 占位说明）。"""
        if self._session_service is None:
            raise RuntimeError("会话服务不可用")
        payload = self._session_service.export_session(session_id)
        if payload is None:
            raise ValueError(f"会话不存在: {session_id}")
        return self.create_template(
            "session",
            name,
            description,
            vars_schema={"type": "object", "properties": {v: {"type": "string"} for v in (variables or [])}},
            payload=payload,
            source_session_id=session_id,
        )

    # ── 实例化 ──────────────────────────────────────
    def instantiate(self, template_id: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """渲染占位符并落地为新资产，返回 {type, session_id?/workflow_id?, pending_message?}。

        会话/提示词模板：若渲染后最后一条消息是 user 提问，则**不落库**、作为
        ``pending_message`` 返回——前端跳转会话页后经正常发送链路（WS + AgentLoop）
        发出，用户才能看到流式回答；否则提问只存在于数据库，永远没有回答。
        """
        tpl = self.get_template(template_id)
        if tpl is None:
            raise KeyError(f"模板不存在: {template_id}")
        variables = variables or {}
        payload = render_obj(tpl["payload"], variables)

        if tpl["type"] in ("session", "prompt"):
            if self._session_service is None:
                # 用 ServiceUnavailable 而非 RuntimeError：前者有全局异常处理器，
                # 会返回 503 + 结构化错误码；后者会冒泡成 500，前端无从区分。
                raise ServiceUnavailable("session_manager")
            messages = list(payload.get("messages") or [])
            if tpl["type"] == "prompt" and not messages:
                raise ValueError("提示词模板缺少 messages")
            # 末尾的 user 提问不落库，交给前端走真实发送链路
            pending_message: str | None = None
            if messages and messages[-1].get("role") == "user":
                pending_message = str(messages.pop().get("content") or "")
            import_payload = {"session": {"title": tpl["name"]}, "messages": messages}
            session = self._session_service.import_session(import_payload, title=tpl["name"])
            return {
                "type": tpl["type"],
                "session_id": session.id,
                "pending_message": pending_message,
            }
        if tpl["type"] == "workflow":
            wf_id = uuid.uuid4().hex[:12]
            now = datetime.now().isoformat(timespec="seconds")
            # 工作流图结构存于 graph_json（与 workflows 服务一致，DDL 经迁移补列）；
            # payload 兼容 graph / steps 两种键名，缺省为空图。
            graph = payload.get("graph") or payload.get("steps") or {"nodes": [], "edges": []}
            self._db.execute(
                "INSERT INTO workflows (id, name, description, vars_json, graph_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    wf_id,
                    tpl["name"],
                    tpl.get("description") or "",
                    json.dumps(payload.get("vars") or [], ensure_ascii=False),
                    json.dumps(graph, ensure_ascii=False),
                    now,
                ),
            )
            return {"type": "workflow", "workflow_id": wf_id}
        raise ValueError(f"不支持的模板类型: {tpl['type']}")
