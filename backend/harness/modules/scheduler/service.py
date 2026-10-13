"""scheduler —— 定时任务（计划任务）服务。

一个「定时任务」= 到点后在一个**新建的隔离会话**里自动跑一段提示词，
并且**只开放任务里勾选的工具**（内置工具 / 指定 MCP 服务器的工具 / 指定 Skill）。

调度策略：
- `daily`   每天固定时间（HH:MM）
- `weekly`  每周指定星期几 + 时间
- `interval` 每隔 N 分钟
- `once`    指定日期时间执行一次（跑完自动停用）
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from harness.infra.database import Database

logger = logging.getLogger("harness.scheduler")

DAILY = "daily"
WEEKLY = "weekly"
INTERVAL = "interval"
ONCE = "once"
VALID_TYPES = {DAILY, WEEKLY, INTERVAL, ONCE}

_WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _now_local() -> datetime:
    """本地当前时间（naive）。"""
    return datetime.now().astimezone().replace(tzinfo=None)


def _to_iso(dt: datetime | None) -> str:
    """naive 本地时间 → 带时区偏移的 ISO 字符串（前端可正确转本地）。"""
    if dt is None:
        return ""
    return dt.astimezone().isoformat(timespec="seconds")


def parse_hhmm(value: str) -> tuple[int, int]:
    """解析 "HH:MM"（非法时回落到 09:00）。"""
    try:
        parts = str(value).strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (ValueError, IndexError, TypeError):
        pass
    return 9, 0


@dataclass
class ScheduleSpec:
    """调度规则。"""

    type: str = DAILY
    time: str = "09:00"  # daily / weekly
    weekdays: list[int] = field(default_factory=list)  # weekly: 0=周一
    interval_minutes: int = 60  # interval
    run_at: str = ""  # once: "YYYY-MM-DD HH:MM"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> ScheduleSpec:
        raw = raw or {}
        stype = str(raw.get("type") or DAILY).strip().lower()
        if stype not in VALID_TYPES:
            stype = DAILY
        weekdays: list[int] = []
        for d in raw.get("weekdays") or []:
            try:
                n = int(d)
                if 0 <= n <= 6:
                    weekdays.append(n)
            except (TypeError, ValueError):
                continue
        try:
            interval = int(raw.get("interval_minutes") or 60)
        except (TypeError, ValueError):
            interval = 60
        return cls(
            type=stype,
            time=str(raw.get("time") or "09:00"),
            weekdays=weekdays,
            interval_minutes=max(1, interval),
            run_at=str(raw.get("run_at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "time": self.time,
            "weekdays": list(self.weekdays),
            "interval_minutes": self.interval_minutes,
            "run_at": self.run_at,
        }

    def describe(self) -> str:
        """人类可读的调度描述。"""
        hhmm = self.time
        if self.type == DAILY:
            return f"每天 {hhmm}"
        if self.type == WEEKLY:
            days = [_WEEKDAY_LABELS[d] for d in sorted(set(self.weekdays))]
            label = "、".join(days) if days else "（未选择星期）"
            return f"每{label} {hhmm}"
        if self.type == INTERVAL:
            m = self.interval_minutes
            if m % 60 == 0:
                return f"每隔 {m // 60} 小时"
            return f"每隔 {m} 分钟"
        if self.type == ONCE:
            return f"一次性：{self.run_at or '（未设置时间）'}"
        return self.type


def schedule_period_minutes(spec: ScheduleSpec) -> int | None:
    """调度周期（分钟）；一次性任务无周期返回 ``None``。

    SEC-33：供 missed-run 判定使用——停机后重启时，若某周期任务的应跑时刻
    已过去超过 N 个完整周期，则历史欠账不再补跑（补跑会形成重启风暴）。
    """
    if spec.type == INTERVAL:
        return max(1, spec.interval_minutes)
    if spec.type == DAILY:
        return 24 * 60
    if spec.type == WEEKLY:
        return 7 * 24 * 60
    return None  # once：一次性任务无周期概念，照常补跑


def compute_next_run(spec: ScheduleSpec, now: datetime | None = None) -> str:
    """计算下一次运行时间（返回带偏移的 ISO 字符串；无下次则空串）。"""
    now = now or _now_local()

    if spec.type == INTERVAL:
        return _to_iso(now + timedelta(minutes=spec.interval_minutes))

    if spec.type == ONCE:
        raw = (spec.run_at or "").strip()
        if not raw:
            return ""
        target = _parse_datetime(raw)
        if target is None or target <= now:
            return ""
        return _to_iso(target)

    hour, minute = parse_hhmm(spec.time)
    base = now.replace(hour=hour, minute=0, second=0, microsecond=0)

    if spec.type == DAILY:
        candidate = base.replace(minute=minute)
        if candidate <= now:
            candidate += timedelta(days=1)
        return _to_iso(candidate)

    # weekly
    weekdays = sorted(set(spec.weekdays))
    if not weekdays:
        weekdays = [now.weekday()]
    for delta in range(0, 8):
        day = (now + timedelta(days=delta)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day.weekday() in weekdays and day > now:
            return _to_iso(day)
    return ""


def _parse_datetime(raw: str) -> datetime | None:
    """宽松解析 "YYYY-MM-DD HH:MM" / ISO 字符串（naive 本地）。"""
    text = raw.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


@dataclass
class ScheduledTask:
    """一个定时任务。"""

    id: str
    name: str
    description: str = ""
    enabled: bool = True
    schedule: ScheduleSpec = field(default_factory=ScheduleSpec)
    prompt: str = ""
    mcp_servers: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    provider_id: str = ""
    model: str = ""
    max_iterations: int = 8
    timeout_seconds: int = 300
    next_run_at: str = ""
    last_run_at: str = ""
    last_status: str = ""
    last_error: str = ""
    run_count: int = 0
    fail_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    # SEC-31：归属用户（认证启用后按用户隔离；空串表示无归属/旧数据）
    user_id: str = ""

    @classmethod
    def from_row(cls, row: Any) -> ScheduledTask:
        def _load(raw: Any) -> list[Any]:
            try:
                value = json.loads(raw or "[]")
                return value if isinstance(value, list) else []
            except (json.JSONDecodeError, TypeError):
                return []

        return cls(
            id=row["id"],
            name=row["name"],
            description=row["description"] or "",
            enabled=bool(row["enabled"]),
            schedule=ScheduleSpec.from_dict(_safe_json(row["schedule_json"])),
            prompt=row["prompt"] or "",
            mcp_servers=[str(x) for x in _load(row["mcp_servers_json"])],
            skills=[str(x) for x in _load(row["skills_json"])],
            tools=[str(x) for x in _load(row["tools_json"])],
            provider_id=row["provider_id"] or "",
            model=row["model"] or "",
            max_iterations=int(row["max_iterations"] or 8),
            timeout_seconds=int(row["timeout_seconds"] or 300),
            next_run_at=row["next_run_at"] or "",
            last_run_at=row["last_run_at"] or "",
            last_status=row["last_status"] or "",
            last_error=row["last_error"] or "",
            run_count=int(row["run_count"] or 0),
            fail_count=int(row["fail_count"] or 0),
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            # 旧库（迁移前）没有该列，用 _row_get 容错读取
            user_id=_row_get(row, "user_id", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "schedule": self.schedule.to_dict(),
            "schedule_type": self.schedule.type,
            "schedule_desc": self.schedule.describe(),
            "prompt": self.prompt,
            "mcp_servers": list(self.mcp_servers),
            "skills": list(self.skills),
            "tools": list(self.tools),
            "provider_id": self.provider_id,
            "model": self.model,
            "max_iterations": self.max_iterations,
            "timeout_seconds": self.timeout_seconds,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "run_count": self.run_count,
            "fail_count": self.fail_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "user_id": self.user_id,
        }


def _row_get(row: Any, key: str, default: str = "") -> str:
    """从 sqlite3.Row 读取列，列不存在（旧库未迁移）时回落到默认值。"""
    try:
        value = row[key]
    except (IndexError, KeyError, TypeError):
        return default
    return str(value or default)


def _safe_json(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


@dataclass
class TaskRun:
    """一次运行记录。"""

    id: str
    task_id: str
    started_at: str = ""
    finished_at: str = ""
    status: str = "running"
    duration_ms: int = 0
    session_id: str = ""
    summary: str = ""
    error: str = ""
    trigger: str = "schedule"

    @classmethod
    def from_row(cls, row: Any) -> TaskRun:
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            started_at=row["started_at"] or "",
            finished_at=row["finished_at"] or "",
            status=row["status"] or "",
            duration_ms=int(row["duration_ms"] or 0),
            session_id=row["session_id"] or "",
            summary=row["summary"] or "",
            error=row["error"] or "",
            trigger=row["trigger"] or "schedule",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "session_id": self.session_id,
            "summary": self.summary,
            "error": self.error,
            "trigger": self.trigger,
        }


class SchedulerService:
    """定时任务的存储与调度计算（运行交给 TaskRunner）。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ── 查询 ──────────────────────────────────────────

    def list_tasks(self, user_id: str | None = None) -> list[ScheduledTask]:
        """列出任务。

        ``user_id`` 非空时只返回该用户的任务（SEC-31：管理员传空串看全部）。
        """
        if user_id:
            rows = self._db.query(
                "SELECT * FROM scheduled_tasks WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            )
        else:
            rows = self._db.query("SELECT * FROM scheduled_tasks ORDER BY created_at DESC")
        return [ScheduledTask.from_row(r) for r in rows]

    def get_task(self, task_id: str, user_id: str | None = None) -> ScheduledTask | None:
        """读取任务；``user_id`` 非空时校验归属，非本人任务视为不存在（fail-closed）。"""
        row = self._db.query_one("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
        if row is None:
            return None
        task = ScheduledTask.from_row(row)
        if user_id and task.user_id != user_id:
            return None
        return task

    def due_tasks(self, now: datetime | None = None) -> list[ScheduledTask]:
        """已到期需要执行的任务。"""
        now = now or _now_local()
        stamp = _to_iso(now)
        rows = self._db.query(
            "SELECT * FROM scheduled_tasks WHERE enabled = 1 "
            "AND next_run_at IS NOT NULL AND next_run_at != '' AND next_run_at <= ? "
            "ORDER BY next_run_at ASC",
            (stamp,),
        )
        return [ScheduledTask.from_row(r) for r in rows]

    # SEC-33：missed-run 宽限倍数。停机/长离线后重启时，周期任务的应跑时刻
    # 可能已过去成百上千个周期；若把历史欠账全部补跑会形成「重启风暴」——
    # 调度循环连续执行几百次任务，白烧 provider token、堆出大量重复会话。
    # 迟到超过「周期 × 该倍数」的任务直接跳过补跑（记录 skipped 并推进到未来）。
    MISSED_RUN_GRACE_FACTOR = 3

    def is_missed_too_long(self, task: ScheduledTask, now: datetime | None = None) -> bool:
        """SEC-33：任务的应跑时刻是否已错过太久（超过宽限倍数的周期）。

        - 周期任务（daily/weekly/interval）：``now - next_run_at > 周期 × 3`` 判定为
          错过太久，跳过补跑；
        - 一次性（once）任务：无周期概念，长时间欠账也照常补跑（用户明确指定了
          这个时刻，跳过反而是静默丢任务）；
        - ``next_run_at`` 缺失/不可解析：按未错过处理（交给正常流程）。
        """
        if not task.next_run_at:
            return False
        due = _parse_datetime(task.next_run_at)
        if due is None:
            return False
        period = schedule_period_minutes(task.schedule)
        if period is None:
            return False
        now = now or _now_local()
        grace = timedelta(minutes=period * self.MISSED_RUN_GRACE_FACTOR)
        return (now - due) > grace

    def missed_late_minutes(self, task: ScheduledTask, now: datetime | None = None) -> int:
        """SEC-33：应跑时刻已迟到多少分钟（供日志/记录；无法解析时返回 0）。"""
        if not task.next_run_at:
            return 0
        due = _parse_datetime(task.next_run_at)
        if due is None:
            return 0
        now = now or _now_local()
        return max(0, int((now - due).total_seconds() // 60))

    def list_runs(self, task_id: str, limit: int = 20) -> list[TaskRun]:
        rows = self._db.query(
            "SELECT * FROM scheduled_task_runs WHERE task_id = ? ORDER BY rowid DESC LIMIT ?",
            (task_id, max(1, limit)),
        )
        return [TaskRun.from_row(r) for r in rows]

    # ── 增删改 ────────────────────────────────────────

    @staticmethod
    def validate_schedule(spec: ScheduleSpec, *, enabled: bool = True) -> None:
        """校验调度规格，重点堵住「一次性任务永不执行」的静默失败。

        `once` 任务在未设置 `run_at` 或 `run_at` 已过期时，`compute_next_run`
        返回空串，而 `due_tasks` 要求 `next_run_at != ''`——结果是任务显示为
        已启用却永远不会执行，且没有任何提示。这里在创建/更新时直接拒绝，
        把问题暴露在配置阶段。
        """
        if spec.type != ONCE or not enabled:
            return
        raw = (spec.run_at or "").strip()
        if not raw:
            raise ValueError("一次性任务必须设置运行时间（run_at）")
        if _parse_datetime(raw) is None:
            raise ValueError(f"一次性任务的运行时间格式无效: {raw}（应为 YYYY-MM-DD HH:MM）")
        # 拒绝过去时间：一次性任务若设在过去，``compute_next_run`` 返回空串导致
        # ``due_tasks``（``next_run_at != ''``）永不选中——任务显示为已启用却永远
        # 不执行且无任何提示（回归测试 ``test_once_task_requires_future_run_at`` 守护点）。
        # SEC-33 的「once 不计入 missed-run 跳过」语义作用于运行时 next_run_at 被拨回
        # 过去的存量任务（见 test_once_task_never_skipped 的 DB 覆盖模拟），而非创建入口。
        if _parse_datetime(raw) <= _now_local():
            raise ValueError(f"一次性任务的运行时间必须晚于当前时间: {raw}")

    def create_task(
        self, data: dict[str, Any], user_id: str | None = None
    ) -> ScheduledTask:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        schedule = ScheduleSpec.from_dict(data.get("schedule"))
        enabled = bool(data.get("enabled", True))
        self.validate_schedule(schedule, enabled=enabled)
        now = _to_iso(_now_local())
        next_run = compute_next_run(schedule) if enabled else ""
        # user_id 优先级：显式参数 > data 内嵌；空值（含 ''）记为 ''
        uid = user_id if user_id is not None else data.get("user_id")
        self._db.execute(
            "INSERT INTO scheduled_tasks "
            "(id, name, description, enabled, schedule_type, schedule_json, prompt, "
            " mcp_servers_json, skills_json, tools_json, provider_id, model, "
            " max_iterations, timeout_seconds, next_run_at, created_at, updated_at, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                str(data.get("name") or "未命名任务"),
                str(data.get("description") or ""),
                1 if data.get("enabled", True) else 0,
                schedule.type,
                json.dumps(schedule.to_dict(), ensure_ascii=False),
                str(data.get("prompt") or ""),
                json.dumps(list(data.get("mcp_servers") or []), ensure_ascii=False),
                json.dumps(list(data.get("skills") or []), ensure_ascii=False),
                json.dumps(list(data.get("tools") or []), ensure_ascii=False),
                str(data.get("provider_id") or ""),
                str(data.get("model") or ""),
                int(data.get("max_iterations") or 8),
                int(data.get("timeout_seconds") or 300),
                next_run,
                now,
                now,
                str(uid or ""),
            ),
        )
        created = self.get_task(task_id)
        assert created is not None
        return created

    def update_task(self, task_id: str, data: dict[str, Any], user_id: str | None = None) -> ScheduledTask | None:
        task = self.get_task(task_id, user_id=user_id)
        if task is None:
            return None

        enabled = bool(data.get("enabled", task.enabled))
        schedule = ScheduleSpec.from_dict(data.get("schedule")) if "schedule" in data else task.schedule
        if "schedule" in data or "enabled" in data:
            self.validate_schedule(schedule, enabled=enabled)

        sets: list[str] = []
        params: list[Any] = []

        def _put(column: str, value: Any) -> None:
            sets.append(f"{column} = ?")
            params.append(value)

        if "name" in data:
            _put("name", str(data.get("name") or task.name))
        if "description" in data:
            _put("description", str(data.get("description") or ""))
        if "prompt" in data:
            _put("prompt", str(data.get("prompt") or ""))
        if "mcp_servers" in data:
            _put("mcp_servers_json", json.dumps(list(data.get("mcp_servers") or []), ensure_ascii=False))
        if "skills" in data:
            _put("skills_json", json.dumps(list(data.get("skills") or []), ensure_ascii=False))
        if "tools" in data:
            _put("tools_json", json.dumps(list(data.get("tools") or []), ensure_ascii=False))
        if "provider_id" in data:
            _put("provider_id", str(data.get("provider_id") or ""))
        if "model" in data:
            _put("model", str(data.get("model") or ""))
        if "max_iterations" in data:
            _put("max_iterations", int(data.get("max_iterations") or 8))
        if "timeout_seconds" in data:
            _put("timeout_seconds", int(data.get("timeout_seconds") or 300))
        if "schedule" in data:
            _put("schedule_type", schedule.type)
            _put("schedule_json", json.dumps(schedule.to_dict(), ensure_ascii=False))
        if "enabled" in data:
            _put("enabled", 1 if enabled else 0)

        # 启停或改调度 → 重算下次运行时间
        if "enabled" in data or "schedule" in data:
            _put("next_run_at", compute_next_run(schedule) if enabled else "")

        _put("updated_at", _to_iso(_now_local()))
        params.append(task_id)
        self._db.execute(
            f"UPDATE scheduled_tasks SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )
        return self.get_task(task_id)

    def delete_task(self, task_id: str, user_id: str | None = None) -> bool:
        if self.get_task(task_id, user_id=user_id) is None:
            return False
        self._db.execute("DELETE FROM scheduled_task_runs WHERE task_id = ?", (task_id,))
        self._db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        return True

    def clear_runs(self, task_id: str, user_id: str | None = None) -> int | None:
        """清空运行历史；``user_id`` 非空时校验归属，越权返回 None（由路由转 404）。"""
        if self.get_task(task_id, user_id=user_id) is None:
            return None
        rows = self._db.query(
            "SELECT COUNT(*) AS n FROM scheduled_task_runs WHERE task_id = ?", (task_id,)
        )
        count = int(rows[0]["n"]) if rows else 0
        if count:
            self._db.execute("DELETE FROM scheduled_task_runs WHERE task_id = ?", (task_id,))
        return count

    # ── 运行记录 ──────────────────────────────────────

    def start_run(self, task_id: str, trigger: str = "schedule") -> str:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        self._db.execute(
            "INSERT INTO scheduled_task_runs (id, task_id, started_at, status, trigger) VALUES (?, ?, ?, 'running', ?)",
            (run_id, task_id, _to_iso(_now_local()), trigger),
        )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        duration_ms: int = 0,
        session_id: str = "",
        summary: str = "",
        error: str = "",
    ) -> None:
        self._db.execute(
            "UPDATE scheduled_task_runs SET finished_at = ?, status = ?, duration_ms = ?, "
            "session_id = ?, summary = ?, error = ? WHERE id = ?",
            (
                _to_iso(_now_local()),
                status,
                int(duration_ms),
                session_id,
                (summary or "")[:4000],
                (error or "")[:2000],
                run_id,
            ),
        )

    def record_task_result(
        self,
        task: ScheduledTask,
        *,
        status: str,
        error: str = "",
        advance_schedule: bool = True,
    ) -> None:
        """更新任务的运行统计与下次运行时间。"""
        next_run = ""
        enabled = task.enabled
        if status == "ok":
            fail_count = 0
        elif status == "skipped":
            # 跳过（如无可用 provider）属配置/环境问题，不是任务本身执行失败。
            # 计入 fail_count 会让失败率虚高，也会误伤基于此的告警与重试判断。
            fail_count = task.fail_count
        else:
            fail_count = task.fail_count + 1
        # once 任务跑完（无论成败）自动停用
        if task.schedule.type == ONCE:
            enabled = False
            next_run = ""
        elif advance_schedule and enabled:
            next_run = compute_next_run(task.schedule)

        self._db.execute(
            "UPDATE scheduled_tasks SET last_run_at = ?, last_status = ?, last_error = ?, "
            "run_count = run_count + 1, fail_count = ?, next_run_at = ?, enabled = ?, "
            "updated_at = ? WHERE id = ?",
            (
                _to_iso(_now_local()),
                status,
                (error or "")[:1000],
                fail_count,
                next_run,
                1 if enabled else 0,
                _to_iso(_now_local()),
                task.id,
            ),
        )

    def claim_task(self, task_id: str) -> None:
        """抢占任务：立刻把 next_run_at 推进到下一次，避免执行期间被重复触发。

        只动 next_run_at，不改运行统计（统计由 record_task_result 负责）。
        """
        task = self.get_task(task_id)
        if task is None:
            return
        self._db.execute(
            "UPDATE scheduled_tasks SET next_run_at = ?, updated_at = ? WHERE id = ?",
            (compute_next_run(task.schedule), _to_iso(_now_local()), task_id),
        )

    def refresh_next_runs(self) -> int:
        """补齐/修正所有启用任务的下次运行时间（启动时调用）。"""
        count = 0
        for task in self.list_tasks():
            if not task.enabled:
                continue
            if not task.next_run_at:
                self._db.execute(
                    "UPDATE scheduled_tasks SET next_run_at = ?, updated_at = ? WHERE id = ?",
                    (compute_next_run(task.schedule), _to_iso(_now_local()), task.id),
                )
                count += 1
        return count
