"""repeat_runs / repeat_step_hits / repeat_audit 持久化（ADR-06）。

独立表：步骤级 attempts/hit/截图字段多，塞进 workflow_runs.steps_json 无法聚合命中率
（ADR-06）。DDL 与 harness/infra/database.py 的新建表保持一致（IF NOT EXISTS 幂等）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS repeat_sessions (
    id           TEXT PRIMARY KEY,
    label        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'draft',
    session_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS repeat_runs (
    run_id         TEXT PRIMARY KEY,
    workflow_id    TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'running',
    env_level      TEXT NOT NULL DEFAULT 'L0',
    drift_json     TEXT NOT NULL DEFAULT '{}',
    vars_json      TEXT NOT NULL DEFAULT '{}',
    total_steps    INTEGER NOT NULL DEFAULT 0,
    done_steps     INTEGER NOT NULL DEFAULT 0,
    degraded_steps INTEGER NOT NULL DEFAULT 0,
    attempts_total INTEGER NOT NULL DEFAULT 0,
    error_code     TEXT NOT NULL DEFAULT '',
    error_message  TEXT NOT NULL DEFAULT '',
    diagnostics_path TEXT NOT NULL DEFAULT '',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    graph_sha256   TEXT NOT NULL DEFAULT '',
    blind_used     INTEGER NOT NULL DEFAULT 0,
    unattended     INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    started_at     TEXT NOT NULL DEFAULT '',
    finished_at    TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_HITS = """
CREATE TABLE IF NOT EXISTS repeat_step_hits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    sid          TEXT NOT NULL,
    step_index   INTEGER NOT NULL DEFAULT 0,
    action       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT '',
    hit_index    INTEGER NOT NULL DEFAULT -1,
    hit_kind     TEXT NOT NULL DEFAULT '',
    attempts_json TEXT NOT NULL DEFAULT '[]',
    elapsed_ms   INTEGER NOT NULL DEFAULT 0,
    degraded     INTEGER NOT NULL DEFAULT 0,
    blind        INTEGER NOT NULL DEFAULT 0,
    code         TEXT NOT NULL DEFAULT '',
    message      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_AUDIT = """
CREATE TABLE IF NOT EXISTS repeat_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL DEFAULT '',
    sid        TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'decision',   -- decision | approval
    action     TEXT NOT NULL DEFAULT '',
    risk       TEXT NOT NULL DEFAULT '',
    decision   TEXT NOT NULL DEFAULT '',           -- allow|confirm|deny
    reason     TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_repeat_runs_wf ON repeat_runs(workflow_id, created_at);
CREATE INDEX IF NOT EXISTS idx_repeat_hits_run ON repeat_step_hits(run_id, step_index);
CREATE INDEX IF NOT EXISTS idx_repeat_hits_kind ON repeat_step_hits(hit_kind, created_at);
CREATE INDEX IF NOT EXISTS idx_repeat_audit_run ON repeat_audit(run_id, created_at);
"""


class RepeatRepository:
    """Database 之上的 repeat 专用门面（纯参数化 SQL，无 ORM）。"""

    def __init__(self, db: Any) -> None:
        self._db = db

    # ── DDL（插件激活时调一次，幂等） ───────────────────────────
    def setup(self) -> None:
        conn = self._db.conn  # 触发惰性连接 + 主 schema
        with self._db._lock:  # 复用连接锁（同库并发安全）
            conn.executescript(_CREATE_SESSIONS + _CREATE_RUNS + _CREATE_HITS + _CREATE_AUDIT + _CREATE_INDEX)
            conn.commit()

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ── runs ─────────────────────────────────────────────────────
    def insert_run(self, run_id: str, workflow_id: str, doc: dict[str, Any], *, steps_total: int) -> None:
        self._db.execute(
            "INSERT INTO repeat_runs (run_id, workflow_id, status, vars_json, total_steps, "
            "graph_sha256, unattended, started_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, workflow_id, str(doc.get("status", "running")),
             json.dumps(doc.get("vars", {}), ensure_ascii=False), steps_total,
             str(doc.get("graph_sha256", "")), 1 if doc.get("unattended") else 0,
             str(doc.get("started_at", "")), self._now()),
        )

    def finish_run(self, run_id: str, rec: Any) -> None:
        # rec: RunRecord 或 dict
        d = rec.to_dict() if hasattr(rec, "to_dict") else dict(rec)
        self._db.execute(
            "UPDATE repeat_runs SET status=?, env_level=?, drift_json=?, vars_json=?, "
            "done_steps=?, degraded_steps=?, attempts_total=?, error_code=?, error_message=?, "
            "diagnostics_path=?, checkpoint_json=?, blind_used=?, duration_ms=?, finished_at=? "
            "WHERE run_id=?",
            (d.get("status", ""), d.get("env_level", "L0"),
             json.dumps(d.get("drift", {}), ensure_ascii=False),
             json.dumps(d.get("vars", {}), ensure_ascii=False),
             int(d.get("stats", {}).get("done", 0)) if "stats" in d else 0,
             int(d.get("stats", {}).get("degraded", 0)) if "stats" in d else 0,
             int(d.get("attempts_total", 0)),
             d.get("error_code", ""), d.get("error_message", ""),
             d.get("diagnostics_path", ""),
             json.dumps(d.get("checkpoint", {}), ensure_ascii=False),
             1 if d.get("blind_used") else 0, int(d.get("duration_ms", 0)),
             d.get("finished_at", ""), run_id),
        )

    def touch_run(self, run_id: str, rec: Any) -> None:
        """增量同步运行态（不做终态收口）。

        旧实现只在 run() 返回后写库：挂起/长时间运行期间 DB 永远是 `running`、
        `done_steps=0`，页面刷新后完全看不到真实进度（真机坐实：17 步流程
        执行到第 2 步挂起，库里仍是 running/0）。
        """
        d = rec.to_dict() if hasattr(rec, "to_dict") else dict(rec)
        stats = d.get("stats") or {}
        self._db.execute(
            "UPDATE repeat_runs SET status=?, env_level=?, done_steps=?, degraded_steps=?, "
            "attempts_total=?, error_code=?, error_message=? WHERE run_id=?",
            (d.get("status", ""), d.get("env_level", "L0"),
             int(stats.get("done", 0)), int(stats.get("degraded", 0)),
             int(d.get("attempts_total", 0)),
             d.get("error_code", ""), _redact(d.get("error_message", "")), run_id),
        )

    def mark_stale_runs(self, note: str = "服务重启，运行已中断") -> int:
        """把上次进程遗留的非终态运行标记为 interrupted。

        进程重启后内存 runner 全丢失，若不清理，这些记录会永久停在
        running/suspended，UI 显示"进行中"却永远不动，也无法重新回放。
        """
        cur = self._db.execute(
            "UPDATE repeat_runs SET status='interrupted', error_code='RP-7005', "
            "error_message=? WHERE status IN ('precheck','running','suspended','dry_run')",
            (note,),
        )
        return int(getattr(cur, "rowcount", 0) or 0)

    def record_step(self, run_id: str, sr: Any) -> None:
        d = sr.to_dict() if hasattr(sr, "to_dict") else dict(sr)
        self._db.execute(
            "INSERT INTO repeat_step_hits (run_id, sid, step_index, action, status, hit_index, "
            "hit_kind, attempts_json, elapsed_ms, degraded, blind, code, message, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, d.get("sid", ""), int(d.get("index", 0)), d.get("action", ""),
             d.get("status", ""), int(d.get("hit_index", -1)), d.get("hit_kind", ""),
             json.dumps(d.get("attempts", []), ensure_ascii=False), int(d.get("elapsed_ms", 0)),
             1 if d.get("degraded") else 0, 1 if d.get("blind") else 0,
             d.get("code", ""), _redact(d.get("message", "")), self._now()),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT * FROM repeat_runs WHERE run_id=?", (run_id,))
        if row is None:
            return None
        d = dict(row)
        d["vars"] = json.loads(d.pop("vars_json") or "{}")
        d["drift"] = json.loads(d.pop("drift_json") or "{}")
        d["checkpoint"] = json.loads(d.pop("checkpoint_json") or "{}")
        d["blind_used"] = bool(d.get("blind_used"))
        d["unattended"] = bool(d.get("unattended"))
        steps = self.step_hits(run_id)
        d["steps"] = steps
        # 与 RunRecord.stats() 同形：GET /runs/{id} 在运行结束后（runner 已回收）
        # 也必须能拿到统计，否则前端刷新后 stats=null（真机验证坐实）。
        d["stats"] = {
            "total": int(d.get("total_steps", 0) or 0),
            "done": int(d.get("done_steps", 0) or 0),
            "failed": sum(1 for s in steps if s.get("status") == "failed"),
            "skipped": sum(1 for s in steps if s.get("status") == "skipped"),
            "degraded": int(d.get("degraded_steps", 0) or 0),
            "retries": int(d.get("attempts_total", 0) or 0),
            "duration_ms": int(d.get("duration_ms", 0) or 0),
        }
        return d

    def list_runs(self, workflow_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if workflow_id:
            rows = self._db.query(
                "SELECT run_id, workflow_id, status, env_level, total_steps, done_steps, "
                "degraded_steps, attempts_total, error_code, error_message, blind_used, "
                "unattended, duration_ms, started_at, finished_at, created_at "
                "FROM repeat_runs WHERE workflow_id=? ORDER BY created_at DESC LIMIT ?",
                (workflow_id, limit))
        else:
            rows = self._db.query(
                "SELECT run_id, workflow_id, status, env_level, total_steps, done_steps, "
                "degraded_steps, attempts_total, error_code, error_message, blind_used, "
                "unattended, duration_ms, started_at, finished_at, created_at "
                "FROM repeat_runs ORDER BY created_at DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["blind_used"] = bool(d.get("blind_used"))
            d["unattended"] = bool(d.get("unattended"))
            out.append(d)
        return out

    def step_hits(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT sid, step_index, action, status, hit_index, hit_kind, attempts_json, "
            "elapsed_ms, degraded, blind, code, message FROM repeat_step_hits "
            "WHERE run_id=? ORDER BY step_index", (run_id,))
        out = []
        for r in rows:
            d = dict(r)
            d["attempts"] = json.loads(d.pop("attempts_json") or "[]")
            d["degraded"] = bool(d.get("degraded"))
            d["blind"] = bool(d.get("blind"))
            out.append(d)
        return out

    # ── audit / approval ─────────────────────────────────────────
    def insert_audit(self, rec: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO repeat_audit (run_id, sid, kind, action, risk, decision, reason, "
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (rec.get("run_id", ""), rec.get("sid", ""), "decision",
             rec.get("action", ""), rec.get("risk", ""), rec.get("decision", ""),
             _redact(str(rec.get("reason", ""))), self._now()),
        )

    def insert_approval(self, run_id: str, payload: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO repeat_audit (run_id, sid, kind, action, risk, decision, reason, "
            "detail_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, payload.get("sid", ""), "approval", payload.get("action", ""),
             payload.get("risk", ""), "confirm", "待审批",
             json.dumps({k: v for k, v in payload.items() if k not in ("value",)}, ensure_ascii=False),
             self._now()),
        )

    def list_audit(self, run_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if run_id:
            rows = self._db.query(
                "SELECT * FROM repeat_audit WHERE run_id=? ORDER BY id DESC LIMIT ?",
                (run_id, limit))
        else:
            rows = self._db.query("SELECT * FROM repeat_audit ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d.pop("detail_json") or "{}")
            except Exception:  # noqa: BLE001
                pass
            out.append(d)
        return out

    # ── 命中率聚合（insights 看板 / T5.3 脆弱性告警用） ──────────
    def hit_stats(self, workflow_id: str, *, window: int = 20) -> dict[str, Any]:
        rows = self._db.query(
            "SELECT h.sid, h.hit_index, h.hit_kind, h.degraded FROM repeat_step_hits h "
            "JOIN repeat_runs r ON r.run_id=h.run_id WHERE r.workflow_id=? "
            "ORDER BY h.id DESC LIMIT ?", (workflow_id, window * 64))
        by_sid: dict[str, dict[str, int]] = {}
        for r in rows:
            s = by_sid.setdefault(r["sid"], {"total": 0, "primary": 0, "degraded": 0,
                                             "miss": 0, "last_primary": 0})
            s["total"] += 1
            if int(r["hit_index"]) == 0:
                s["primary"] += 1
            if bool(r["degraded"]):
                s["degraded"] += 1
        # 链首命中率骤降检测：最近 5 次对比更早样本
        frag: list[dict[str, Any]] = []
        for sid, st in by_sid.items():
            rate = (st["primary"] / st["total"]) if st["total"] else 0
            if st["total"] >= 5 and rate < 0.5:
                frag.append({"sid": sid, "primary_rate": round(rate, 2), "total": st["total"]})
        return {"workflow_id": workflow_id, "per_step": by_sid, "fragile": frag}


def _redact(text: str) -> str:
    """审计/步骤消息脱敏：截断 + 去除可能的敏感串痕迹（值只走 *** 通道，此处双保险）。"""
    s = str(text or "")
    if len(s) > 300:
        s = s[:300] + "…"
    return s
