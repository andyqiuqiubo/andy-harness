"""E3 · 结构化 compaction（compaction v2）测试。"""

from __future__ import annotations

from typing import Any

from harness.modules.context_manager.service import (
    StructuredCompactionStrategy,
)


def _call(cid: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "id": cid,
        "type": "tool_call",
        "function": {"name": name, "arguments": arguments},
    }


def make_messages() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "第一个问题"},
        {"role": "assistant", "content": "第一个回答"},
        {"role": "user", "content": "第二个问题"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_call("tc1", "calculator", '{"expression": "1+1"}')],
        },
        {"role": "tool", "content": "2", "tool_call_id": "tc1"},
        {"role": "assistant", "content": "结果是2"},
        {"role": "user", "content": "第三个问题"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_call("tc2", "web_search", '{"query": "天气"}')],
        },
        {"role": "tool", "content": "晴天", "tool_call_id": "tc2"},
        {"role": "assistant", "content": "今天晴天"},
    ]


def orphan_tool_messages(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """找出没有对应 assistant tool_calls 的孤立 tool 消息。"""
    valid: set[str] = set()
    orphans: list[dict[str, Any]] = []
    for m in msgs:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    valid.add(tc["id"])
        if m.get("role") == "tool" and m.get("tool_call_id") not in valid:
            orphans.append(m)
    return orphans


class TestStructuredCompaction:
    def test_short_conversation_unchanged(self) -> None:
        msgs = make_messages()[:4]
        out = StructuredCompactionStrategy(keep_recent_messages=8).apply(msgs, None, "m", 4096, set())
        assert out == msgs

    def test_recent_kept_verbatim(self) -> None:
        msgs = make_messages()
        out = StructuredCompactionStrategy(keep_recent_messages=4).apply(msgs, None, "m", 4096, set())
        # 最近 4 条（下标 6..9）原样保留
        assert out[-4:] == msgs[6:]

    def test_old_folded_into_event_log(self) -> None:
        msgs = make_messages()
        out = StructuredCompactionStrategy(keep_recent_messages=4).apply(msgs, None, "m", 4096, set())
        log = next(m for m in out if m["role"] == "system")
        body = log["content"]
        assert "结构化历史记录" in body
        assert "第一个问题" in body
        assert "第一个回答" in body
        assert "calculator" in body
        assert "→ 2" in body

    def test_single_log_before_recent(self) -> None:
        msgs = make_messages()
        out = StructuredCompactionStrategy(keep_recent_messages=4).apply(msgs, None, "m", 4096, set())
        system_idx = [i for i, m in enumerate(out) if m["role"] == "system"]
        assert len(system_idx) == 1
        assert system_idx[0] == len(out) - 4 - 1

    def test_no_orphan_tool_messages(self) -> None:
        # 让最近边界落在工具组附近，验证不切断。
        for keep in (3, 4, 5):
            msgs = make_messages()
            out = StructuredCompactionStrategy(keep_recent_messages=keep).apply(msgs, None, "m", 4096, set())
            assert orphan_tool_messages(out) == []

    def test_pinned_old_message_kept(self) -> None:
        msgs = make_messages()
        out = StructuredCompactionStrategy(keep_recent_messages=4).apply(msgs, None, "m", 4096, pinned_indices={2})
        # 第二个问题被钉住，作为实际消息出现，不在事件日志正文。
        pinned = [m for m in out if m.get("content") == "第二个问题"]
        assert len(pinned) == 1 and pinned[0]["role"] == "user"
        log = next(m for m in out if m["role"] == "system")["content"]
        assert "第二个问题" not in log

    def test_long_content_truncated(self) -> None:
        msgs = [
            {"role": "user", "content": "x" * 500},
            {"role": "assistant", "content": "y" * 500},
            {"role": "user", "content": "近期"},
            {"role": "assistant", "content": "近期回答"},
        ]
        out = StructuredCompactionStrategy(keep_recent_messages=2, max_content_chars=50).apply(
            msgs, None, "m", 4096, set()
        )
        log = next(m for m in out if m["role"] == "system")["content"]
        assert "…" in log

    def test_none_content_does_not_crash(self) -> None:
        msgs = [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": None},
            {"role": "tool", "content": None, "tool_call_id": "x"},
            {"role": "user", "content": "近期"},
            {"role": "assistant", "content": "近期回答"},
        ]
        out = StructuredCompactionStrategy(keep_recent_messages=2).apply(msgs, None, "m", 4096, set())
        log = next(m for m in out if m["role"] == "system")["content"]
        assert "(无输出)" in log
