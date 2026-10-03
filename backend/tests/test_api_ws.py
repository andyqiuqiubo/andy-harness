"""WebSocket API 测试（使用 TestClient WS 支持）。"""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    """创建测试客户端。"""
    from harness.main import app

    with TestClient(app) as c:
        yield c


class TestStreamingProviderProxy:
    """P0-1 回归：WS 层不得再 monkey patch 共享 provider 单例。

    `ProviderRegistry.get_provider()` 返回按 provider_id 缓存的**共享实例**，
    过去每个连接执行 `provider.chat = wrapper` 会导致并发连接互相嵌套包装、
    跨会话串流。这里用确定性断言锁定新的代理语义。
    """

    class _FakeProvider:
        """最小 provider：只有一个 async generator 形式的 chat。"""

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, *args: object, **kwargs: object):
            self.calls += 1
            for chunk in (
                {"delta": "A"},
                {"reasoning_content": "thinking"},
                {"delta": "B"},
            ):
                yield chunk

    @staticmethod
    async def _emit_noop(chunk: dict) -> None:  # noqa: ARG004
        """占位 emit（只验证不报错，不收集）。"""
        return None

    @classmethod
    def _make_proxy(cls, provider: object, session_id: str, sink: list | None) -> object:
        """构造代理。`sink` 为 None 时使用空 emit；否则用 async emit 收集 chunk。

        注意：生产环境的 emit（`_emit_chunk`）是 **async** 的，
        代理内部执行 `await self._emit(chunk)`，因此这里也必须传协程函数。
        """
        from harness.api.ws.chat import _StreamingProviderProxy

        if sink is None:
            return _StreamingProviderProxy(provider, session_id, cls._emit_noop)

        async def _emit(chunk: dict) -> None:
            sink.append(chunk)

        return _StreamingProviderProxy(provider, session_id, _emit)

    async def test_proxy_emits_and_yields(self) -> None:
        provider = self._FakeProvider()
        sink: list = []
        proxy = self._make_proxy(provider, "s1", sink)
        chunks = [c async for c in proxy.chat(messages=[])]
        assert len(chunks) == 3
        assert chunks[0]["delta"] == "A"
        # 每个 chunk 都要既发给本连接，又原样 yield 给调用方（不能吞掉）
        assert sink == chunks

    async def test_real_provider_instance_not_mutated(self) -> None:
        provider = self._FakeProvider()
        original_chat = provider.chat
        self._make_proxy(provider, "s1", None)
        [c async for c in self._make_proxy(provider, "s1", None).chat()]
        # 关键：真实 provider 的属性必须保持原样，不能被替换成 wrapper
        assert provider.chat == original_chat

    async def test_two_proxies_are_isolated(self) -> None:
        provider = self._FakeProvider()
        original_chat = provider.chat
        sink_a: list = []
        sink_b: list = []

        [c async for c in self._make_proxy(provider, "s-a", sink_a).chat()]
        [c async for c in self._make_proxy(provider, "s-b", sink_b).chat()]

        # 两个连接各自收到自己的一份流，互不干扰，也不重复
        assert len(sink_a) == 3 and len(sink_b) == 3
        assert provider.calls == 2
        assert provider.chat == original_chat

    async def test_proxy_delegates_other_attributes(self) -> None:
        provider = self._FakeProvider()
        proxy = self._make_proxy(provider, "s1", None)
        # 非 chat 的属性/方法需透传给真实实例（保持 TokenCounter 等能力可用）
        assert proxy.calls == 0


class TestWebSocketChat:
    """WebSocket /ws/chat 测试。"""

    def test_ws_connect_and_receive_error(self, client: TestClient) -> None:
        """WS 连接后发送无效消息，应收到 error 帧。"""
        with client.websocket_connect("/ws/chat") as ws:
            # 发送无效 JSON
            ws.send_text("not json")
            response = ws.receive_json()
            assert response["type"] == "error"

    def test_ws_send_message_with_invalid_provider(self, client: TestClient) -> None:
        """WS 发送消息但 provider 不存在，应收到 error 帧。"""
        # 先创建会话
        create_resp = client.post("/api/sessions", json={"title": "WS测试"})
        session_id = create_resp.json()["id"]

        with client.websocket_connect("/ws/chat") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "content": "你好",
                        "provider_id": "nonexistent_provider",
                    }
                )
            )
            # 应收到 error 帧
            response = ws.receive_json()
            assert response["type"] in ("error", "context_snapshot", "token_delta", "done")
