"""jev_manager 插件测试 —— 重点覆盖 SEC-26（决策调用审计留痕）。"""

from typing import Any

import pytest


class _Resp:
    """httpx 响应桩。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _Client:
    """httpx.AsyncClient 桩：记录请求、返回固定载荷。"""

    last: dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, headers: Any, json: Any) -> _Resp:
        _Client.last = {"url": url, "headers": headers, "json": json}
        return _Resp({"results": [{"type": "score", "score": 0.5}]})


@pytest.fixture
def manager(monkeypatch):
    """构造 JevManager，并把 httpx.AsyncClient 换成桩。"""
    import httpx

    from plugins.jev_manager.main import JevManager

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return JevManager(api_key="sk-secret-key-123", base_url="https://api.example/v1")


class TestJevAudit:
    """SEC-26：每次决策调用都要留下可追溯的审计日志。"""

    @pytest.mark.asyncio
    async def test_choice_call_is_audited(self, manager, caplog) -> None:
        with caplog.at_level("INFO", logger="harness.jev_manager"):
            results = await manager.decide(
                scene="客服工单分级",
                queries=[{"type": "choice", "question": "客户情绪?", "options": ["angry", "calm"]}],
            )
        assert results  # 正常返回
        audit_lines = [r for r in caplog.records if "jev.decide 审计" in r.getMessage()]
        assert audit_lines, "choice 决策未记录审计日志"
        msg = audit_lines[-1].getMessage()
        assert "客服工单分级" in msg
        assert "choice(options=" in msg  # 记录了选项，便于事后复核

    @pytest.mark.asyncio
    async def test_audit_never_leaks_api_key(self, manager, caplog) -> None:
        with caplog.at_level("INFO", logger="harness.jev_manager"):
            await manager.decide(
                scene="内容审核",
                queries=[{"type": "noul", "proposition": "这段文本是否违规"}],
            )
        blob = "\n".join(r.getMessage() + " " + str(r.args) for r in caplog.records)
        assert "sk-secret-key-123" not in blob, "审计日志泄露了 API Key"

    @pytest.mark.asyncio
    async def test_request_payload_unchanged(self, manager) -> None:
        """审计不改动发给 API 的实际 payload（纯旁路记录）。"""
        queries = [{"type": "score", "question": "质量", "scale": [{"min": 0, "max": 1}]}]
        await manager.decide(scene="评估", queries=queries)
        assert _Client.last["json"]["queries"] == queries
        assert _Client.last["json"]["scene"] == "评估"
        # 请求头仍带 Authorization（功能不变）
        assert _Client.last["headers"]["Authorization"].startswith("Bearer ")
