"""SEC-17：provider 流式重试的**增量游标**专项测试。

背景：流中途失败（读超时 / 连接被重置）后 ``_stream_with_retry`` 会整条请求重发。
修复前没有任何游标，已推给前端的正文与思维链被再次全量发送——用户看到重复文本，
且实时展示与落库记录错位（重复内容不落库）。

本文件用脚本化的假 SSE 流模拟「首段产出 → 中途失败 → 重试续传」，断言调用方
收到的内容**不重复且不丢字**。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx
import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from harness.modules.model_manager import openai_compatible as oc  # noqa: E402


# ── 假 SSE 流 ────────────────────────────────────────
def sse(
    content: str | None = None,
    *,
    reasoning: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
) -> str:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    obj: dict[str, Any] = {"choices": [{"delta": delta}]}
    if usage:
        obj["usage"] = usage
    return "data: " + json.dumps(obj, ensure_ascii=False)


DONE = "data: [DONE]"


class _Resp:
    """一次 HTTP 流式响应：按脚本吐行，吐完后可选抛传输错误。"""

    def __init__(self, lines: list[str], error: BaseException | None = None) -> None:
        self._lines = lines
        self._error = error
        self.status_code = 200

    async def aread(self) -> bytes:
        return b""

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def aiter_lines(self):  # type: ignore[no-untyped-def]
        for line in self._lines:
            yield line
        if self._error is not None:
            raise self._error


class _Client:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    def stream(self, *a: Any, **kw: Any) -> _Resp:
        return self._resp


def _scripted(scripts: list[tuple[list[str], BaseException | None]], monkeypatch) -> None:
    """把每次 httpx.AsyncClient() 依次绑到脚本里的下一段响应。"""
    remaining = list(scripts)

    def factory(*a: Any, **kw: Any) -> _Client:
        lines, error = remaining.pop(0)
        return _Client(_Resp(lines, error))

    monkeypatch.setattr(oc.httpx, "AsyncClient", factory)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """跳过指数退避的真实等待，保持测试快速。"""
    real_sleep = asyncio.sleep

    async def fast_sleep(delay: float, *a: Any, **kw: Any) -> None:
        if delay >= 1:  # 退避等待：直接跳过
            return
        await real_sleep(delay, *a, **kw)

    monkeypatch.setattr(oc.asyncio, "sleep", fast_sleep)


def _provider() -> oc.OpenAICompatibleProvider:
    return oc.OpenAICompatibleProvider(api_key="k", base_url="https://example.test/v1")


async def _collect_text(scripts, monkeypatch) -> str:
    _scripted(scripts, monkeypatch)
    parts: list[str] = []
    async for chunk in _provider()._stream_with_retry("https://x/chat", {}, max_retries=3):
        if chunk.get("delta"):
            parts.append(chunk["delta"])
    return "".join(parts)


# ── 用例 ────────────────────────────────────────────
async def test_retry_resumes_without_duplicating_text(monkeypatch):
    """首段产出后中途断流：重试只续传增量，前端不得看到重复文本。"""
    text = await _collect_text(
        [
            ([sse("你好"), sse("，世界")], httpx.ReadError("连接被重置")),
            ([sse("你好"), sse("，世界"), sse("！"), DONE], None),
        ],
        monkeypatch,
    )
    assert text == "你好，世界！", f"重试产生了重复/丢字：{text!r}"


async def test_no_retry_leaves_stream_untouched(monkeypatch):
    """无重试的正常路径必须逐字透传（不能被游标误裁剪）。"""
    text = await _collect_text(
        [([sse("a"), sse("b"), sse("c"), DONE], None)],
        monkeypatch,
    )
    assert text == "abc"


async def test_mid_stream_read_error_is_retried(monkeypatch):
    """流中途的 ReadError/RemoteProtocolError 必须被捕获并重试（原实现漏掉）。

    修复前只捕获 TimeoutException / ConnectError，连接被重置会直接抛出，
    既不重试也不上报，前端表现为「回复静默截断」。
    """
    text = await _collect_text(
        [
            ([sse("前半段")], httpx.RemoteProtocolError("peer closed")),
            ([sse("前半段"), sse("后半段"), DONE], None),
        ],
        monkeypatch,
    )
    assert text == "前半段后半段"


async def test_tool_calls_not_duplicated_on_retry(monkeypatch):
    """工具调用片段同样去重：重试不得把同一批 tool_calls 再推一遍。"""
    call = [{"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{}"}}]
    _scripted(
        [
            ([sse("思考"), sse(tool_calls=call)], httpx.ReadError("boom")),
            ([sse("思考"), sse(tool_calls=call), sse("结束"), DONE], None),
        ],
        monkeypatch,
    )
    got_calls: list[dict[str, Any]] = []
    texts: list[str] = []
    async for chunk in _provider()._stream_with_retry("https://x/chat", {}, max_retries=3):
        if chunk.get("tool_calls"):
            got_calls.extend(chunk["tool_calls"])
        if chunk.get("delta"):
            texts.append(chunk["delta"])
    assert got_calls == call, f"tool_calls 被重复推送: {got_calls}"
    assert "".join(texts) == "思考结束"


async def test_reasoning_content_not_duplicated_on_retry(monkeypatch):
    """思维链（reasoning_content）同样走增量游标。"""
    _scripted(
        [
            ([sse(reasoning="让我想想")], httpx.ReadError("boom")),
            ([sse(reasoning="让我想想"), sse(reasoning="…好了"), DONE], None),
        ],
        monkeypatch,
    )
    parts: list[str] = []
    async for chunk in _provider()._stream_with_retry("https://x/chat", {}, max_retries=3):
        if chunk.get("reasoning_content"):
            parts.append(chunk["reasoning_content"])
    assert "".join(parts) == "让我想想…好了"


async def test_drifted_retry_does_not_lose_characters(monkeypatch):
    """重试内容与上次漂移时：放弃裁剪、整体重发，保证不丢字（可重复不可缺字）。"""
    text = await _collect_text(
        [
            ([sse("Hello")], httpx.ReadError("boom")),
            ([sse("Hi there"), DONE], None),
        ],
        monkeypatch,
    )
    # 'H' 与已产出前缀一致被跳过，从第 2 个字符起重发："Hello" + "i there"
    assert text == "Helloi there", f"漂移重试处理异常：{text!r}"


# ── _ResumeCursor 单元行为 ────────────────────────────
def test_resume_cursor_filters_prefix_then_passes_through():
    cur = oc._ResumeCursor()
    for piece in ("你", "好"):
        cur.record(piece)
    assert cur.pending == 2

    cur.begin_attempt()
    assert cur.filter("你") == ""  # 已产出，跳过
    assert cur.filter("好") == ""
    assert cur.filter("世") == "世"  # 前缀已消耗完，转透传
    assert cur.filter("界") == "界"


def test_resume_cursor_overflow_disables_filtering():
    cur = oc._ResumeCursor()
    cur.MAX_PREFIX = 5  # type: ignore[misc]
    cur.record("12345")
    cur.record("6")  # 超限，游标作废
    assert cur.pending == 0
    cur.begin_attempt()
    assert cur.filter("abc") == "abc"


def test_resume_cursor_second_attempt_uses_full_history():
    """第二次重试必须跳过**历次累计**的产出，而不只是上一轮的。"""
    cur = oc._ResumeCursor()
    cur.record("abc")
    cur.begin_attempt()
    assert cur.filter("abc") == ""
    cur.record("de")  # 续传成功，已产出变为 abcde
    assert cur.pending == 5

    cur.begin_attempt()
    assert cur.filter("ab") == ""
    assert cur.filter("cde") == ""
    assert cur.filter("f") == "f"
