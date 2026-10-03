"""会话 / 消息搜索测试（E9）。"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from harness.infra.database import Database
from harness.modules.session_manager.service import SessionServiceImpl


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
def service(db: Database) -> SessionServiceImpl:
    """会话服务。"""
    return SessionServiceImpl(db)


def _add_messages(service: SessionServiceImpl, sid: str, contents: list[str]) -> None:
    """给会话追加若干 user 消息。"""
    for content in contents:
        service.append_message(sid, "user", content)


class TestSearchService:
    """搜索服务测试。"""

    def test_search_by_message_content(self, service: SessionServiceImpl) -> None:
        """命中消息内容的会话被返回。"""
        s1 = service.create_session(title="会话一")
        _add_messages(service, s1.id, ["今天天气怎么样", "帮我写个脚本"])
        s2 = service.create_session(title="会话二")
        _add_messages(service, s2.id, ["推荐一本书"])

        results = service.search_sessions("天气")
        ids = [r["session"]["id"] for r in results]
        assert s1.id in ids
        assert s2.id not in ids

    def test_search_by_title(self, service: SessionServiceImpl) -> None:
        """标题命中且无消息命中的会话也返回。"""
        s1 = service.create_session(title="项目复盘记录")
        _add_messages(service, s1.id, ["无关内容"])

        results = service.search_sessions("复盘")
        assert len(results) == 1
        assert results[0]["session"]["id"] == s1.id
        assert results[0]["title_hit"] is True
        assert results[0]["matches"] == []

    def test_snippet_contains_query(self, service: SessionServiceImpl) -> None:
        """命中片段包含查询词。"""
        s1 = service.create_session(title="S")
        _add_messages(service, s1.id, ["这是一段关于Docker的讨论内容"])

        results = service.search_sessions("Docker")
        assert results[0]["matches"][0]["snippet"] == "这是一段关于Docker的讨论内容"

    def test_empty_query(self, service: SessionServiceImpl) -> None:
        """空 / 空白查询返回空列表。"""
        s1 = service.create_session(title="S")
        _add_messages(service, s1.id, ["hello"])
        assert service.search_sessions("") == []
        assert service.search_sessions("   ") == []

    def test_like_wildcard_escaped(self, service: SessionServiceImpl) -> None:
        """查询中的 % / _ 不被当作通配符。"""
        s1 = service.create_session(title="S")
        _add_messages(service, s1.id, ["进度完成 100% 了", "a_b 测试"])

        # 搜 "%" 不应命中全部记录
        results = service.search_sessions("%")
        assert len(results) == 1
        assert "100%" in results[0]["matches"][0]["snippet"]

        results = service.search_sessions("_")
        assert len(results) == 1
        assert "a_b" in results[0]["matches"][0]["snippet"]

    def test_archived_excluded_by_default(self, service: SessionServiceImpl) -> None:
        """默认不搜归档会话；include_archived 可包含。"""
        s1 = service.create_session(title="归档会话")
        _add_messages(service, s1.id, ["归档里的关键词"])
        service.archive_session(s1.id)

        assert service.search_sessions("关键词") == []
        results = service.search_sessions("关键词", include_archived=True)
        assert len(results) == 1

    def test_matches_limited_to_three(self, service: SessionServiceImpl) -> None:
        """每个会话最多返回 3 条命中消息。"""
        s1 = service.create_session(title="S")
        _add_messages(service, s1.id, [f"关键词第{i}条" for i in range(5)])

        results = service.search_sessions("关键词")
        assert len(results[0]["matches"]) == 3
        assert results[0]["msg_hits"] == 5

    def test_case_insensitive(self, service: SessionServiceImpl) -> None:
        """搜索大小写不敏感。"""
        s1 = service.create_session(title="S")
        _add_messages(service, s1.id, ["Python Programming"])

        results = service.search_sessions("python")
        assert len(results) == 1

    def test_message_count_included(self, service: SessionServiceImpl) -> None:
        """结果会话带 message_count。"""
        s1 = service.create_session(title="S")
        _add_messages(service, s1.id, ["命中消息", "另一条消息"])

        results = service.search_sessions("命中")
        assert results[0]["session"]["message_count"] == 2


class TestSearchAPI:
    """搜索 REST 路由测试。"""

    @pytest.fixture
    def client(self) -> TestClient:
        """创建测试客户端。"""
        from harness.main import app

        with TestClient(app) as c:
            yield c

    def test_search_route(self, client: TestClient) -> None:
        """GET /api/sessions/search?q= 返回结果。"""
        sid = client.post("/api/sessions", json={"title": "搜索 API 测试"}).json()["id"]
        client.post(
            f"/api/sessions/{sid}/messages",
            json={"role": "user", "content": "需要搜索的关键词XYZ"},
        )

        resp = client.get("/api/sessions/search", params={"q": "关键词XYZ"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["session"]["id"] == sid

    def test_search_not_treated_as_session_id(self, client: TestClient) -> None:
        """路由顺序：/search 不被 /{session_id} 捕获。"""
        resp = client.get("/api/sessions/search", params={"q": "anything"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_search_empty_q(self, client: TestClient) -> None:
        """空 q 也返回空列表（FastAPI 要求 q 参数存在）。"""
        resp = client.get("/api/sessions/search", params={"q": ""})
        assert resp.status_code == 200
        assert resp.json() == []
