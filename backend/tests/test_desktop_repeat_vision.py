"""视觉辅助（百炼 VL）单元测试——全部走假件/monkeypatch，绝不真调外网。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from harness.modules.desktop_repeat.config import RepeatConfig
from harness.modules.desktop_repeat.vision import client as vcli
from harness.modules.desktop_repeat.vision.analyzer import analyze_session, build_prompt
from harness.modules.desktop_repeat.vision.client import (
    BailianVisionClient,
    image_data_url,
    parse_json_answer,
    resolve_api_key,
)


# ── 客户端 ───────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None,
                 text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """捕获请求的 httpx.AsyncClient 替身；类属性可编程响应。"""

    calls: list[tuple[str, dict, dict]] = []
    status_code: int = 200
    content: str = "ok"

    def __init__(self, timeout: Any = None) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def post(self, url: str, json: dict | None = None,
                   headers: dict | None = None) -> _Resp:
        type(self).calls.append((url, json or {}, headers or {}))
        if type(self).status_code == 200:
            return _Resp(200, {"choices": [{"message": {"content": type(self).content}}],
                               "usage": {"total_tokens": 7}})
        return _Resp(type(self).status_code, text="boom")


@pytest.fixture()
def fake_http(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAsyncClient]:
    monkeypatch.setattr(vcli.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.status_code = 200
    _FakeAsyncClient.content = "ok"
    return _FakeAsyncClient


def test_client_request_shape_and_parse(tmp_path: Path, fake_http,
                                        monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    img = tmp_path / "f.jpg"
    img.write_bytes(b"\xff\xd8fakejpeg")
    cli = BailianVisionClient(api_key="sk-test" + "x" * 28, model="qwen3-vl-plus")
    r = asyncio.run(cli.analyze_images([img], "描述"))
    assert r is not None and r["text"] == "ok" and r["usage"]["total_tokens"] == 7
    url, body, headers = fake_http.calls[-1]
    assert url.endswith("/compatible-mode/v1/chat/completions")
    assert headers["Authorization"].startswith("Bearer sk-test")
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert content[1] == {"type": "text", "text": "描述"}
    assert body["model"] == "qwen3-vl-plus"


def test_client_retries_twice_then_gives_up(tmp_path: Path, fake_http):
    fake_http.status_code = 500
    img = tmp_path / "f.png"
    img.write_bytes(b"\x89PNGfake")
    cli = BailianVisionClient(api_key="sk-test" + "x" * 28)
    assert asyncio.run(cli.analyze_images([img], "x")) is None
    assert len(fake_http.calls) == 2, "应恰好重试一次（共 2 次调用）"


def test_client_skips_when_no_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    cli = BailianVisionClient(api_key="", key_file=str(
        Path(__file__).parent / "no-such-key-file.txt"))
    assert cli.available is False
    assert asyncio.run(cli.analyze_images([], "x")) is None


def test_resolve_api_key_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    kf = tmp_path / "key.txt"
    kf.write_text("阿里百炼key：sk-filekey000000000000000000000011\n", encoding="utf-8")
    # env 优先
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-envkey0000000000000000000000022")
    assert resolve_api_key("", str(kf)).startswith("sk-envkey")
    # 无 env 时读指定文件
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    assert resolve_api_key("", str(kf)).startswith("sk-filekey")
    # 文件不含 sk- 时返回空
    kf2 = tmp_path / "empty.txt"
    kf2.write_text("no key here", encoding="utf-8")
    assert resolve_api_key("", str(kf2)) == ""


def test_image_data_url_rejects_missing(tmp_path: Path):
    assert image_data_url(tmp_path / "nope.jpg") is None
    p = tmp_path / "empty.jpg"
    p.write_bytes(b"")
    assert image_data_url(p) is None


# ── 回答解析 ─────────────────────────────────────────────────────
def test_parse_json_answer_variants():
    fenced = '前缀\n```json\n{"summary":"a","effective":true}\n```\n后缀'
    assert parse_json_answer(fenced)["summary"] == "a"
    assert parse_json_answer('{"screen_changed": false}')["screen_changed"] is False
    assert parse_json_answer("完全不是 JSON") is None
    assert parse_json_answer("") is None


# ── 会话逐步分析（假 client，不触网） ────────────────────────────
class _FakeVision:
    model = "fake-vl"

    def __init__(self, answers: list[str | None]) -> None:
        self.answers = list(answers)

    async def analyze_images(self, images, prompt, system=None):
        return None if not self.answers else \
            {"text": self.answers.pop(0) or "", "usage": {}}


def _bundle_with_frames(tmp_path: Path) -> Path:
    frames = tmp_path / "frames"
    frames.mkdir()
    for name in ("000001-pre.jpg", "000001-post.jpg", "000002-pre.jpg"):
        (frames / name).write_bytes(b"\xff\xd8x")
    return tmp_path


def test_analyze_session_summarizes_and_flags_suspects(tmp_path: Path):
    bundle = _bundle_with_frames(tmp_path)
    steps = [
        {"sid": "s01", "action": "click", "label": "点击",
         "evidence": {"frames": ["frames/000001-pre.jpg", "frames/000001-post.jpg"]},
         "target": {"locators": [{"by": "name_type", "value": "保存"}]}},
        {"sid": "s02", "action": "type", "label": "输入",
         "evidence": {"frames": ["frames/000002-pre.jpg"]},  # 只有 pre → 跳过
         "target": {}},
    ]
    fake = _FakeVision([json.dumps({
        "summary": "点击了保存按钮", "screen_changed": True,
        "effective": True, "issues": []})])
    r = asyncio.run(analyze_session(fake, bundle, steps, max_steps=10))
    assert r["analyzed"] == 1 and r["failed"] == 0
    assert r["skipped_no_frames"] == 1, "无后帧的步骤应跳过（省 token）"
    assert r["steps"][0]["summary"] == "点击了保存按钮"
    assert r["suspects"] == []
    assert "s01" in build_prompt(steps[0])


def test_analyze_session_counts_failures_and_suspects(tmp_path: Path):
    bundle = _bundle_with_frames(tmp_path)
    steps = [{"sid": "s01", "action": "click", "label": "",
              "evidence": {"frames": ["frames/000001-pre.jpg", "frames/000001-post.jpg"]},
              "target": {}}]
    fake = _FakeVision([json.dumps({
        "summary": "点了但没反应", "screen_changed": False,
        "effective": False, "issues": ["画面无变化"]})])
    r = asyncio.run(analyze_session(fake, bundle, steps, max_steps=10))
    assert r["suspects"] == ["s01"], "effective=false 必须进疑似清单"
    fake2 = _FakeVision([None])
    r2 = asyncio.run(analyze_session(fake2, bundle, steps, max_steps=10))
    # _FakeVision(None) 返回空文本 = 调用成功但不可解析，同样计入 failed
    assert r2["failed"] == 1 and r2["steps"][0]["issues"] == ["视觉分析返回不可解析"]


# ── 配置 ─────────────────────────────────────────────────────────
def test_vision_config_defaults_and_overrides():
    c = RepeatConfig.load({})
    assert c.vision_enabled is False and c.vision_model == "qwen3-vl-plus"
    assert c.vision_max_steps == 24 and c.window_appear_wait_ms == 2500
    c2 = RepeatConfig.load({"repeat.vision_enabled": True,
                            "repeat.vision_model": "qwen3-vl-max",
                            "repeat.vision_max_steps": 5,
                            "repeat.vision_timeout_s": 999})
    assert c2.vision_enabled is True and c2.vision_model == "qwen3-vl-max"
    assert c2.vision_max_steps == 5 and c2.vision_timeout_s == 300, "超时应被钳制"


# ── REST ─────────────────────────────────────────────────────────
def _make_svc(tmp_path: Path) -> Any:
    from harness.infra.database import Database
    from harness.modules.desktop_repeat.service import DesktopRepeatService

    return DesktopRepeatService(Database(db_path=str(tmp_path / "v.db")),
                                tmp_path / "sessions", RepeatConfig.load())


def test_vision_status_roundtrip(tmp_path: Path):
    svc = _make_svc(tmp_path)
    assert svc.vision_status("r-x")["available"] is False
    (tmp_path / "sessions" / "r-x").mkdir(parents=True)
    (tmp_path / "sessions" / "r-x" / "vision.json").write_text(
        json.dumps({"analyzed": 3, "suspects": ["s09"]}), encoding="utf-8")
    st = svc.vision_status("r-x")
    assert st["available"] is True and st["analyzed"] == 3 and st["suspects"] == ["s09"]


def test_vision_persist_writes_bundle_and_summary(tmp_path: Path, monkeypatch):
    """vision_analyze 的落盘行为：vision.json + 会话 doc.vision 摘要。"""
    svc = _make_svc(tmp_path)
    sid = "r-v1"
    sdir = tmp_path / "sessions" / sid
    (sdir / "frames").mkdir(parents=True)
    for n in ("000001-pre.jpg", "000001-post.jpg"):
        (sdir / "frames" / n).write_bytes(b"\xff\xd8x")
    (sdir / "session.json").write_text(json.dumps(
        {"schema": 1, "session_id": sid, "status": "ready", "label": "",
         "counters": {}, "integrity": {}, "error": None}), encoding="utf-8")

    from harness.modules.desktop_repeat.models import CompileResult, StepDef

    result = CompileResult(session_id=sid,
                           steps=[StepDef(sid="s01", action="click", label="x")],
                           warnings=[])
    monkeypatch.setattr(
        "harness.modules.desktop_repeat.vision.analyzer.analyze_session",
        _fake_analyze)
    summary = asyncio.run(svc.vision_analyze(sid, result))
    assert summary["analyzed"] == 1
    saved = json.loads((sdir / "vision.json").read_text(encoding="utf-8"))
    assert saved["analyzed"] == 1 and saved["suspects"] == ["s01"]
    doc = svc.get_session(sid)
    assert doc.get("vision", {}).get("suspects") == ["s01"], "会话摘要应回写 DB"


async def _fake_analyze(client, bundle_dir, steps, max_steps=24):
    return {"generated_at": "t", "model": client.model, "analyzed": len(steps),
            "skipped_no_frames": 0, "failed": 0,
            "steps": [{"sid": s.get("sid"), "summary": "", "screen_changed": None,
                       "effective": False, "issues": [], "raw": ""} for s in steps],
            "suspects": [s.get("sid") for s in steps]}
