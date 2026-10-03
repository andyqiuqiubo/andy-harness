"""附件（文档 / 图片）上传、校验、回传与多模态渲染测试。"""

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    from harness.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def session_id(client: TestClient) -> str:
    resp = client.post("/api/sessions", json={"title": "附件测试"})
    return resp.json()["id"]


def _upload(client: TestClient, sid: str, files: list[tuple]) -> object:
    # 每个元素为 (content_bytes, filename, mime)
    prepped = []
    for data, filename, mime in files:
        prepped.append(("files", (filename, io.BytesIO(data), mime)))
    return client.post(f"/api/sessions/{sid}/attachments", files=prepped)


class TestUpload:
    def test_upload_document_and_image(self, client: TestClient, session_id: str) -> None:
        txt = (b"hello world\n", "note.txt", "text/plain")
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "pic.png", "image/png")
        resp = _upload(client, session_id, [txt, png])
        assert resp.status_code == 200, resp.text
        data = resp.json()
        atts = data["attachments"]
        assert len(atts) == 2
        kinds = {a["kind"] for a in atts}
        assert kinds == {"document", "image"}
        for a in atts:
            # 公开元信息不得包含存储路径
            assert "storage_path" not in a
            assert "id" in a and "filename" in a and "mime" in a and "size" in a
            assert a["size"] > 0

    def test_upload_unsupported_type(self, client: TestClient, session_id: str) -> None:
        resp = _upload(client, session_id, [(b"x", "evil.exe", "application/x-msdownload")])
        assert resp.status_code == 400
        assert "ATTACHMENT_INVALID" in resp.text

    def test_upload_document_too_large(self, client: TestClient, session_id: str) -> None:
        big = ("x" * (201 * 1024)).encode("utf-8")
        resp = _upload(client, session_id, [(big, "big.txt", "text/plain")])
        assert resp.status_code == 400
        assert "ATTACHMENT_INVALID" in resp.text

    def test_upload_exceeds_document_limit(self, client: TestClient, session_id: str) -> None:
        files = [(b"doc", f"d{i}.txt", "text/plain") for i in range(6)]
        resp = _upload(client, session_id, files)
        assert resp.status_code == 400
        assert "ATTACHMENT_LIMIT" in resp.text

    def test_upload_to_missing_session(self, client: TestClient) -> None:
        resp = _upload(client, "no-such-session", [(b"x", "a.txt", "text/plain")])
        assert resp.status_code == 404

    def test_serve_attachment(self, client: TestClient, session_id: str) -> None:
        resp = _upload(client, session_id, [(b"secret", "a.txt", "text/plain")])
        att_id = resp.json()["attachments"][0]["id"]
        get_resp = client.get(f"/api/sessions/{session_id}/attachments/{att_id}")
        assert get_resp.status_code == 200
        assert get_resp.content == b"secret"
        assert get_resp.headers["content-type"].startswith("text/plain")

    def test_serve_attachment_wrong_session(self, client: TestClient, session_id: str) -> None:
        resp = _upload(client, session_id, [(b"secret", "a.txt", "text/plain")])
        att_id = resp.json()["attachments"][0]["id"]
        other = client.post("/api/sessions", json={"title": "other"}).json()["id"]
        get_resp = client.get(f"/api/sessions/{other}/attachments/{att_id}")
        assert get_resp.status_code == 404


class TestMessageWithAttachments:
    def test_append_message_with_attachments(self, client: TestClient, session_id: str) -> None:
        resp = _upload(client, session_id, [(b"hi", "a.txt", "text/plain")])
        att = resp.json()["attachments"][0]
        msg_resp = client.post(
            f"/api/sessions/{session_id}/messages",
            json={
                "role": "user",
                "content": "看图",
                "attachments": [att],
            },
        )
        assert msg_resp.status_code == 200
        body = msg_resp.json()
        assert body["attachments"] == [att]

    def test_list_messages_returns_attachments(self, client: TestClient, session_id: str) -> None:
        resp = _upload(client, session_id, [(b"hi", "a.txt", "text/plain")])
        att = resp.json()["attachments"][0]
        client.post(
            f"/api/sessions/{session_id}/messages",
            json={"role": "user", "content": "x", "attachments": [att]},
        )
        lst = client.get(f"/api/sessions/{session_id}/messages").json()
        user_msgs = [m for m in lst if m["role"] == "user"]
        assert user_msgs and user_msgs[0]["attachments"] == [att]


class TestClassifyLimits:
    def test_classify_allowed(self) -> None:
        from harness.modules.attachment.service import classify

        kind, mime = classify("a.md", 10)
        assert kind == "document" and mime == "text/markdown"
        kind, mime = classify("a.png", 10)
        assert kind == "image" and mime == "image/png"

    def test_classify_rejects_unknown(self) -> None:
        from harness.modules.attachment.service import AttachmentError, classify

        with pytest.raises(AttachmentError):
            classify("a.xyz", 10)

    def test_limits_constant(self) -> None:
        from harness.modules.attachment.limits import (
            MAX_DOCUMENTS_PER_MESSAGE,
            MAX_FILES_PER_MESSAGE,
            MAX_IMAGES_PER_MESSAGE,
        )

        assert MAX_DOCUMENTS_PER_MESSAGE == 5
        assert MAX_IMAGES_PER_MESSAGE == 4
        assert MAX_FILES_PER_MESSAGE == 8


class TestRenderContentParts:
    def test_text_only_returns_string(self) -> None:
        from harness.modules.attachment.service import render_content_parts

        out = render_content_parts("hi", [], None)
        assert out == "hi"

    def test_renders_document_and_image(self, tmp_path) -> None:
        from harness.modules.attachment.service import render_content_parts

        img_path = tmp_path / "p.png"
        img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
        doc_path = tmp_path / "d.txt"
        doc_path.write_text("文档内容abc", encoding="utf-8")

        class FakeRepo:
            def get(self, att_id: str) -> dict:
                if "img" in att_id:
                    return {"storage_path": str(img_path), "mime": "image/png"}
                return {"storage_path": str(doc_path), "mime": "text/plain"}

        metas = [
            {"id": "img1", "kind": "image", "filename": "p.png"},
            {"id": "doc1", "kind": "document", "filename": "d.txt"},
        ]
        parts = render_content_parts("问题", metas, FakeRepo())
        # 文本块 + 图片块 + 文档块
        assert parts[0]["type"] == "text" and parts[0]["text"] == "问题"
        assert parts[1]["type"] == "image_url"
        assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert parts[2]["type"] == "text" and "[文档 d.txt]" in parts[2]["text"]
        assert "文档内容abc" in parts[2]["text"]


class TestContextMultimodal:
    """ContextService.build 应把带附件的 user 消息转成多模态 content。"""

    def test_build_renders_attachments_multimodal(self, client: TestClient, session_id: str) -> None:
        from harness.main import _services as app_services
        from harness.modules.context_manager.service import ContextServiceImpl

        # 上传一个文档
        up = _upload(client, session_id, [("文件正文xyz".encode(), "note.txt", "text/plain")])
        assert up.status_code == 200
        att = up.json()["attachments"][0]

        # 通过 REST 追加一条 user 消息，带附件
        client.post(
            f"/api/sessions/{session_id}/messages",
            json={"role": "user", "content": "读一下这个", "attachments": [att]},
        )

        # 直接复用 app 全局注册表（lifespan 已注册 Database/SessionService 等）
        ctx = ContextServiceImpl(app_services)
        msgs = ctx.build(session_id)
        user_msgs = [m for m in msgs if m["role"] == "user"]
        assert user_msgs, "至少一条 user 消息"
        content = user_msgs[0]["content"]
        # 多模态：content 是 list，含文本块 + 文档块
        assert isinstance(content, list)
        assert content[0]["type"] == "text" and content[0]["text"] == "读一下这个"
        assert any(b.get("type") == "text" and "[文档 note.txt]" in b.get("text", "") for b in content)
        # 附件元数据不能泄进 messages（已 pop）
        assert "attachments" not in user_msgs[0]

    def test_build_plain_text_unchanged(self, client: TestClient, session_id: str) -> None:
        """无附件的消息保持纯字符串 content，不影响既有链路。"""
        from harness.main import _services as app_services
        from harness.modules.context_manager.service import ContextServiceImpl

        client.post(
            f"/api/sessions/{session_id}/messages",
            json={"role": "user", "content": "只是普通文本"},
        )
        msgs = ContextServiceImpl(app_services).build(session_id)
        user_msgs = [m for m in msgs if m["role"] == "user"]
        assert user_msgs
        assert user_msgs[0]["content"] == "只是普通文本"
        assert isinstance(user_msgs[0]["content"], str)


class TestSessionDeleteCleansAttachments:
    def test_delete_session_removes_files_and_rows(self, client: TestClient, session_id: str) -> None:
        """删会话时附件文件与数据库行都要一起清掉。"""
        from pathlib import Path

        from harness.main import _services as app_services
        from harness.modules.attachment.limits import attachments_dir

        up = _upload(
            client,
            session_id,
            [(b"data", "a.txt", "text/plain"), (b"data", "b.txt", "text/plain")],
        )
        atts = up.json()["attachments"]
        sess_dir = Path(attachments_dir()) / session_id
        assert sess_dir.exists()
        stored = list(sess_dir.iterdir())
        assert len(stored) == 2, f"期望 2 个物理文件，实际 {len(stored)}"

        # 删会话
        resp = client.delete(f"/api/sessions/{session_id}")
        assert resp.status_code in (200, 204)

        # 物理文件应消失
        assert not sess_dir.exists() or not any(sess_dir.iterdir())

        # 数据库行也应清空
        from harness.infra.database import Database
        from harness.infra.repository import AttachmentRepository

        db = app_services.get(Database)
        repo = AttachmentRepository(db)
        for a in atts:
            assert repo.get(a["id"]) is None


class TestAttachmentLifecycleFixes:
    """本轮审查修复项的回归覆盖：P1-2 / P1-3 / P1-4 / P2-1 / P2-3。"""

    def _append_user_msg(self, client: TestClient, sid: str, att: dict, text: str = "带附件") -> dict:
        resp = client.post(
            f"/api/sessions/{sid}/messages",
            json={"role": "user", "content": text, "attachments": [att]},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_delete_turn_purges_attachments(self, client: TestClient, session_id: str) -> None:
        """P1-2：删除整轮问答必须连带回收附件文件与元数据，不能留孤儿。"""
        from harness.infra.database import Database
        from harness.infra.repository import AttachmentRepository
        from harness.main import _services as app_services
        from harness.modules.attachment.limits import attachments_dir

        att = _upload(client, session_id, [(b"payload", "a.txt", "text/plain")]).json()["attachments"][0]
        msg = self._append_user_msg(client, session_id, att)

        sess_dir = Path(attachments_dir()) / session_id
        assert any(sess_dir.iterdir())

        resp = client.delete(f"/api/sessions/{session_id}/messages/{msg['id']}?with_turn=true")
        assert resp.status_code == 200

        repo = AttachmentRepository(app_services.get(Database))
        assert repo.get(att["id"]) is None, "附件元数据未被清理"
        assert not sess_dir.exists() or not any(sess_dir.iterdir()), "附件文件未被清理"

    def test_fork_clones_attachments_independently(self, client: TestClient, session_id: str) -> None:
        """P1-3：分叉得到的是**副本**，与原会话互不干扰。"""
        from harness.infra.database import Database
        from harness.infra.repository import AttachmentRepository
        from harness.main import _services as app_services
        from harness.modules.attachment.limits import attachments_dir

        att = _upload(client, session_id, [(b"payload", "a.txt", "text/plain")]).json()["attachments"][0]
        msg = self._append_user_msg(client, session_id, att)

        fork = client.post(f"/api/sessions/{session_id}/fork", json={"at_message_id": msg["id"]}).json()

        msgs = client.get(f"/api/sessions/{fork['id']}/messages").json()
        user_msgs = [m for m in msgs if m["role"] == "user"]
        assert user_msgs, "分叉后应包含带附件的 user 消息"
        cloned = user_msgs[0]["attachments"][0]

        # 必须是新的 id（不能与原会话共享同一份记录）
        assert cloned["id"] != att["id"]
        # 文件应物理复制到新会话目录
        fork_dir = Path(attachments_dir()) / fork["id"]
        assert fork_dir.exists() and any(fork_dir.iterdir())

        # 删除分叉会话不应影响原会话的附件
        client.delete(f"/api/sessions/{fork['id']}")
        repo = AttachmentRepository(app_services.get(Database))
        assert repo.get(att["id"]) is not None, "原会话附件被误删（说明两者仍共享）"

    def test_upload_rejects_too_many_files(self, client: TestClient, session_id: str) -> None:
        """P1-4：数量超限必须在读取文件内容之前就被拒绝。"""
        files = [(b"x", f"f{i}.txt", "text/plain") for i in range(9)]
        resp = _upload(client, session_id, files)
        assert resp.status_code == 400
        assert "ATTACHMENT_LIMIT" in resp.text

    def test_dotfile_extension_accepted(self, client: TestClient, session_id: str) -> None:
        """P2-1：点文件名此前因 splitext 取不到后缀而被后端拒绝（前端却放行）。"""
        resp = _upload(client, session_id, [(b"node_modules\n", ".gitignore", "text/plain")])
        assert resp.status_code == 200, resp.text
        resp2 = _upload(client, session_id, [(b"KEY=1\n", ".env.example", "text/plain")])
        assert resp2.status_code == 200, resp2.text

    def test_list_messages_pagination(self, client: TestClient, session_id: str) -> None:
        """P2-3：list_messages 支持 limit / offset；不传时行为不变。"""
        for i in range(5):
            client.post(
                f"/api/sessions/{session_id}/messages",
                json={"role": "user", "content": f"m{i}"},
            )

        all_msgs = client.get(f"/api/sessions/{session_id}/messages").json()
        assert len(all_msgs) == 5

        first_page = client.get(f"/api/sessions/{session_id}/messages?limit=2").json()
        assert len(first_page) == 2
        assert first_page[0]["content"] == all_msgs[0]["content"]

        second_page = client.get(f"/api/sessions/{session_id}/messages?limit=2&offset=2").json()
        assert len(second_page) == 2
        assert second_page[0]["content"] == all_msgs[2]["content"]
