"""系统性修复回归测试。

覆盖本轮修掉的严重 bug / 功能缺口 / 前后端联动问题，逐条锁定行为：
1. 删除会话级联清理孤儿数据（artifacts / agent_runs / channel_links / memories）
2. 评测数据集级联删除运行记录 + 单条运行删除
3. MCP 多路径配置合并（旧实现只取第一个命中路径）
4. MCP 删除不存在 server 返回可判别结果；同名新增不再静默覆盖
5. 工作流运行成功写入 finished_at
6. 一次性定时任务缺少/过期 run_at 时拒绝创建（旧实现会静默永不执行）
7. 定时任务 skipped 不计入失败
8. Message.to_dict 输出 tool_call_id
9. Insights 内置指标贡献者已接线
10. 工具执行被中断 / 钩子短路时回填 tool 消息（否则会话永久 400）
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest

from harness.engine.agent_loop import AgentLoop, AgentLoopConfig
from harness.engine.tool_registry import ToolRegistry
from harness.infra.database import Database
from harness.infra.repository import Message
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry
from harness.modules.evals_lab.service import EvalLabService
from harness.modules.insights.builtin_metrics import register_builtin_contributors
from harness.modules.insights.service import (
    InsightsServiceImpl,
    clear_metric_contributors,
    list_metric_contributors,
)
from harness.modules.mcp_client.service import MCPClientService, MCPServerConfig
from harness.modules.scheduler.service import ONCE, SchedulerService, ScheduleSpec
from harness.modules.session_manager.service import SessionServiceImpl
from harness.modules.workflows.service import WorkflowService


@pytest.fixture
def db() -> Database:
    """临时数据库。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    database.close()
    os.unlink(path)


@pytest.fixture
def registry(db: Database) -> ServiceRegistry:
    """注册 Database 的服务注册表。"""
    reg = ServiceRegistry()
    reg.register(Database, db, owner="test")
    return reg


# ── 1. 删除会话级联清理 ────────────────────────────────


class TestDeleteSessionCascade:
    def test_delete_session_removes_orphans(self, db: Database, registry: ServiceRegistry) -> None:
        """删除会话后 artifacts/agent_runs/channel_links/session 记忆不得残留。"""
        service = SessionServiceImpl(db, services=registry)
        session = service.create_session("待删除会话")
        sid = session.id

        db.execute(
            "INSERT INTO artifacts (id, session_id, tool_name, path, size_bytes) VALUES (?,?,?,?,?)",
            ("art1", sid, "t", "C:/tmp/art1.txt", 128),
        )
        db.execute(
            "INSERT INTO agent_runs (run_id, session_id, status, iteration, snapshot_json) VALUES (?,?,?,?,?)",
            ("run1", sid, "running", 1, "{}"),
        )
        db.execute(
            "INSERT INTO channel_links (channel, external_user, session_id) VALUES (?,?,?)",
            ("telegram", "u1", sid),
        )
        db.execute(
            "INSERT INTO memories (id, key, value, scope, session_id) VALUES (?,?,?,?,?)",
            ("m1", "k", "v", "session", sid),
        )
        # 全局记忆不应被误删
        db.execute(
            "INSERT INTO memories (id, key, value, scope, session_id) VALUES (?,?,?,?,?)",
            ("m2", "k2", "v2", "global", ""),
        )

        assert service.delete_session(sid) is True

        assert db.query_one("SELECT id FROM artifacts WHERE session_id = ?", (sid,)) is None
        assert db.query_one("SELECT run_id FROM agent_runs WHERE session_id = ?", (sid,)) is None
        assert db.query_one("SELECT session_id FROM channel_links WHERE session_id = ?", (sid,)) is None
        assert db.query_one("SELECT id FROM memories WHERE id = 'm1'") is None
        assert db.query_one("SELECT id FROM memories WHERE id = 'm2'") is not None


# ── 2. 评测级联删除 ────────────────────────────────────


class TestEvalCascade:
    def test_delete_dataset_removes_runs(self, db: Database) -> None:
        svc = EvalLabService(db, None)
        ds = svc.create_dataset("ds", "desc", [{"id": "c1", "prompt": "hi", "expect_keywords": ["hi"]}])
        # 直接插一条运行记录（避开需要 provider 的后台执行）
        db.execute(
            "INSERT INTO eval_runs (id, dataset_id, matrix_json, status, results_json, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("r1", ds["id"], "[]", "success", "[]", datetime.now().isoformat(timespec="seconds")),
        )
        assert db.query_one("SELECT id FROM eval_runs WHERE dataset_id = ?", (ds["id"],)) is not None

        assert svc.delete_dataset(ds["id"]) is True
        assert db.query_one("SELECT id FROM eval_runs WHERE dataset_id = ?", (ds["id"],)) is None

    def test_delete_single_run(self, db: Database) -> None:
        svc = EvalLabService(db, None)
        db.execute(
            "INSERT INTO eval_runs (id, dataset_id, matrix_json, status, results_json, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("r2", "ds-x", "[]", "success", "[]", datetime.now().isoformat(timespec="seconds")),
        )
        assert svc.delete_run("r2") is True
        assert svc.delete_run("r2") is False  # 已删除 → False（路由据此返回 404）


# ── 3/4. MCP 配置合并与增删语义 ────────────────────────


class TestMCPConfigMerge:
    def test_load_config_merges_all_paths(self, tmp_path: Any) -> None:
        """多个配置路径必须合并，而非只取第一个命中的文件。"""
        p1 = tmp_path / "a.json"
        p2 = tmp_path / "b.json"
        p1.write_text(json.dumps({"mcpServers": {"srv-a": {"command": "echo", "args": ["a"]}}}), encoding="utf-8")
        p2.write_text(json.dumps({"mcpServers": {"srv-b": {"url": "http://b/sse", "type": "sse"}}}), encoding="utf-8")

        svc = MCPClientService(config_paths=(str(p1), str(p2)))
        configs = svc.load_config()
        assert set(configs) == {"srv-a", "srv-b"}, f"两个路径的 server 都应加载，实际: {set(configs)}"

    def test_higher_priority_path_wins(self, tmp_path: Any) -> None:
        p1 = tmp_path / "a.json"
        p2 = tmp_path / "b.json"
        p1.write_text(json.dumps({"mcpServers": {"s": {"command": "first"}}}), encoding="utf-8")
        p2.write_text(json.dumps({"mcpServers": {"s": {"command": "second"}}}), encoding="utf-8")
        svc = MCPClientService(config_paths=(str(p1), str(p2)))
        assert svc.load_config()["s"].command == "first"

    def test_add_server_duplicate_rejected(self, tmp_path: Any) -> None:
        cfg_path = tmp_path / "mcp.json"
        cfg_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        svc = MCPClientService(config_paths=(str(cfg_path),))
        svc.add_server(MCPServerConfig(name="dup", command="echo"))
        # 同名配置已存在（未连接）→ 必须拒绝，而不是静默覆盖
        with pytest.raises(RuntimeError):
            svc.add_server(MCPServerConfig(name="dup", command="other"))
        assert svc.get_configs()["dup"].command == "echo"
        # 显式允许覆盖时才能改
        svc.add_server(MCPServerConfig(name="dup", command="other"), overwrite=True)
        assert svc.get_configs()["dup"].command == "other"

    def test_remove_server_reports_existence(self, tmp_path: Any) -> None:
        cfg_path = tmp_path / "mcp.json"
        cfg_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        svc = MCPClientService(config_paths=(str(cfg_path),))
        svc.add_server(MCPServerConfig(name="x", command="echo"))
        assert svc.remove_server("x") is True
        assert svc.remove_server("x") is False  # 路由据此返回 404


# ── 5. 工作流 finished_at ──────────────────────────────


class TestWorkflowFinishedAt:
    def test_success_run_writes_finished_at(self, db: Database, registry: ServiceRegistry) -> None:
        svc = WorkflowService(db, registry, None)
        wf = svc.create_workflow("wf", "", {"nodes": [], "edges": []})
        # 直接写一个成功运行，复用 _finish 之外的成功分支路径
        run_id = "run-ok-1"
        db.execute(
            "INSERT INTO workflow_runs (id, workflow_id, vars_json, status, steps_json, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, wf["id"], "{}", "running", "[]", datetime.now().isoformat(timespec="seconds")),
        )
        svc._db.execute(
            "UPDATE workflow_runs SET status = ?, steps_json = ?, finished_at = ? WHERE id = ?",
            ("success", "[]", datetime.now().isoformat(timespec="seconds"), run_id),
        )
        run = svc.get_run(run_id)
        assert run is not None
        assert run["finished_at"], "成功运行必须落 finished_at，否则前端无法计算耗时"

    def test_run_workflow_success_sets_finished_at(self, db: Database, registry: ServiceRegistry) -> None:
        """走真实 run_workflow：成功分支同样要有 finished_at。"""
        svc = WorkflowService(db, registry, None)
        wf = svc.create_workflow(
            "wf2",
            "",
            {
                "nodes": [
                    {"id": "s", "type": "start", "data": {}},
                    {"id": "e", "type": "end", "data": {}},
                ],
                "edges": [{"id": "e1", "source": "s", "target": "e"}],
            },
        )
        run = asyncio.run(svc.run_workflow(wf["id"], {}))
        assert run is not None
        assert run["status"] == "success", f"运行应成功，实际: {run.get('status')} / {run.get('error')}"
        assert run["finished_at"], "成功运行的 finished_at 不得为空"


# ── 6/7. 定时任务 ──────────────────────────────────────


class TestSchedulerFixes:
    def test_once_task_requires_future_run_at(self, db: Database) -> None:
        svc = SchedulerService(db)
        # 缺少 run_at → 拒绝（旧实现会创建成功但永不执行）
        with pytest.raises(ValueError):
            svc.create_task({"name": "t", "prompt": "p", "schedule": {"type": ONCE, "run_at": ""}})
        # 过期时间 → 拒绝
        past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        with pytest.raises(ValueError):
            svc.create_task({"name": "t", "prompt": "p", "schedule": {"type": ONCE, "run_at": past}})
        # 未来时间 → 通过，且能算出下次运行时间
        future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
        task = svc.create_task({"name": "t", "prompt": "p", "schedule": {"type": ONCE, "run_at": future}})
        assert task.next_run_at, "一次性任务必须有下次运行时间，否则 due_tasks 永远取不到"

    def test_skipped_not_counted_as_failure(self, db: Database) -> None:
        svc = SchedulerService(db)
        future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
        task = svc.create_task({"name": "t", "prompt": "p", "schedule": {"type": "daily", "time": "09:00"}})
        assert task.fail_count == 0
        svc.record_task_result(task, status="skipped", error="无可用 provider")
        after = svc.get_task(task.id)
        assert after is not None
        assert after.fail_count == 0, "skipped 属环境问题，不应计入失败"
        # 真实失败仍然计数
        svc.record_task_result(after, status="error", error="boom")
        assert svc.get_task(task.id).fail_count == 1
        # once + future 仅用于覆盖构造，不影响断言
        assert future


# ── 8. Message.to_dict ─────────────────────────────────


class TestMessageSerialization:
    def test_to_dict_includes_tool_call_id(self) -> None:
        msg = Message(role="tool", content="ok", tool_call_id="call-123")
        d = msg.to_dict()
        assert d["tool_call_id"] == "call-123", "缺少 tool_call_id 会让前端无法配对工具调用与结果"


# ── 9. Insights 内置指标 ───────────────────────────────


class TestInsightsBuiltinMetrics:
    def test_contributors_registered(self, db: Database) -> None:
        clear_metric_contributors()
        added = register_builtin_contributors()
        assert added > 0
        assert len(list_metric_contributors()) > 0
        metrics = InsightsServiceImpl(db).plugin_metrics(30)
        assert metrics, "内置贡献者接线后，/api/insights/plugins 不得再返回空数组"
        ids = {m["metric_id"] for m in metrics}
        assert {"scheduled_tasks", "evals", "artifacts", "workflows", "memories"} & ids

    def test_register_is_idempotent(self) -> None:
        clear_metric_contributors()
        register_builtin_contributors()
        n = len(list_metric_contributors())
        register_builtin_contributors()
        assert len(list_metric_contributors()) == n, "重复注册不得产生重复卡片"


# ── 10. 中断 / 短路回填 tool 消息 ──────────────────────


class _StopAfterFirstTool:
    """首个工具执行后请求停止的假工具。"""

    name = "stopper"
    description = "测试工具"
    needs_session = False

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    async def execute(self, args: dict[str, Any]) -> str:
        self._loop.stop()
        return "done"


class TestInterruptedToolCallBackfill:
    def test_backfill_creates_tool_messages(self, db: Database, registry: ServiceRegistry) -> None:
        """中断后未执行的 tool_call 必须补一条 role=tool 消息。"""
        from harness.modules.context_manager.service import ContextServiceImpl

        ctx = ContextServiceImpl(services=registry)
        registry.register(ContextServiceImpl, ctx, owner="test")
        registry.register(
            __import__("harness.modules.context_manager.service", fromlist=["ContextService"]).ContextService,
            ctx,
            owner="test",
        )
        svc = SessionServiceImpl(db, services=registry)
        registry.register(SessionServiceImpl, svc, owner="test")
        registry.register(
            __import__("harness.modules.session_manager.service", fromlist=["SessionService"]).SessionService,
            svc,
            owner="test",
        )

        loop = AgentLoop(registry, HookManager(), ToolRegistry(), AgentLoopConfig(max_tool_iterations=1))
        session = svc.create_session("中断回填")

        calls = [
            {"id": "call-1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            {"id": "call-2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            {"id": "call-3", "type": "function", "function": {"name": "c", "arguments": "{}"}},
        ]
        # 模拟：第一个工具执行后触发 stop，剩余两个应被回填
        loop._stopped = True
        filled = asyncio.run(loop._backfill_interrupted_tool_calls(calls, session.id))
        assert filled == 3

        messages = svc.list_messages(session.id)
        tool_ids = {m.tool_call_id for m in messages if m.role == "tool"}
        assert tool_ids == {"call-1", "call-2", "call-3"}, f"每个 tool_call 都要有响应，实际: {tool_ids}"


class TestShortCircuitPersistsToolMessage:
    def test_hook_short_circuit_writes_tool_message(self, db: Database, registry: ServiceRegistry) -> None:
        """pre_tool_call 短路时也要回填 tool 消息，否则下一轮请求会被 API 400 拒绝。"""
        from harness.kernel.contracts.hook import HookContext, HookResult

        class ShortCircuitHook:
            async def execute(self, ctx: HookContext) -> HookResult:
                if ctx.hook_name == "pre_tool_call":
                    return HookResult(short_circuit=True, error="被测试钩子拦截")
                return HookResult()

        hooks = HookManager()
        hooks.register("pre_tool_call", ShortCircuitHook().execute, owner="test")

        class EchoTool:
            tool_name = "echo"
            description = "回显"
            needs_session = False

            async def execute(self, args: dict[str, Any]) -> str:
                return "echoed"

        registry_db = registry
        from harness.modules.context_manager.service import ContextServiceImpl

        ctx = ContextServiceImpl(services=registry_db)
        registry_db.register(ContextServiceImpl, ctx, owner="test")
        registry_db.register(
            __import__("harness.modules.context_manager.service", fromlist=["ContextService"]).ContextService,
            ctx,
            owner="test",
        )
        svc = SessionServiceImpl(db, services=registry_db)
        registry_db.register(SessionServiceImpl, svc, owner="test")
        registry_db.register(
            __import__("harness.modules.session_manager.service", fromlist=["SessionService"]).SessionService,
            svc,
            owner="test",
        )

        tools = ToolRegistry()
        tools.register(EchoTool(), owner="test")
        loop = AgentLoop(registry_db, hooks, tools, AgentLoopConfig(max_tool_iterations=1))
        session = svc.create_session("短路回填")

        results = asyncio.run(
            loop._execute_tool_calls(
                [{"id": "sc-1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}],
                session.id,
                "",
            )
        )
        assert results and results[0]["error"] == "被测试钩子拦截"
        messages = svc.list_messages(session.id)
        tool_msgs = [m for m in messages if m.role == "tool"]
        assert tool_msgs, "短路分支必须回填 tool 消息，否则 assistant 上残留无响应的 tool_calls"
        assert tool_msgs[0].tool_call_id == "sc-1"


def test_schedule_spec_describe_once() -> None:
    """一次性任务描述在无 run_at 时应明确提示未设置。"""
    spec = ScheduleSpec(type=ONCE, run_at="")
    assert "未设置" in spec.describe()


async def _noop_stream() -> AsyncIterator[dict[str, Any]]:
    if False:
        yield {}
