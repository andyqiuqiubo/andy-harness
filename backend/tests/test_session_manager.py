"""会话管理测试（SessionService CRUD + 消息追加 + 持久化）。"""

import os
import tempfile

import pytest

from harness.infra.database import Database
from harness.modules.session_manager.service import SessionServiceImpl


@pytest.fixture
def db() -> Database:
    """创建临时数据库。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    database.close()
    os.unlink(path)


@pytest.fixture
def service(db: Database) -> SessionServiceImpl:
    """创建 SessionService。"""
    return SessionServiceImpl(db)


class TestSessionCRUD:
    """会话 CRUD 测试。"""

    def test_create_session(self, service: SessionServiceImpl) -> None:
        """创建会话。"""
        session = service.create_session("测试会话")
        assert session.id is not None
        assert session.title == "测试会话"
        assert session.archived is False

    def test_get_session(self, service: SessionServiceImpl) -> None:
        """获取会话。"""
        created = service.create_session("测试")
        fetched = service.get_session(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.title == "测试"

    def test_get_nonexistent_session(self, service: SessionServiceImpl) -> None:
        """获取不存在的会话返回 None。"""
        assert service.get_session("nonexistent") is None

    def test_list_sessions(self, service: SessionServiceImpl) -> None:
        """列出会话。"""
        service.create_session("会话1")
        service.create_session("会话2")
        sessions = service.list_sessions()
        assert len(sessions) == 2

    def test_rename_session(self, service: SessionServiceImpl) -> None:
        """重命名会话。"""
        session = service.create_session("旧名称")
        renamed = service.rename_session(session.id, "新名称")
        assert renamed is not None
        assert renamed.title == "新名称"

    def test_archive_session(self, service: SessionServiceImpl) -> None:
        """归档会话。"""
        session = service.create_session("待归档")
        archived = service.archive_session(session.id)
        assert archived is not None
        assert archived.archived is True

        # 默认不列出已归档的
        sessions = service.list_sessions()
        assert len(sessions) == 0

        # 包含已归档
        all_sessions = service.list_sessions(include_archived=True)
        assert len(all_sessions) == 1

    def test_delete_session(self, service: SessionServiceImpl) -> None:
        """删除会话。"""
        session = service.create_session("待删除")
        assert service.delete_session(session.id) is True
        assert service.get_session(session.id) is None


class TestMessageAppend:
    """消息追加测试。"""

    def test_append_message(self, service: SessionServiceImpl) -> None:
        """追加消息到会话。"""
        session = service.create_session("测试")
        msg = service.append_message(
            session.id, role="user", content="你好", tokens=10
        )
        assert msg.session_id == session.id
        assert msg.role == "user"
        assert msg.content == "你好"
        assert msg.tokens == 10

    def test_list_messages(self, service: SessionServiceImpl) -> None:
        """列出会话消息。"""
        session = service.create_session("测试")
        service.append_message(session.id, role="user", content="你好")
        service.append_message(session.id, role="assistant", content="你好！")
        service.append_message(session.id, role="user", content="再见")

        messages = service.list_messages(session.id)
        assert len(messages) == 3
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"
        assert messages[2].role == "user"

    def test_append_message_with_tool_calls(self, service: SessionServiceImpl) -> None:
        """带 tool_calls 的消息。"""
        session = service.create_session("测试")
        tool_calls = [{"id": "call-1", "function": {"name": "calc", "arguments": "{}"}}]
        msg = service.append_message(
            session.id, role="assistant", content="", tool_calls=tool_calls
        )
        assert msg.tool_calls == tool_calls


class TestPersistence:
    """持久化测试（重启后历史完整加载）。"""

    def test_data_persists_across_reconnect(self) -> None:
        """重启服务后历史完整加载。"""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            # 第一次连接：创建会话和消息
            db1 = Database(path)
            service1 = SessionServiceImpl(db1)
            session = service1.create_session("持久化测试")
            service1.append_message(session.id, role="user", content="你好")
            service1.append_message(session.id, role="assistant", content="你好！")
            db1.close()

            # 第二次连接：验证数据仍在
            db2 = Database(path)
            service2 = SessionServiceImpl(db2)
            sessions = service2.list_sessions()
            assert len(sessions) == 1
            assert sessions[0].title == "持久化测试"

            messages = service2.list_messages(session.id)
            assert len(messages) == 2
            assert messages[0].content == "你好"
            assert messages[1].content == "你好！"
            db2.close()
        finally:
            os.unlink(path)
