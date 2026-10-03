"""本轮审计修复项的回归覆盖（P0-1 / P0-2 / P0-4 / P2-2）。

- P0-1 / P0-2：附件 / 工件 / 轨迹在多用户（E12 启用）下按 user_id 两跳归属过滤，
  他人会话的工件 / 轨迹不可见、不可读、不可删；单用户默认部署（user_id 为空）不过滤。
- P0-4：WS 单 reader 重构后，控制帧（stop）由 dispatcher 统一处理并返回 stop_ack，
  主循环不再因并发 receive_text 竞争而丢帧。
- P2-2：认证密钥持久化到 data/auth_secret.txt，重启后保持稳定（已签发 token 不失效），
  且文件权限收敛为 0o600。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from harness.infra.database import Database
from harness.infra.repository import Session, SessionRepository
from harness.modules.artifact_store.service import ArtifactStoreImpl
from harness.modules.tracing.service import SpanServiceImpl


@pytest.fixture
def client() -> TestClient:
    from harness.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(tmp_path) -> Database:
    # 每个测试使用独立的临时数据库，避免与共享 DB 文件的连接相互持锁
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def tmp_artifacts_dir(tmp_path) -> str:
    d = tmp_path / "artifacts"
    d.mkdir()
    return str(d)


class TestArtifactOwnership:
    """P0-2：工件按归属用户两跳过滤。"""

    def test_list_artifacts_filters_by_user(self, db: Database, tmp_artifacts_dir: str) -> None:
        sessions = SessionRepository(db)
        sessions.create(Session(id="sA", title="A"), user_id="userA")
        sessions.create(Session(id="sB", title="B"), user_id="userB")

        store = ArtifactStoreImpl(db, base_dir=tmp_artifacts_dir)
        store.offload("sA", "tool_x", "artifact of A")
        store.offload("sB", "tool_y", "artifact of B")

        a_items = store.list_artifacts(user_id="userA")
        b_items = store.list_artifacts(user_id="userB")

        assert len(a_items) == 1 and a_items[0].session_id == "sA"
        assert len(b_items) == 1 and b_items[0].session_id == "sB"
        # 任意一方都看不到对方的工件
        assert all(it.session_id == "sA" for it in a_items)
        assert all(it.session_id == "sB" for it in b_items)

    def test_list_artifacts_empty_user_id_no_filter(self, db: Database, tmp_artifacts_dir: str) -> None:
        sessions = SessionRepository(db)
        sessions.create(Session(id="sA", title="A"), user_id="userA")
        sessions.create(Session(id="sB", title="B"), user_id="userB")

        store = ArtifactStoreImpl(db, base_dir=tmp_artifacts_dir)
        store.offload("sA", "t", "a")
        store.offload("sB", "t", "b")

        # 单用户默认部署：user_id 为空，不过滤
        all_items = store.list_artifacts(user_id="")
        assert len(all_items) == 2


class TestTraceOwnership:
    """P0-2：轨迹（traces）按归属用户两跳过滤。"""

    def test_list_traces_filters_by_user(self, db: Database) -> None:
        sessions = SessionRepository(db)
        sessions.create(Session(id="sA", title="A"), user_id="userA")
        sessions.create(Session(id="sB", title="B"), user_id="userB")

        spans = SpanServiceImpl(db)
        db.execute(
            "INSERT INTO spans (id, trace_id, session_id, name, kind, status, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("spA", "traceA", "sA", "run A", "run", "ok", 10),
        )
        db.execute(
            "INSERT INTO spans (id, trace_id, session_id, name, kind, status, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("spB", "traceB", "sB", "run B", "run", "ok", 20),
        )

        a_traces = spans.list_traces(user_id="userA")
        b_traces = spans.list_traces(user_id="userB")

        assert len(a_traces) == 1 and a_traces[0].session_id == "sA"
        assert len(b_traces) == 1 and b_traces[0].session_id == "sB"


class TestWsControlFrame:
    """P0-4：WS 控制帧（stop）由 dispatcher 稳定处理，无并发 receive_text 竞争。"""

    def test_stop_control_frame_returns_ack(self, client) -> None:
        with client.websocket_connect("/ws/chat") as ws:
            for _ in range(2):
                ws.send_json({"type": "stop"})
                frame = ws.receive_json()
                assert frame.get("type") == "stop_ack", frame


class TestAuthSecretPersistence:
    """P2-2：认证密钥持久化，重启后稳定。"""

    def test_secret_persists_across_restart(self, tmp_path, monkeypatch) -> None:
        secret_file = tmp_path / "auth_secret.txt"

        # 把密钥落地文件重定向到临时目录，避免触碰真实 data/ 目录
        monkeypatch.setattr(
            "harness.modules.auth_manager.service.AuthServiceImpl._secret_file",
            staticmethod(lambda: secret_file),
        )
        monkeypatch.delenv("HARNESS_AUTH_SECRET", raising=False)

        from harness.infra.database import Database
        from harness.modules.auth_manager.service import AuthServiceImpl

        db = Database(str(tmp_path / "auth.db"))
        svc1 = AuthServiceImpl(db)
        secret1 = svc1.secret
        assert secret1, "密钥不应为空"
        assert secret_file.exists(), "密钥应持久化到文件"

        # 模拟重启：新建实例，应从文件加载同一密钥
        svc2 = AuthServiceImpl(db)
        assert svc2.secret == secret1

        # 文件权限应尽力收敛为 0o600（仅属主可读写；Windows 上 chmod 语义有限，跳过强校验）
        if os.name == "posix":
            mode = secret_file.stat().st_mode & 0o777
            assert mode == 0o600, f"密钥文件权限应为 600，实际 {oct(mode)}"
