"""定时任务测试：调度计算 / 服务 CRUD / 受限工具集 / 执行器 / REST。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest

from harness.engine.tool_registry import ToolRegistry
from harness.infra.database import Database
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry
from harness.modules.context_manager.service import ContextService, ContextServiceImpl
from harness.modules.scheduler.runner import ScopedUseSkillTool, TaskRunner
from harness.modules.scheduler.service import (
    DAILY,
    INTERVAL,
    ONCE,
    WEEKLY,
    SchedulerService,
    ScheduleSpec,
    compute_next_run,
)
from harness.modules.session_manager.service import SessionService, SessionServiceImpl


@pytest.fixture
def db() -> Database:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    database.close()
    os.unlink(path)


@pytest.fixture
def service(db: Database) -> SchedulerService:
    return SchedulerService(db)


class TestScheduleMath:
    """下次运行时间计算。"""

    def test_daily_today_or_tomorrow(self) -> None:
        now = datetime(2026, 9, 28, 8, 0, 0)  # 周一 08:00
        assert compute_next_run(ScheduleSpec(type=DAILY, time="09:00"), now).startswith("2026-09-28T09:00")
        # 已过点 → 次日
        later = datetime(2026, 9, 28, 10, 0, 0)
        assert compute_next_run(ScheduleSpec(type=DAILY, time="09:00"), later).startswith("2026-09-29T09:00")

    def test_weekly_picks_next_matching_weekday(self) -> None:
        monday = datetime(2026, 9, 28, 12, 0, 0)
        assert monday.weekday() == 0
        spec = ScheduleSpec(type=WEEKLY, time="09:00", weekdays=[2])  # 周三
        assert compute_next_run(spec, monday).startswith("2026-09-30T09:00")

    def test_interval(self) -> None:
        now = datetime(2026, 9, 28, 8, 0, 0)
        spec = ScheduleSpec(type=INTERVAL, interval_minutes=90)
        assert compute_next_run(spec, now).startswith("2026-09-28T09:30")

    def test_once_past_returns_empty(self) -> None:
        now = datetime(2026, 9, 28, 8, 0, 0)
        future = ScheduleSpec(type=ONCE, run_at="2026-09-29 07:00")
        past = ScheduleSpec(type=ONCE, run_at="2026-09-27 07:00")
        assert compute_next_run(future, now).startswith("2026-09-29T07:00")
        assert compute_next_run(past, now) == ""
        assert compute_next_run(ScheduleSpec(type=ONCE, run_at=""), now) == ""

    def test_describe(self) -> None:
        assert ScheduleSpec(type=DAILY, time="09:00").describe() == "每天 09:00"
        assert "周三" in ScheduleSpec(type=WEEKLY, time="09:00", weekdays=[2]).describe()
        assert ScheduleSpec(type=INTERVAL, interval_minutes=120).describe() == "每隔 2 小时"
        assert ScheduleSpec(type=INTERVAL, interval_minutes=45).describe() == "每隔 45 分钟"

    def test_invalid_type_falls_back_to_daily(self) -> None:
        assert ScheduleSpec.from_dict({"type": "每小时"}).type == DAILY


class TestSchedulerService:
    """任务存储与到期判断。"""

    def test_crud_and_next_run(self, service: SchedulerService) -> None:
        task = service.create_task(
            {
                "name": "每日新闻",
                "prompt": "搜集今天的大模型新闻",
                "schedule": {"type": DAILY, "time": "23:59"},
                "mcp_servers": ["youth-mcp"],
                "skills": ["daily-llm-news"],
                "tools": ["web_search"],
            }
        )
        assert task.id.startswith("task_")
        assert task.next_run_at  # 启用的任务创建时即计算下次运行
        assert service.get_task(task.id) is not None

        updated = service.update_task(task.id, {"name": "改名", "enabled": False})
        assert updated is not None and updated.name == "改名"
        assert updated.next_run_at == ""  # 停用后清空

        assert service.delete_task(task.id) is True
        assert service.delete_task(task.id) is False

    def test_due_tasks_and_claim(self, service: SchedulerService, db: Database) -> None:
        task = service.create_task({"name": "t", "prompt": "p", "schedule": {"type": DAILY, "time": "23:59"}})
        # 手动把 next_run_at 设为过去 → 应到期
        db.execute(
            "UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+08:00", task.id),
        )
        due = service.due_tasks()
        assert task.id in {t.id for t in due}

        # 抢占后 next_run_at 被推进 → 不再到期
        service.claim_task(task.id)
        assert task.id not in {t.id for t in service.due_tasks()}

    def test_record_result_and_once_autodisable(self, service: SchedulerService) -> None:
        task = service.create_task(
            {
                "name": "一次性",
                "prompt": "p",
                "schedule": {"type": ONCE, "run_at": "2030-01-01 09:00"},
            }
        )
        service.record_task_result(task, status="ok")
        after = service.get_task(task.id)
        assert after is not None
        assert after.run_count == 1
        assert after.last_status == "ok"
        assert after.enabled is False  # 一次性任务跑完自动停用

    def test_runs_history(self, service: SchedulerService) -> None:
        task = service.create_task({"name": "t", "prompt": "p", "schedule": {"type": INTERVAL, "interval_minutes": 30}})
        run_id = service.start_run(task.id, trigger="manual")
        service.finish_run(run_id, status="ok", duration_ms=120, summary="done")
        runs = service.list_runs(task.id)
        assert len(runs) == 1
        assert runs[0].status == "ok"
        assert runs[0].trigger == "manual"
        assert runs[0].summary == "done"
        assert service.clear_runs(task.id) == 1
        assert service.delete_task(task.id) is True
        assert service.list_runs(task.id) == []


class _EchoTool:
    """极简工具替身。"""

    def __init__(self, name: str, risk: str = "read") -> None:
        self._name = name
        self._risk = risk

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"tool {self._name}"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    @property
    def risk_level(self) -> str:
        return self._risk

    async def execute(self, args: dict[str, Any]) -> str:
        return f"{self._name} ok"


class _FakeUseSkill:
    tool_name = "use_skill"
    description = "use skill"
    parameters_schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    risk_level = "read"
    needs_session = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, args: dict[str, Any]) -> str:
        name = str(args.get("name") or "")
        self.calls.append(name)
        return f"loaded {name}"


class _FakeProvider:
    def __init__(self, reply: str = "任务完成") -> None:
        self.reply = reply
        self.calls = 0

    async def chat(
        self, messages: list[dict[str, str]], model: str, stream: bool = True, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls += 1
        yield {"delta": self.reply}


class _FakeProviderRegistry:
    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def list_providers(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "deepseek",
                "enabled": True,
                "has_api_key": True,
                "models": ["fake-model"],
            }
        ]

    def get_provider(self, provider_id: str) -> Any:
        return self._provider


def _build_env(db: Database, provider: Any = None, with_provider: bool = True):
    from harness.modules.model_manager.provider_registry import ProviderRegistry

    services = ServiceRegistry()
    services.register(Database, db, owner="test")
    services.register(HookManager, HookManager(), owner="test")
    session_service = SessionServiceImpl(db)
    services.register(SessionService, session_service, owner="test")
    services.register(ContextService, ContextServiceImpl(services=services), owner="test")
    services.register(SchedulerService, SchedulerService(db), owner="test")

    tool_registry = ToolRegistry()
    tool_registry.register(_EchoTool("calculator"), owner="test")
    tool_registry.register(_EchoTool("web_search"), owner="test")
    tool_registry.register(_EchoTool("code_runner", risk="dangerous"), owner="test")
    tool_registry.register(_EchoTool("mcp__youth-mcp__query_ads"), owner="test")
    tool_registry.register(_EchoTool("mcp__other__do_thing"), owner="test")
    tool_registry.register(_FakeUseSkill(), owner="test")
    services.register(ToolRegistry, tool_registry, owner="kernel")
    if with_provider:
        services.register(
            ProviderRegistry,
            _FakeProviderRegistry(provider or _FakeProvider()),
            owner="test",
        )
    return services, session_service


class TestScopedRegistry:
    """受限工具集：只开放勾选的工具。"""

    def test_only_selected_tools_and_mcp_servers(self, db: Database) -> None:
        services, _ss = _build_env(db)
        runner = TaskRunner(services)
        task = {
            "tools": ["calculator"],
            "mcp_servers": ["youth-mcp"],
            "skills": ["s1"],
        }
        from harness.modules.scheduler.service import ScheduledTask

        t = ScheduledTask(
            id="x",
            name="n",
            tools=task["tools"],
            mcp_servers=task["mcp_servers"],
            skills=task["skills"],
        )
        registry, allowed = runner.build_registry(t)
        names = {info["name"] for info in registry.list_tools()}
        assert "calculator" in names
        assert "mcp__youth-mcp__query_ads" in names
        assert "use_skill" in names
        # 未勾选的一律不开放
        assert "web_search" not in names
        assert "code_runner" not in names
        assert "mcp__other__do_thing" not in names
        assert "calculator" in allowed and "code_runner" not in allowed

    def test_use_skill_scope_blocks_others(self, db: Database) -> None:
        services, _ss = _build_env(db)
        fake = _FakeUseSkill()
        scoped = ScopedUseSkillTool(fake, ["alpha"])
        import asyncio

        out_ok = asyncio.run(scoped.execute({"name": "alpha"}))
        out_deny = asyncio.run(scoped.execute({"name": "beta"}))
        assert "loaded alpha" in out_ok
        assert "只允许使用" in out_deny
        assert fake.calls == ["alpha"]  # 被拦下的不会真正调用底层

    def test_no_tools_selected_gives_empty_registry(self, db: Database) -> None:
        services, _ss = _build_env(db)
        runner = TaskRunner(services)
        from harness.modules.scheduler.service import ScheduledTask

        registry, allowed = runner.build_registry(ScheduledTask(id="x", name="n"))
        assert registry.list_tools() == []
        assert allowed == []


class TestTaskRunner:
    """执行器：跑通、记录结果、无 provider 时跳过。"""

    async def test_run_task_ok(self, db: Database) -> None:
        provider = _FakeProvider("这是任务结果")
        services, session_service = _build_env(db, provider=provider)
        runner = TaskRunner(services)
        from harness.modules.scheduler.service import ScheduledTask

        task = ScheduledTask(
            id="t1",
            name="测试任务",
            prompt="做点事",
            enabled=True,
            tools=["calculator"],
        )
        services.get(SchedulerService).create_task(
            {"name": "测试任务", "prompt": "做点事", "schedule": {"type": INTERVAL, "interval_minutes": 60}}
        )
        outcome = await runner.run_task(task, trigger="manual")
        assert outcome.status == "ok"
        assert outcome.summary == "这是任务结果"
        assert outcome.session_id
        # 新建了隔离会话
        assert session_service.get_session(outcome.session_id) is not None
        # 运行历史已记录
        runs = services.get(SchedulerService).list_runs(task.id)
        assert runs and runs[0].status == "ok"

    async def test_run_task_without_provider_is_skipped(self, db: Database) -> None:
        services, _ss = _build_env(db, with_provider=False)
        runner = TaskRunner(services)
        from harness.modules.scheduler.service import ScheduledTask

        task = ScheduledTask(id="t2", name="无 provider", prompt="p")
        services.get(SchedulerService).create_task(
            {"name": "无 provider", "prompt": "p", "schedule": {"type": INTERVAL, "interval_minutes": 60}}
        )
        outcome = await runner.run_task(task)
        assert outcome.status == "skipped"
        assert outcome.error


class TestScheduleAPI:
    """REST 接口。"""

    @pytest.fixture
    def client(self) -> Any:
        from fastapi.testclient import TestClient

        from harness.main import app

        with TestClient(app) as c:
            yield c

    def test_options(self, client: Any) -> None:
        r = client.get("/api/schedules/options")
        assert r.status_code == 200
        data = r.json()
        assert "tools" in data and "skills" in data and "mcp_servers" in data
        assert any(t["name"] == "calculator" for t in data["tools"])
        # use_skill / task 这类托管工具不出现在可选项里
        assert not any(t["name"] in ("use_skill", "task") for t in data["tools"])

    def test_crud_and_runs(self, client: Any) -> None:
        r = client.post(
            "/api/schedules",
            json={
                "name": "API 任务",
                "prompt": "写个摘要",
                "schedule": {"type": "daily", "time": "07:30"},
                "tools": ["calculator"],
            },
        )
        assert r.status_code == 200
        task = r.json()["task"]
        tid = task["id"]
        try:
            assert task["schedule_desc"] == "每天 07:30"
            assert task["next_run_at"]

            r = client.get("/api/schedules")
            assert any(t["id"] == tid for t in r.json()["tasks"])

            r = client.patch(f"/api/schedules/{tid}", json={"enabled": False})
            assert r.status_code == 200
            assert r.json()["task"]["enabled"] is False
            assert r.json()["task"]["next_run_at"] == ""

            r = client.get(f"/api/schedules/{tid}/runs")
            assert r.status_code == 200 and r.json()["runs"] == []
        finally:
            client.delete(f"/api/schedules/{tid}")

    def test_validation_and_404(self, client: Any) -> None:
        assert client.post("/api/schedules", json={"name": "", "prompt": "p"}).status_code == 400
        assert client.post("/api/schedules", json={"name": "n", "prompt": ""}).status_code == 400
        assert (
            client.post(
                "/api/schedules",
                json={"name": "n", "prompt": "p", "schedule": {"type": "每半小时"}},
            ).status_code
            == 400
        )
        assert client.patch("/api/schedules/nope", json={"name": "x"}).status_code == 404
        assert client.delete("/api/schedules/nope").status_code == 404
        assert client.get("/api/schedules/nope/runs").status_code == 404
        assert client.post("/api/schedules/nope/run").status_code == 404
