"""media_gen 服务单元测试。

全部用例**不联网**：通过 monkeypatch 替换上游提交/下载，仅校验参数校验、
状态机与落盘读取逻辑（真实联调由 tools/e2e_media_check.py 覆盖）。
"""

from __future__ import annotations

import asyncio
import pytest

from harness.infra.database import Database
from harness.modules.media_gen.service import (
    COGVIEW_SIZES,
    MediaGenError,
    MediaGenService,
)


@pytest.fixture()
def svc(tmp_path):
    db = Database(":memory:")
    service = MediaGenService(db=db, data_dir=tmp_path)
    service.set_api_key("test-key")
    return service


def _jpeg(tmp_path, name="a.jpg") -> str:
    p = tmp_path / name
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
    return str(p)


def test_image_sync_path_marks_success_and_stores_file(svc, tmp_path, monkeypatch):
    """图像同步返回 data[0].url：直接落盘并置 SUCCESS，产物可读。"""
    async def fake_post(*_a, **_k):
        return {"data": [{"url": "http://example.com/a.jpg"}]}

    async def fake_download(self, task_id, url, kind):
        path = _jpeg(tmp_path, f"{task_id}.jpg")
        return path, "image/jpeg", 36

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))
    monkeypatch.setattr(MediaGenService, "_download_and_store", fake_download)

    task = asyncio.run(svc.create_task("image", "cogview-3-flash", "水墨远山", size="1024x1024"))
    assert task["status"] == "SUCCESS"
    assert task["kind"] == "image"
    data, mime = svc.read_file(task["id"])
    assert data[:2] == b"\xff\xd8" and mime == "image/jpeg"


def test_video_submit_is_async_processing(svc, monkeypatch):
    """视频恒异步：提交后为 PROCESSING 并保存上游任务号。"""
    async def fake_post(*_a, **_k):
        return {"id": "up-1", "task_status": "PROCESSING"}

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))

    task = asyncio.run(svc.create_task("video", "cogvideox-flash", "一只猫", size="720x480"))
    assert task["status"] == "PROCESSING"
    assert task["upstream_id"] == "up-1"


def test_rejects_cogvideox3_only_sizes(svc):
    """1280x720 属于 cogvideox-3，flash 不接受（参数枚举必须区分）。"""
    with pytest.raises(MediaGenError) as e:
        asyncio.run(svc.create_task("video", "cogvideox-flash", "x", size="1280x720"))
    assert "size" in str(e.value)


def test_rejects_unknown_model_and_kind(svc):
    with pytest.raises(MediaGenError):
        asyncio.run(svc.create_task("image", "not-a-model", "x"))
    with pytest.raises(MediaGenError):
        asyncio.run(svc.create_task("audio", "cogvideox-flash", "x"))


def test_video_model_cannot_be_used_as_image(svc):
    with pytest.raises(MediaGenError):
        asyncio.run(svc.create_task("image", "cogvideox-flash", "x"))


def test_requires_prompt_or_image(svc):
    with pytest.raises(MediaGenError):
        asyncio.run(svc.create_task("video", "cogvideox-flash", "", image=""))


def test_unknown_task_raises(svc):
    with pytest.raises(MediaGenError):
        asyncio.run(svc.get_task("nope"))


def test_list_tasks_filters_by_session(svc, monkeypatch):
    async def fake_post(*_a, **_k):
        return {"id": "up-2"}

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))
    asyncio.run(svc.create_task("video", "cogvideox-flash", "a", session_id="s1"))
    asyncio.run(svc.create_task("video", "cogvideox-flash", "b", session_id="s2"))

    assert len(svc.list_tasks("s1")) == 1
    assert len(svc.list_tasks()) == 2


def test_video_data_uri_passthrough_and_has_image(svc, monkeypatch):
    """图生视频（前端附件 Base64 通道）：data URI 原样透传为上游 image_url，
    并落库 has_image=true——联调时据此确认「图片确实发给了模型」。"""
    captured = {}

    async def fake_post(*args, **_k):
        captured["payload"] = args[1]
        return {"id": "up-i2v", "task_status": "PROCESSING"}

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))

    data_uri = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD"
    task = asyncio.run(svc.create_task(
        "video", "cogvideox-flash", "让画面动起来", image=data_uri, size="720x480",
    ))
    # 不落地、不改写，原样进入上游 payload
    assert captured["payload"]["image_url"] == data_uri
    assert task["params"]["has_image"] is True
    assert task["status"] == "PROCESSING"


def test_video_local_image_file_becomes_data_uri(svc, tmp_path, monkeypatch):
    """本地路径图片：读字节转 data:...;base64 后提交（_as_image_url 路径）。"""
    captured = {}

    async def fake_post(*args, **_k):
        captured["payload"] = args[1]
        return {"id": "up-local", "task_status": "PROCESSING"}

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))

    asyncio.run(svc.create_task(
        "video", "cogvideox-flash", "x", image=_jpeg(tmp_path, "i2v.jpg"), size="720x480",
    ))
    assert captured["payload"]["image_url"].startswith("data:image/jpeg;base64,")


def test_content_endpoint_sets_download_filename(monkeypatch):
    """content 端点带 Content-Disposition：图片落 .jpg、视频落 .mp4。

    直接调 REST 函数（不起 TestClient），避免 harness.main 全局单例污染。
    """
    from harness.api.rest.media import get_task_content

    def fake_jpeg(self, task_id):
        return b"\xff\xd8\xff\xe0" + b"\x00" * 16, "image/jpeg"

    monkeypatch.setattr(MediaGenService, "read_file", fake_jpeg)
    resp = asyncio.run(get_task_content("deadbeef"))
    assert 'attachment; filename="andy-harness_deadbeef.jpg"' in resp.headers.get(
        "content-disposition", "")

    def fake_mp4(self, task_id):
        return b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8, "video/mp4"

    monkeypatch.setattr(MediaGenService, "read_file", fake_mp4)
    resp = asyncio.run(get_task_content("cafe1234"))
    assert 'attachment; filename="andy-harness_cafe1234.mp4"' in resp.headers.get(
        "content-disposition", "")


def test_submit_failure_persists_fail_task(svc, monkeypatch):
    """提交被上游拒绝（如 1305）也必须落库 FAIL——刷新后能看到失败记录与原因。"""
    async def fake_post(*_a, **_k):
        raise MediaGenError("上游错误: {'code': '1305', 'message': '访问量过大'}")

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))

    task = asyncio.run(svc.create_task("video", "cogvideox-flash", "x", size="720x480"))
    assert task["status"] == "FAIL", "提交失败必须落库 FAIL，而不是抛异常丢任务"
    assert "1305" in task["error"]
    assert asyncio.run(svc.get_task(task["id"]))["status"] == "FAIL"


def test_task_times_out_after_max_wait_and_stops_polling(svc, monkeypatch):
    """超过 max_wait：判 FAIL、写明确原因，且不再向上游查询（主动断开）。"""
    async def fake_post(*_a, **_k):
        return {"id": "up-timeout", "task_status": "PROCESSING"}

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))
    task = asyncio.run(svc.create_task(
        "video", "cogvideox-flash", "x", size="720x480", max_wait_seconds=3600,
    ))
    assert task["status"] == "PROCESSING"
    assert task["params"]["max_wait"] == 3600

    # 把截止时间推到过去，模拟已排队超时
    import json as _json

    row = svc.db.query_one(
        "SELECT params_json FROM media_tasks WHERE id = ?", (task["id"],))
    p = _json.loads(row["params_json"])
    p["deadline_ts"] = 1
    svc.db.execute(
        "UPDATE media_tasks SET params_json = ? WHERE id = ?",
        (_json.dumps(p), task["id"]),
    )

    upstream_calls = []

    async def fake_refresh(tid):
        upstream_calls.append(tid)

    monkeypatch.setattr(MediaGenService, "_refresh", staticmethod(fake_refresh))

    t = asyncio.run(svc.get_task(task["id"], poll=True))
    assert t["status"] == "FAIL"
    assert "超时" in t["error"] and "最大等待" in t["error"]
    assert upstream_calls == [], "超时后不得再向智谱查询（主动断开）"


def test_params_record_reference_attachments(svc, monkeypatch):
    """参考图片元信息落库：刷新后前端据此恢复问答区的缩略图。"""
    async def fake_post(*_a, **_k):
        return {"id": "up-att", "task_status": "PROCESSING"}

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))
    atts = [{"id": "att-1", "filename": "pig.png", "mime": "image/png"}]
    task = asyncio.run(svc.create_task(
        "video", "cogvideox-flash", "小猪与小狗", image="data:image/png;base64,AAA",
        size="720x480", attachments=atts,
    ))
    assert task["params"]["attachments"] == atts
    assert task["params"]["has_image"] is True
    assert task["params"]["deadline_ts"] > 0


def test_video_defaults_use_max_available_quality(svc, monkeypatch):
    """视频默认取最大可用档：1080p + quality（原 720x480 / speed）。"""
    captured = {}

    async def fake_post(*args, **_k):
        captured["payload"] = args[1]
        return {"id": "up-def", "task_status": "PROCESSING"}

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))

    task = asyncio.run(svc.create_task("video", "cogvideox-flash", "x"))
    assert task["params"]["size"] == "1920x1080"
    assert task["params"]["quality"] == "quality"
    assert captured["payload"]["size"] == "1920x1080"
    assert captured["payload"]["quality"] == "quality"


def test_image_default_size_is_largest_in_catalog(svc, monkeypatch):
    """生图默认 1024x1024 ——COGVIEW 尺寸清单中像素量最大的一项。"""
    captured = {}

    async def fake_post(*args, **_k):
        captured["payload"] = args[1]
        return {"id": "up-img", "task_status": "PROCESSING"}

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))

    task = asyncio.run(svc.create_task("image", "cogview-3-flash", "x"))
    assert task["params"]["size"] == "1024x1024"
    assert captured["payload"]["size"] == "1024x1024"
    # 清单里没有任何尺寸的像素总量超过 1024x1024
    assert max(
        int(s.split("x")[0]) * int(s.split("x")[1]) for s in COGVIEW_SIZES
    ) == 1024 * 1024


def test_watermark_defaults_off(svc, monkeypatch):
    """默认不带水印（watermark_enabled=false）：用户明确要求关闭 AI 生成水印。"""
    captured = {}

    async def fake_post(*args, **_k):
        captured["payload"] = args[1]
        return {"id": "up-wm", "task_status": "PROCESSING"}

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))

    task = asyncio.run(svc.create_task("image", "cogview-3-flash", "x"))
    assert task["params"]["watermark"] is False
    assert captured["payload"]["watermark_enabled"] is False


def test_watermark_defaults_off(svc, monkeypatch):
    """默认不带水印（watermark_enabled=false）：用户明确要求关闭 AI 生成水印。"""
    captured = {}

    async def fake_post(*args, **_k):
        captured["payload"] = args[1]
        return {"id": "up-wm", "task_status": "PROCESSING"}

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))

    task = asyncio.run(svc.create_task("image", "cogview-3-flash", "x"))
    assert task["params"]["watermark"] is False
    assert captured["payload"]["watermark_enabled"] is False


def test_infinite_wait_never_times_out(svc, monkeypatch):
    """max_wait_seconds=0（不限等待）：不触发超时熔断，仍持续向上游查询。"""
    import json as _json

    async def fake_post(*_a, **_k):
        return {"id": "up-inf", "task_status": "PROCESSING"}

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))

    task = asyncio.run(svc.create_task(
        "video", "cogvideox-flash", "x", size="1920x1080", max_wait_seconds=0,
    ))
    assert task["params"]["max_wait"] == 0
    assert task["params"]["deadline_ts"] == 0
    assert task["remaining_seconds"] == -1, "-1 表示不限等待"

    # 把创建时间推到很早：普通任务早该超时，无限等待必须继续
    row = svc.db.query_one(
        "SELECT params_json FROM media_tasks WHERE id = ?", (task["id"],))
    p = _json.loads(row["params_json"])
    p["created_ts"] = 1
    svc.db.execute(
        "UPDATE media_tasks SET params_json = ? WHERE id = ?",
        (_json.dumps(p), task["id"]),
    )

    refreshed = []

    async def fake_refresh(tid):
        refreshed.append(tid)

    monkeypatch.setattr(MediaGenService, "_refresh", staticmethod(fake_refresh))

    t = asyncio.run(svc.get_task(task["id"], poll=True))
    assert t["status"] == "PROCESSING", "不限等待不得判超时"
    assert refreshed == [task["id"]], "仍应向上游查询"


def test_video_duration_defaults_and_rejects_invalid(svc, monkeypatch):
    """duration：cogvideox-flash 默认 5 秒（实测 10 被上游静默忽略），非法值必须拒绝。"""
    captured = {}

    async def fake_post(*args, **_k):
        captured["payload"] = args[1]
        return {"id": "up-dur", "task_status": "PROCESSING"}

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))

    task = asyncio.run(svc.create_task("video", "cogvideox-flash", "x"))
    assert task["params"]["duration"] == 5, "flash 实测仅 5 秒"
    assert captured["payload"]["duration"] == 5

    # 实测：传 10 或 30 上游均不报错、按 5 秒生成 → 必须在前置校验拦住，
    # 否则用户以为生成了 10 秒，实际只有 5 秒
    with pytest.raises(MediaGenError, match="仅支持"):
        asyncio.run(svc.create_task("video", "cogvideox-flash", "x", duration=10))
    with pytest.raises(MediaGenError, match="仅支持"):
        asyncio.run(svc.create_task("video", "cogvideox-flash", "x", duration=30))


def test_stop_task_cancels_active_and_disconnects_upstream(svc, monkeypatch):
    """用户停止：PROCESSING → CANCELLED，且此后 poll 不再查上游（主动断开）。"""
    async def fake_post(*_a, **_k):
        return {"id": "up-stop", "task_status": "PROCESSING"}

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))
    task = asyncio.run(svc.create_task("video", "cogvideox-flash", "x", size="720x480"))
    assert task["status"] == "PROCESSING"

    stopped = svc.stop_task(task["id"])
    assert stopped["status"] == "CANCELLED"
    assert "停止" in stopped["error"]

    upstream_calls = []

    async def fake_refresh(tid):
        upstream_calls.append(tid)

    monkeypatch.setattr(MediaGenService, "_refresh", staticmethod(fake_refresh))

    t = asyncio.run(svc.get_task(task["id"], poll=True))
    assert t["status"] == "CANCELLED", "停止后迟到的轮询不得改写终态"
    assert upstream_calls == [], "CANCELLED 后不得再向智谱查询"


def test_stop_task_is_noop_on_terminal_task(svc, monkeypatch):
    """已终态任务再停止：空操作，不覆盖既有失败原因（刷新后仍能看到真相）。"""
    async def fake_post(*_a, **_k):
        raise MediaGenError("上游错误: {'code': '1305', 'message': '访问量过大'}")

    monkeypatch.setattr(MediaGenService, "_post_with_retry", staticmethod(fake_post))
    task = asyncio.run(svc.create_task("video", "cogvideox-flash", "x", size="720x480"))
    assert task["status"] == "FAIL"

    stopped = svc.stop_task(task["id"])
    assert stopped["status"] == "FAIL"
    assert "1305" in stopped["error"], "不得覆盖既有失败原因"


def test_stop_unknown_task_raises(svc):
    with pytest.raises(MediaGenError):
        svc.stop_task("nope")


def test_stop_endpoint_returns_cancelled_task(monkeypatch):
    """REST 停止端点：替换服务工厂直接调函数（不碰真实 DB / 全局单例）。"""
    from harness.api.rest import media as media_route

    class _FakeSvc:
        def stop_task(self, task_id):
            return {"id": task_id, "status": "CANCELLED", "error": "用户已停止生成"}

    monkeypatch.setattr(media_route, "get_media_service", lambda: _FakeSvc())
    out = asyncio.run(media_route.stop_generation("deadbeef"))
    assert out["status"] == "CANCELLED"


def test_stop_endpoint_unknown_task_raises_404(monkeypatch):
    from harness.api.errors import APIError
    from harness.api.rest import media as media_route

    class _FakeSvc:
        def stop_task(self, task_id):
            raise MediaGenError(f"任务不存在: {task_id}")

    monkeypatch.setattr(media_route, "get_media_service", lambda: _FakeSvc())
    with pytest.raises(APIError) as e:
        asyncio.run(media_route.stop_generation("nope"))
    assert e.value.status_code == 404
