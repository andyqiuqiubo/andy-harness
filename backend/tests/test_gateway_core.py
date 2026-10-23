"""gateway 核心测试：路由、鉴权、对话（MockTransport）、/v1 鉴权。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from harness.kernel.services import ServiceRegistry
from harness.modules.gateway.api.openai_compat import router as v1_router
from harness.modules.gateway.api.openai_compat import setup_gateway_v1_routes
from harness.modules.gateway.config import (
    ChannelConfig,
    ConfigError,
    ConfigService,
    ConfigSnapshot,
    Defaults,
)
from harness.modules.gateway.contracts import GatewayError
from harness.modules.gateway.router import HealthTracker, Router
from harness.modules.gateway.service import GatewayService


class FakeDB:
    """最小 sqlite 内存库，满足 gateway 的 execute/query/query_one 协议。"""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()


def _snapshot(channels: list[ChannelConfig], aliases: dict[str, list[str]] | None = None) -> ConfigSnapshot:
    return ConfigSnapshot(
        version=1,
        checksum="x",
        channels={c.id: c for c in channels},
        aliases=aliases or {},
        defaults=Defaults(),
        tenants=[],
        raw={},
    )


def _channel(cid: str, models: list[str], **kw: Any) -> ChannelConfig:
    return ChannelConfig(
        id=cid,
        base_url=f"https://{cid}.local/v1",
        auth="bearer",
        models=models,
        credentials=[{"id": "c1", "source": "env:MOCK_KEY"}],
        **kw,
    )


def test_router_alias_order_and_health() -> None:
    a = _channel("a", ["m1"], weight=3)
    b = _channel("b", ["m1"], weight=1)
    snap = _snapshot([a, b], {"free/auto": ["a/m1", "b/m1"]})
    health = HealthTracker()
    router = Router(health)

    cands = router.candidates(snap, "free/auto")
    assert [c.channel.id for c in cands] == ["a", "b"]

    health.mark_failure("a", 30)
    cands = router.candidates(snap, "free/auto")
    assert [c.channel.id for c in cands] == ["b"]


def test_router_tenant_filter() -> None:
    a = _channel("a", ["m1"])
    b = _channel("b", ["m2"])
    snap = _snapshot([a, b])
    router = Router(HealthTracker())

    assert router.candidates(snap, "b/m2", allowed_channels=["a"]) == []
    assert [c.channel.id for c in router.candidates(snap, "b/m2", allowed_models=["m2"])] == ["b"]

    # 同一通道多模型：只允许其中一个时，不应整条通道被过滤
    multi = _channel("multi", ["m1", "m2"])
    multi_snap = _snapshot([multi])
    assert [c.channel.id for c in router.candidates(multi_snap, "multi/m2", allowed_models=["m2"])] == ["multi"]

    # 别名按成员做租户过滤
    alias_snap = _snapshot([a, b], {"free/auto": ["a/m1", "b/m2"]})
    assert [c.channel.id for c in router.candidates(alias_snap, "free/auto", allowed_models=["m2"])] == ["b"]


def _write_config(path: Path, channels: list[dict[str, Any]] | None = None) -> None:
    path.write_text(
        json.dumps({"version": 1, "channels": channels if channels is not None else []}), encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_auth_issue_verify_revoke(tmp_path: Path) -> None:
    db = FakeDB()
    cfg = tmp_path / "gateway.json"
    _write_config(cfg)
    svc = GatewayService(db, config_path=cfg, bootstrap_key="agw-test", watch_config=False)
    await svc.start()
    try:
        ctx = svc.auth.verify("agw-test")
        assert ctx is not None
        assert ctx.tenant_id == "default"

        assert svc.auth.verify("agw-wrong") is None
        issued = svc.auth.issue_key("default", "second", ["chat"], 10, 1000)
        assert svc.auth.verify(issued.token) is not None
        rotated = svc.auth.rotate(issued.key_id)
        assert rotated is not None
        assert svc.auth.verify(rotated.token) is not None
        assert svc.auth.verify(issued.token) is not None  # 旧 Key 仍在 24h 宽限期
        assert svc.auth.revoke(issued.key_id) is True
        assert svc.auth.verify(issued.token) is None
        assert svc.auth.verify(rotated.token) is not None
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_chat_non_stream_and_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = {
        "version": 1,
        "channels": [
            {
                "id": "mock",
                "base_url": "https://mock.local/v1",
                "auth": "bearer",
                "models": ["m1"],
                "credentials": [{"id": "c1", "source": "env:MOCK_KEY"}],
            }
        ],
    }
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")

    sse = (
        'data: {"id":"x","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}\n'
        'data: {"id":"x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n'
        "data: [DONE]\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())

    db = FakeDB()
    svc = GatewayService(
        db,
        config_path=path,
        transport=httpx.MockTransport(handler),
        bootstrap_key="agw-test",
        watch_config=False,
    )
    await svc.start()
    try:
        ctx = svc.auth.verify("agw-test")
        assert ctx is not None
        result = await svc.chat(
            {"model": "mock/m1", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            ctx,
            "trace-1",
        )
        assert result.stream is False
        assert result.payload is not None
        assert result.payload["choices"][0]["message"]["content"] == "Hello"
        assert result.payload["usage"]["total_tokens"] == 4
        await svc.usage.stop()
        rows = svc.store.query_usage("default")
        assert rows and rows[0]["trace_id"] == "trace-1"
        assert rows[0]["total_tokens"] == 4
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_chat_failover_on_429(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = {
        "version": 1,
        "channels": [
            {
                "id": "a",
                "base_url": "https://a.local/v1",
                "auth": "bearer",
                "models": ["m1"],
                "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                "weight": 5,
            },
            {
                "id": "b",
                "base_url": "https://b.local/v1",
                "auth": "bearer",
                "models": ["m1"],
                "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                "weight": 1,
            },
        ],
        "group_aliases": {"free/auto": ["a/m1", "b/m1"]},
    }
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.host))
        if request.url.host == "a.local":
            return httpx.Response(429, content=b'{"error":"rate limit"}')
        sse = (
            'data: {"id":"y","choices":[{"index":0,"delta":{"content":"OK"},"finish_reason":null}]}\n'
            'data: {"id":"y","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n'
            "data: [DONE]\n"
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())

    db = FakeDB()
    svc = GatewayService(
        db,
        config_path=path,
        transport=httpx.MockTransport(handler),
        bootstrap_key="agw-test",
        watch_config=False,
    )
    await svc.start()
    try:
        ctx = svc.auth.verify("agw-test")
        assert ctx is not None
        result = await svc.chat(
            {"model": "free/auto", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            ctx,
        )
        assert result.payload is not None
        assert result.payload["choices"][0]["message"]["content"] == "OK"
        assert calls == ["a.local", "b.local"]
        # 双粒度熔断：429 只锁 (a, m1)，通道级仍放行（同通道其他模型可用）
        assert svc.health.available("a", "m1") is False
        assert svc.health.available("a") is True
        text = svc.metrics_text()
        assert "agw_failovers_total" in text
        assert "agw_requests_total" in text
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_v1_requires_key(tmp_path: Path) -> None:
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps({"version": 1, "channels": []}), encoding="utf-8")
    db = FakeDB()
    svc = GatewayService(db, config_path=path, bootstrap_key="agw-test", watch_config=False)
    await svc.start()
    try:
        registry = ServiceRegistry()
        registry.register(GatewayService, svc, owner="gateway")
        setup_gateway_v1_routes(registry)
        app = FastAPI()
        app.include_router(v1_router)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/v1/models")
            assert r.status_code == 401
            r = await client.get("/v1/models", headers={"Authorization": "Bearer agw-test"})
            assert r.status_code == 200
            assert r.json()["object"] == "list"
    finally:
        await svc.stop()


def test_config_rejects_private_literal(tmp_path: Path) -> None:
    path = tmp_path / "gateway.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "local",
                        "base_url": "http://127.0.0.1:11434/v1",
                        "auth": "bearer",
                        "models": ["m"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        ConfigService(path).load()

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["channels"][0]["allow_private"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert ConfigService(path).load().channels["local"].allow_private is True


@pytest.mark.asyncio
async def test_issue_key_requires_existing_tenant(tmp_path: Path) -> None:
    db = FakeDB()
    cfg = tmp_path / "gateway.json"
    _write_config(cfg)
    svc = GatewayService(db, config_path=cfg, bootstrap_key="agw-test", watch_config=False)
    await svc.start()
    try:
        with pytest.raises(ValueError):
            svc.auth.issue_key("no-such-tenant", "x", ["chat"])
        svc.auth.create_tenant("t1", "T1", ["m2"])
        issued = svc.auth.issue_key("t1", "ok", ["chat"])
        assert svc.auth.verify(issued.token) is not None
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_models_alias_filtered_by_tenant(tmp_path: Path) -> None:
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "a",
                        "base_url": "https://a.local/v1",
                        "auth": "bearer",
                        "models": ["m1"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    },
                    {
                        "id": "b",
                        "base_url": "https://b.local/v1",
                        "auth": "bearer",
                        "models": ["m2"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    },
                ],
                "group_aliases": {"free/auto": ["a/m1", "b/m2"]},
            }
        ),
        encoding="utf-8",
    )
    db = FakeDB()
    svc = GatewayService(db, config_path=cfg, bootstrap_key="agw-test", watch_config=False)
    await svc.start()
    try:
        svc.auth.create_tenant("t1", "T1", ["m2"])
        issued = svc.auth.issue_key("t1", "k", ["chat", "models"])
        ctx = svc.auth.verify(issued.token)
        assert ctx is not None
        ids = {m["id"] for m in svc.models(ctx)}
        assert "b/m2" in ids
        assert "a/m1" not in ids
        # 别名语义：只要有一个成员对租户可见就列出；路由阶段仍按成员过滤
        assert "free/auto" in ids
        assert [c.channel.id for c in svc.router.candidates(svc.snapshot, "free/auto", allowed_models=["m2"])] == ["b"]
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_apply_config_validates_before_write(tmp_path: Path) -> None:
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "a",
                        "base_url": "https://a.local/v1",
                        "auth": "bearer",
                        "models": ["m1"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    db = FakeDB()
    svc = GatewayService(db, config_path=cfg, bootstrap_key="agw-test", watch_config=False)
    await svc.start()
    try:
        before = cfg.read_text(encoding="utf-8")
        payload = json.loads(before)
        payload["channels"].append(dict(payload["channels"][0], id="a"))  # 重复 id
        with pytest.raises(ConfigError):
            svc.apply_config(payload)
        assert cfg.read_text(encoding="utf-8") == before  # 非法配置不得落盘

        payload = json.loads(before)
        payload["channels"][0]["weight"] = 7
        result = svc.apply_config(payload, actor="tester")
        assert result["version"] >= 1
        assert svc.snapshot.channels["a"].weight == 7
        # 回滚到首版
        first = svc.store.get_config_version(1)
        assert first is not None
        svc.apply_config(first, actor="tester", source="rollback")
        assert svc.snapshot.channels["a"].weight == 1
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_stream_passthrough_keepalive_and_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "mock",
                        "base_url": "https://mock.local/v1",
                        "auth": "bearer",
                        "models": ["m1"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    db = FakeDB()
    svc = GatewayService(db, config_path=cfg, bootstrap_key="agw-test", watch_config=False)
    await svc.start()
    try:
        ctx = svc.auth.verify("agw-test")
        assert ctx is not None

        class _SlowResponse:
            def __init__(self) -> None:
                self.closed = False

            def aiter_lines(self):  # noqa: ANN202
                async def gen():
                    await asyncio.sleep(0.2)  # 超过被压小后的 keepalive 阈值
                    yield 'data: {"choices":[{"delta":{"content":"Hi"}}]}'
                    yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"total_tokens":4}}'
                    yield "data: [DONE]"

                return gen()

            async def aclose(self) -> None:
                self.closed = True

        import harness.modules.gateway.service as service_module

        monkeypatch.setattr(service_module, "KEEPALIVE_SECONDS", 0.05)
        resp = _SlowResponse()
        chunks: list[bytes] = []
        async for chunk in svc._stream(resp, ctx, "mock", "c", "m1", "t", time.perf_counter(), 1):
            chunks.append(chunk)
        blob = b"".join(chunks)
        assert b": keepalive" in blob
        assert b"data: [DONE]" in blob
        assert resp.closed is True
        await svc.usage.stop()
        rows = svc.store.query_usage("default")
        assert rows and rows[0]["status"] == 200 and rows[0]["total_tokens"] == 4
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_v1_rejects_missing_messages(tmp_path: Path) -> None:
    cfg = tmp_path / "gateway.json"
    _write_config(cfg)
    db = FakeDB()
    svc = GatewayService(db, config_path=cfg, bootstrap_key="agw-test", watch_config=False)
    await svc.start()
    try:
        registry = ServiceRegistry()
        registry.register(GatewayService, svc, owner="gateway")
        setup_gateway_v1_routes(registry)
        app = FastAPI()
        app.include_router(v1_router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer agw-test"},
                json={"model": "mock/m1"},
            )
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "GW_INVALID_REQUEST"
    finally:
        await svc.stop()


def test_cline_credential_missing_file(tmp_path: Path) -> None:
    from harness.modules.gateway.credentials import ClineTokenManager

    manager = ClineTokenManager()
    manager.path = tmp_path / "missing.json"
    with pytest.raises(GatewayError):
        manager.token()


# ---------------------------------------------------------------------------
# P0-1 失败分类与冷却（classify.py / HealthTracker 双粒度）
# ---------------------------------------------------------------------------


def test_cooldown_remaining_zero_when_uncooled() -> None:
    """回归：未冷却的键必须报 0，而不是 0.0-now 的巨大负数（真机曾把 -1.79e9 显示给前端）。"""
    from harness.modules.gateway.router import HealthTracker

    health = HealthTracker()
    assert health.cooldown_remaining("a") == 0
    assert health.cooldown_remaining("a", "m1") == 0

    health.mark_failure("a", 600, model="m1")
    assert health.cooldown_remaining("a", "m1") > 500
    assert health.cooldown_remaining("a", "m2") == 0  # 只锁了 m1
    assert health.cooldown_remaining("a") == 0  # 通道级未锁


def test_classify_text_rules_beat_status() -> None:
    from harness.modules.gateway.classify import (
        COOLDOWN_LONG_MS,
        COOLDOWN_SHORT_MS,
        KIND_AUTH,
        KIND_INVALID_REQUEST,
        KIND_OVERLOADED,
        KIND_RATE_LIMIT,
        SCOPE_ACCOUNT,
        SCOPE_PROVIDER,
        SCOPE_REQUEST,
        classify_failure,
    )

    # 文本规则优先于状态码：一条 400 说 rate limit 就是限流
    v = classify_failure(400, b'{"error":{"message":"Rate limit exceeded for account"}}')
    assert v.kind == KIND_RATE_LIMIT
    assert v.scope == SCOPE_ACCOUNT
    assert v.backoff is True

    # 一条 429 说 no credentials 就是凭据没了（2min 账号级，不退避）
    v = classify_failure(429, b'{"error":{"message":"no credentials available"}}')
    assert v.kind == KIND_AUTH
    assert v.cooldown_ms == COOLDOWN_LONG_MS
    assert v.backoff is False

    # 等刷新即可的拒绝：5s 而非 2min
    v = classify_failure(403, b'{"error":{"message":"request not allowed"}}')
    assert v.cooldown_ms == COOLDOWN_SHORT_MS

    # 容量措辞是 provider 范围
    v = classify_failure(503, b'{"error":{"message":"capacity exceeded"}}')
    assert v.kind == KIND_OVERLOADED
    assert v.scope == SCOPE_PROVIDER

    # 未匹配 4xx = request scope，不冷却任何东西
    v = classify_failure(400, b'{"error":{"message":"temperature must be <= 2"}}')
    assert v.scope == SCOPE_REQUEST
    assert v.cooldown_ms == 0

    # OpenCode Zen 2026-10-23 收紧：免费档只允许自家客户端调用（HTTP 403 +
    # FreeTierError）——归 account scope 并给出专属错误码，避免用户对着
    # "没有可用通道"猜谜。
    v_tier = classify_failure(
        403,
        b'{"type":"error","error":{"type":"FreeTierError","message":"OpenCode\'s'
        b' free tier can only be used from within OpenCode."}}',
    )
    assert v_tier.code == "UPSTREAM_FREE_TIER_LOCKED"
    assert v_tier.scope == SCOPE_ACCOUNT
    assert v.retryable is False

    # provider 自己建的 payload 坏了仍冷却账号（唯一 improper 4xx）
    v = classify_failure(400, b'{"error":{"message":"improperly formed request"}}')
    assert v.kind == KIND_INVALID_REQUEST
    assert v.scope == SCOPE_ACCOUNT


def test_classify_never_reads_text_on_5xx() -> None:
    from harness.modules.gateway.classify import SCOPE_TRANSIENT, classify_failure

    # 503 的 reason phrase "Service Unavailable" 含 unavailable——绝不能读成模型判定
    v = classify_failure(503, b'<html><body>503 Service Unavailable</body></html>')
    assert v.scope == SCOPE_TRANSIENT
    assert v.code != "UPSTREAM_MODEL_GONE"

    # 点名拒绝仅在 <500 时采信
    v = classify_failure(404, b'{"error":{"message":"Model is unavailable"}}')
    assert v.code == "UPSTREAM_MODEL_GONE"


def test_classify_retry_after_clamped() -> None:
    from harness.modules.gateway.classify import MAX_PROVIDER_COOLDOWN_MS, classify_failure

    v = classify_failure(429, b"", 10 * 3600 * 1000)  # 上游报 10 小时
    assert v.retry_after_ms == MAX_PROVIDER_COOLDOWN_MS
    assert v.cooldown_ms >= MAX_PROVIDER_COOLDOWN_MS


def test_health_backoff_levels_and_reset() -> None:
    from harness.modules.gateway.classify import KIND_RATE_LIMIT, SCOPE_ACCOUNT, FailureVerdict
    from harness.modules.gateway.router import HealthTracker

    health = HealthTracker()
    verdict = FailureVerdict(KIND_RATE_LIMIT, SCOPE_ACCOUNT, True, 0, backoff=True)

    health.mark_classified("a", verdict, model="m1")
    assert health.available("a", "m1") is False
    first = health.cooldown_remaining("a", "m1")
    assert 1 <= first <= 2  # level 1 → 2s

    health.mark_classified("a", verdict, model="m1")
    second = health.cooldown_remaining("a", "m1")
    assert second >= 3  # level 2 → 4s（单调升）

    # 成功后归零
    health.mark_success("a", "m1")
    assert health.available("a", "m1") is True
    health.mark_classified("a", verdict, model="m1")
    assert health.cooldown_remaining("a", "m1") <= 2

    # request scope 不冷却
    health.mark_classified("b", FailureVerdict("invalid_request", "request", False, 0))
    assert health.available("b") is True

    # provider scope 冷却整条通道
    health.mark_classified("c", FailureVerdict("overloaded", "provider", True, 30_000))
    assert health.available("c") is False
    assert health.available("c", "any-model") is False


# ---------------------------------------------------------------------------
# P0-2 流式 tool_calls 重编号 + 指纹工具抑制
# ---------------------------------------------------------------------------


def test_stream_rewrite_renumbers_tool_calls() -> None:
    from harness.modules.gateway.stream_rewrite import ToolCallRewriter

    rewriter = ToolCallRewriter()
    # 上游与 reasoning/text 共享编号空间：第一个工具调用以 index 2 到达
    line = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":2,"id":"call_1",'
        '"type":"function","function":{"name":"get_weather","arguments":"{\\"ci"}}]}}]}'
    )
    out = rewriter.rewrite_line(line)
    assert len(out) == 1
    chunk = json.loads(out[0][6:])
    tc = chunk["choices"][0]["delta"]["tool_calls"][0]
    assert tc["index"] == 0  # 客户端从 0 开始连续累积
    assert tc["function"]["name"] == "get_weather"

    line2 = 'data: {"choices":[{"delta":{"tool_calls":[{"index":2,"function":{"arguments":"ty\\":\\"BJ\\"}"}}]}}]}'
    out2 = rewriter.rewrite_line(line2)
    tc2 = json.loads(out2[0][6:])["choices"][0]["delta"]["tool_calls"][0]
    assert tc2["index"] == 0  # 同一上游 index 复用同一重编号

    line3 = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":5,"id":"call_2",'
        '"type":"function","function":{"name":"other","arguments":"{}"}}]}}]}'
    )
    tc3 = json.loads(rewriter.rewrite_line(line3)[0][6:])["choices"][0]["delta"]["tool_calls"][0]
    assert tc3["index"] == 1  # 第二个调用接续为 1


def test_stream_rewrite_suppresses_fingerprint_tools() -> None:
    from harness.modules.gateway.stream_rewrite import ToolCallRewriter

    names = frozenset({"bash", "edit", "glob", "grep", "read"})
    rewriter = ToolCallRewriter(names)

    # 指纹工具完整到达 + 参数到达 → 整条丢弃
    line = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":2,"id":"c1",'
        '"type":"function","function":{"name":"read","arguments":"{}"}}]}}]}'
    )
    assert rewriter.rewrite_line(line) == []

    # 合法工具调用不受影响
    line2 = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":3,"id":"c2",'
        '"type":"function","function":{"name":"my_tool","arguments":"{}"}}]}}]}'
    )
    out = rewriter.rewrite_line(line2)
    assert len(out) == 1
    tc = json.loads(out[0][6:])["choices"][0]["delta"]["tool_calls"][0]
    assert tc["index"] == 0  # 被丢弃的键不占用序号
    assert tc["function"]["name"] == "my_tool"


def test_stream_rewrite_name_split_across_deltas() -> None:
    from harness.modules.gateway.stream_rewrite import ToolCallRewriter

    names = frozenset({"grep"})
    rewriter = ToolCallRewriter(names)

    # 名字跨 delta："gr" 仍可能长成 grep → 缓冲；"ep" 补全后精确命中 + 参数到达 → 丢弃
    l1 = 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"gr"}}]}}]}'
    assert rewriter.rewrite_line(l1) == []
    l2 = 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"ep","arguments":"{}"}}]}}]}'
    assert rewriter.rewrite_line(l2) == []

    # "grep_results"：前缀缓冲后放行，且名字完整保留
    rewriter2 = ToolCallRewriter(names)
    a = 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"grep"}}]}}]}'
    assert rewriter2.rewrite_line(a) == []
    b = 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"_results","arguments":"{}"}}]}}]}'
    out = rewriter2.rewrite_line(b)
    frames = [json.loads(x[6:])["choices"][0]["delta"]["tool_calls"][0] for x in out]
    joined = "".join(f.get("function", {}).get("name", "") for f in frames)
    assert joined == "grep_results"  # 缓冲的分片按序补放，不丢字符


def test_stream_rewrite_passthrough_without_tools() -> None:
    from harness.modules.gateway.stream_rewrite import ToolCallRewriter

    rewriter = ToolCallRewriter()  # 无抑制集
    line = 'data: {"choices":[{"delta":{"content":"hi"}}]}'
    assert rewriter.rewrite_line(line) == [line]  # 原样返回，不重新序列化
    assert rewriter.rewrite_line("data: [DONE]") == ["data: [DONE]"]
    assert rewriter.rewrite_line(": keepalive") == [": keepalive"]


@pytest.mark.asyncio
async def test_stream_passthrough_renumbers_and_suppresses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端：思考回合（reasoning 占 index 0/1）+ 指纹工具调用 + 合法调用。"""
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "mock",
                        "base_url": "https://mock.local/v1",
                        "auth": "bearer",
                        "models": ["m1"],
                        "requires_tools": True,
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    db = FakeDB()
    svc = GatewayService(db, config_path=cfg, bootstrap_key="agw-test", watch_config=False)
    await svc.start()
    try:
        ctx = svc.auth.verify("agw-test")
        assert ctx is not None

        class _Resp:
            def aiter_lines(self):  # noqa: ANN202
                async def gen():
                    yield 'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}'
                    # 指纹工具（应被抑制）
                    yield (
                        'data: {"choices":[{"delta":{"tool_calls":[{"index":2,"id":"f1",'
                        '"function":{"name":"read","arguments":"{}"}}]}}]}'
                    )
                    # 合法工具调用（应重编号为 0）
                    yield (
                        'data: {"choices":[{"delta":{"tool_calls":[{"index":3,"id":"c1",'
                        '"function":{"name":"mine","arguments":"{}"}}]}}]}'
                    )
                    yield (
                        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],'
                        '"usage":{"total_tokens":9}}'
                    )
                    yield "data: [DONE]"

                return gen()

            async def aclose(self) -> None:
                return None

        chunks: list[bytes] = []
        suppress = frozenset({"bash", "edit", "glob", "grep", "read"})
        async for chunk in svc._stream(_Resp(), ctx, "mock", "c", "m1", "t", time.perf_counter(), 1, suppress):
            chunks.append(chunk)
        blob = b"".join(chunks).decode("utf-8")
        assert "read" not in blob  # 指纹工具调用被抑制
        assert '"index": 0' in blob or '"index":0' in blob  # 合法调用重编号为 0
        assert "mine" in blob
        assert "data: [DONE]" in blob
        await svc.usage.stop()
        rows = svc.store.query_usage("default")
        assert rows and rows[0]["total_tokens"] == 9
    finally:
        await svc.stop()


# ---------------------------------------------------------------------------
# P0-3 探测判定语义分层
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_verdicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一探测端点，不同故障形态给出不同判定（不对称性是故意的）。"""
    from harness.modules.gateway.adapters.openai_like import OpenAILikeAdapter
    from harness.modules.gateway.contracts import (
        PROBE_GATEWAY_TROUBLE,
        PROBE_MODEL_DENIED,
        PROBE_OK,
        PROBE_REGION_BLOCKED,
    )

    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "mock",
                        "base_url": "https://mock.local/v1",
                        "auth": "bearer",
                        "models": ["m1"],
                        "probe_url": "https://mock.local/v1/models",
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    svc = GatewayService(FakeDB(), config_path=cfg, bootstrap_key="agw-test", watch_config=False)
    await svc.start()
    try:
        adapter = OpenAILikeAdapter()
        channel = svc.snapshot.channels["mock"]
        cred = svc.credentials.resolve(channel)[0]

        cases = [
            (200, b'{"data":[]}', PROBE_OK),
            (503, b"upstream overloaded", PROBE_GATEWAY_TROUBLE),  # 网关自身故障
            (429, b"slow down", PROBE_GATEWAY_TROUBLE),  # 容量/限流不是模型判定
            (404, b'{"error":{"message":"no such model"}}', PROBE_MODEL_DENIED),  # 点名拒绝
            (403, b'{"error":{"message":"not available in your country"}}', PROBE_REGION_BLOCKED),
        ]
        import harness.modules.gateway.adapters.openai_like as oal

        for status, body, expected in cases:

            class _Client:
                def __init__(self, *a, **kw) -> None:  # noqa: ANN002 ANN003
                    pass

                async def __aenter__(self):  # noqa: ANN204
                    return self

                async def __aexit__(self, *a) -> None:  # noqa: ANN002
                    return None

                async def get(self, url, headers=None):  # noqa: ANN001 ANN201
                    return httpx.Response(status, content=body, request=httpx.Request("GET", url))

            monkeypatch.setattr(oal.httpx, "AsyncClient", _Client)
            verdict = await adapter.probe(channel, cred)
            assert verdict == expected, f"status={status} body={body!r} → {verdict}"
        monkeypatch.undo()
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_models_marks_locked_model_unavailable(tmp_path: Path) -> None:
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "a",
                        "base_url": "https://a.local/v1",
                        "auth": "bearer",
                        "models": ["m1", "m2"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    db = FakeDB()
    svc = GatewayService(db, config_path=cfg, bootstrap_key="agw-test", watch_config=False)
    await svc.start()
    try:
        ctx = svc.auth.verify("agw-test")
        assert ctx is not None
        by_id = {m["id"]: m for m in svc.models(ctx)}
        assert by_id["a/m1"]["available"] is True
        assert by_id["a/m2"]["available"] is True

        # 模型级冷却：m1 不可用但保留在列表里（用户看得到"有这个东西"）
        svc.health.mark_failure("a", 600, model="m1")
        by_id = {m["id"]: m for m in svc.models(ctx)}
        assert by_id["a/m1"]["available"] is False
        assert "a/m1" in by_id
        assert by_id["a/m2"]["available"] is True
        # 通道整体仍算可用（还有一个模型活着）
        assert svc.channels_public()[0]["available"] is True

        # 通道级冷却：两个模型都标不可用，通道整体不可用
        svc.health.mark_failure("a", 600, model="m2")
        assert svc.channels_public()[0]["available"] is False
    finally:
        await svc.stop()


# ---------------------------------------------------------------------------
# P0-4 纯思考空停恢复
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_answer_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """首段只有 reasoning 没有正文 → 自动从检查点续写一次索要正文。"""
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "mock",
                        "base_url": "https://mock.local/v1",
                        "auth": "bearer",
                        "models": ["m1"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(json.dumps(body, ensure_ascii=False))
        if len(bodies) == 1:
            # 首段：只有思考，没有正文（推理模型把预算耗光的形态）
            sse = (
                'data: {"choices":[{"delta":{"reasoning_content":"long thinking..."}}]}\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":5,"completion_tokens":50,"total_tokens":55}}\n'
                "data: [DONE]\n"
            )
        else:
            # 续写段：正常给出正文
            sse = (
                'data: {"choices":[{"delta":{"content":"Final answer."}}]}\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":9,"completion_tokens":3,"total_tokens":12}}\n'
                "data: [DONE]\n"
            )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())

    db = FakeDB()
    svc = GatewayService(
        db,
        config_path=cfg,
        transport=httpx.MockTransport(handler),
        bootstrap_key="agw-test",
        watch_config=False,
    )
    await svc.start()
    try:
        ctx = svc.auth.verify("agw-test")
        assert ctx is not None
        result = await svc.chat(
            {"model": "mock/m1", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            ctx,
            "trace-rec",
        )
        assert result.payload is not None
        assert result.payload["choices"][0]["message"]["content"] == "Final answer."
        # 续写指令带上了检查点
        assert len(bodies) == 2
        assert "Interrupted analysis checkpoint" in bodies[1]
        assert "long thinking..." in bodies[1]
        # 用量合并了两段
        assert result.payload["usage"]["total_tokens"] == 55 + 12
        await svc.usage.stop()
        rows = svc.store.query_usage("default")
        assert rows and rows[0]["total_tokens"] == 67
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_empty_answer_recovery_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """关闭开关后：空停原样返回（reasoning_content 保留，不续写）。"""
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "defaults": {"empty_answer_recovery": False},
                "channels": [
                    {
                        "id": "mock",
                        "base_url": "https://mock.local/v1",
                        "auth": "bearer",
                        "models": ["m1"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        sse = (
            'data: {"choices":[{"delta":{"reasoning_content":"only thinking"}}]}\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
            "data: [DONE]\n"
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())

    db = FakeDB()
    svc = GatewayService(
        db,
        config_path=cfg,
        transport=httpx.MockTransport(handler),
        bootstrap_key="agw-test",
        watch_config=False,
    )
    await svc.start()
    try:
        ctx = svc.auth.verify("agw-test")
        assert ctx is not None
        result = await svc.chat(
            {"model": "mock/m1", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            ctx,
        )
        assert calls["n"] == 1  # 不续写
        assert result.payload is not None
        msg = result.payload["choices"][0]["message"]
        assert msg["content"] == ""
        assert msg["reasoning_content"] == "only thinking"
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_no_recovery_when_text_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """有正文（哪怕同时有 reasoning）不触发恢复。"""
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "mock",
                        "base_url": "https://mock.local/v1",
                        "auth": "bearer",
                        "models": ["m1"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        sse = (
            'data: {"choices":[{"delta":{"reasoning_content":"think","content":"answer"}}]}\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
            "data: [DONE]\n"
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())

    db = FakeDB()
    svc = GatewayService(
        db,
        config_path=cfg,
        transport=httpx.MockTransport(handler),
        bootstrap_key="agw-test",
        watch_config=False,
    )
    await svc.start()
    try:
        ctx = svc.auth.verify("agw-test")
        assert ctx is not None
        result = await svc.chat(
            {"model": "mock/m1", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            ctx,
        )
        assert calls["n"] == 1
        assert result.payload is not None
        assert result.payload["choices"][0]["message"]["content"] == "answer"
    finally:
        await svc.stop()

# ---------------------------------------------------------------------------
# 免费模型接入：进程内 provider + 可用性快照 + 清单探测
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_availability_snapshot_and_internal_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """availability_snapshot 反映模型级冷却；internal_context 免鉴权可用。"""
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "workbuddy",
                        "base_url": "https://workbuddy.local/v2",
                        "auth": "bearer",
                        "models": ["hy3", "step-5-preview"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    svc = GatewayService(FakeDB(), config_path=cfg, bootstrap_key="agw-test", watch_config=False)
    await svc.start()
    try:
        ctx = svc.internal_context()
        assert ctx.has_scope("chat")

        snap = svc.availability_snapshot(["workbuddy"])
        ch = snap["channels"]["workbuddy"]
        assert ch["provider_id"] == "gw-workbuddy"
        assert ch["available"] is True
        assert ch["models"]["hy3"]["available"] is True

        # 模型级冷却 → 快照实时反映
        svc.health.mark_failure("workbuddy", 600, model="hy3")
        snap = svc.availability_snapshot(["workbuddy"])
        assert snap["channels"]["workbuddy"]["models"]["hy3"]["available"] is False
        assert snap["channels"]["workbuddy"]["models"]["hy3"]["cooldown_remaining"] > 0
        # 另一个模型不受牵连，通道整体仍可用
        assert snap["channels"]["workbuddy"]["models"]["step-5-preview"]["available"] is True
        assert snap["channels"]["workbuddy"]["available"] is True

        # 全部模型冷却 → 通道整体不可用
        svc.health.mark_failure("workbuddy", 600, model="step-5-preview")
        assert svc.availability_snapshot(["workbuddy"])["channels"]["workbuddy"]["available"] is False
    finally:
        await svc.stop()


def test_catalog_probe_id_extraction() -> None:
    """_collect_ids / present_ids：跨三家目录形状抽 id，并匹配通道模型。"""
    from harness.modules.gateway.catalog_probe import _collect_ids, present_ids

    cline = {"free": [{"id": "cline-free/minimax-m3.1-flash"}, {"id": "reflection/beam-free"}]}
    ids = _collect_ids(cline)
    assert "cline-free/minimax-m3.1-flash" in ids

    # 匹配裸名与带前缀名
    present = present_ids(ids, ["cline-free/minimax-m3.1-flash", "cline-free/ghost"], prefix="cline")
    assert present is not None
    assert "cline-free/minimax-m3.1-flash" in present
    assert "cline-free/ghost" not in present

    # 抓取失败 → None：绝不改变可用性
    assert present_ids(None, ["a", "b"]) is None


@pytest.mark.asyncio
async def test_probe_now_updates_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """probe_now 拉目录：目录里没有的模型被标为不可用（in_catalog=False）。"""
    from harness.modules.gateway.adapters.openai_like import OpenAILikeAdapter
    from harness.modules.gateway.contracts import PROBE_OK

    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "workbuddy",
                        "base_url": "https://workbuddy.local/v2",
                        "auth": "bearer",
                        "models": ["hy3", "gone-model"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                        "probe_url": "https://workbuddy.local/v3/config",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    async def ok_probe(self: Any, channel: Any, credential: Any) -> str:
        return PROBE_OK

    monkeypatch.setattr(OpenAILikeAdapter, "probe", ok_probe)

    catalog_body = json.dumps({"models": [{"id": "hy3"}]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=catalog_body.encode())

    real_fetch = None

    async def fake_fetch(url: str, headers: Any, **kw: Any) -> set[str] | None:
        return await real_fetch(url, headers, transport=httpx.MockTransport(handler))

    import harness.modules.gateway.catalog_probe as cp

    real_fetch = cp.fetch_catalog_ids
    monkeypatch.setattr(cp, "fetch_catalog_ids", fake_fetch)

    svc = GatewayService(FakeDB(), config_path=cfg, bootstrap_key="agw-test", watch_config=False)
    await svc.start()
    try:
        await svc.probe_now(["workbuddy"])
        snap = svc.availability_snapshot(["workbuddy"])
        models = snap["channels"]["workbuddy"]["models"]
        assert models["hy3"]["in_catalog"] is True
        assert models["hy3"]["available"] is True
        assert models["gone-model"]["in_catalog"] is False
        assert models["gone-model"]["available"] is False
        # 通道整体仍可用（至少 hy3 活着）
        assert snap["channels"]["workbuddy"]["available"] is True
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_gateway_provider_chat_via_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """进程内 provider.chat()：走真实 GatewayService，把 SSE 字节还原为块协议。"""
    from plugins.provider_gateway.main import GatewayProvider

    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "workbuddy",
                        "base_url": "https://workbuddy.local/v2",
                        "auth": "bearer",
                        "models": ["hy3"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    sse = (
        'data: {"choices":[{"index":0,"delta":{"reasoning_content":"thinking...\\n"}}]}\n'
        'data: {"choices":[{"index":0,"delta":{"content":"Hello "}}]}\n'
        'data: {"choices":[{"index":0,"delta":{"content":"free!"}}],'
        '"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}\n'
        "data: [DONE]\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())

    from harness.kernel.services import ServiceRegistry as _Reg

    svc = GatewayService(
        FakeDB(),
        services=_Reg(),
        config_path=cfg,
        transport=httpx.MockTransport(handler),
        bootstrap_key="agw-test",
        watch_config=False,
    )
    await svc.start()
    try:
        provider = GatewayProvider(
            api_key="gateway-internal",
            models=["workbuddy/hy3"],
            extra_params={"_gateway_services": svc.services, "_gateway_channel": "workbuddy"},
        )
        # 让 provider 拿到刚启动的 svc（走 services 注册表路径）
        svc.services.register(GatewayService, svc, owner="test")
        chunks: list[dict[str, Any]] = []
        async for blk in provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="workbuddy/hy3",
            stream=True,
            temperature=0.7,
        ):
            chunks.append(blk)
        text = "".join(b.get("delta", "") for b in chunks)
        reasoning = "".join(b.get("reasoning_content", "") for b in chunks)
        usage_last = chunks[-1].get("usage") if chunks else None
        assert text == "Hello free!"
        assert reasoning.startswith("thinking")
        assert usage_last and usage_last["total_tokens"] == 6
    finally:
        await svc.stop()


# ---------------------------------------------------------------------------
# Qoder CN COSY 适配器（协议移植 + 回归）
# ---------------------------------------------------------------------------


def test_qoder_cosy_envelope_and_body_codec() -> None:
    """COSY 信封结构 + 私有 base64 编解码（可逆 + 与参考实现同构）。"""
    from harness.modules.gateway.adapters.qoder_cosy import (
        ALPHABET,
        CosyAccount,
        cosy_headers,
        decode_body,
        encode_body,
        sign_path,
    )

    account = CosyAccount(uid="uid-1", name="安迪", email="a@b.c", access="dt-token", machine_id="mid-1")
    headers = cosy_headers(
        "https://gateway.qoder.com.cn/algo/api/v2/model/list?Encode=1", account, "", ts=1700000000
    )
    auth = headers["Authorization"]
    assert auth.startswith("Bearer COSY.")
    payload, sig = auth[len("Bearer COSY.") :].split(".")
    assert headers["Cosy-ClientType"] == "10"
    assert headers["Cosy-MachineId"] == "mid-1"
    assert headers["Cosy-User"] == "uid-1"
    assert headers["Cosy-Date"] == "1700000000"
    # 签名覆盖的是去掉 /algo 前缀的路径
    assert sign_path("https://x.com/algo/api/v2/model/list?Encode=1") == "/api/v2/model/list"
    # payload 是 base64 的信封 JSON
    import base64

    env = json.loads(base64.b64decode(payload))
    assert env["version"] == "v1"
    assert env["cosyVersion"]
    assert env["info"] and env["requestId"]
    assert len(sig) == 32  # md5 hex

    # 私有 base64：可逆，且只用字母表 + '$' 补位
    text = json.dumps({"a": "中文", "b": [1, 2, 3]}, ensure_ascii=False)
    wire = encode_body(text)
    assert set(wire) <= set(ALPHABET) | {"$"}
    assert decode_body(wire) == text


def test_qoder_agent_body_shape() -> None:
    """Qoder 请求体形状：块内容 + system/business/parameters 等专有字段。"""
    from harness.modules.gateway.adapters.qoder_cosy import build_agent_body

    body = build_agent_body(
        [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "hi"},
        ],
        {"key": "qfmodel", "source": "system"},
        model_key="qfmodel",
        max_tokens=1024,
        session_id="sess-1",
    )
    assert body["agent_id"] == "agent_common"
    assert body["task_id"] == "common"
    assert body["session_type"] == "app"
    assert body["session_id"] == "sess-1"
    assert body["parameters"]["max_tokens"] == 1024
    assert body["parameters"]["enable_thinking"] is True
    # system 提示：QODER_SYS 前缀 + 用户的 system 文本
    assert body["system"][0]["text"].startswith("You are a Qoder agent")
    assert "你是助手" in body["system"][0]["text"]
    # 消息是块数组，system 消息同时出现在 messages 里
    user_msg = next(m for m in body["messages"] if m["role"] == "user")
    assert user_msg["content"][0]["type"] == "text"
    assert user_msg["content"][0]["text"] == "hi"


@pytest.mark.asyncio
async def test_qoder_adapter_build_request_and_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """适配器：build_request 产出 raw_body（wire 串）+ COSY 头；probe 走清单端点。"""
    from harness.modules.gateway.adapters.qoder_cosy_adapter import QoderCosyAdapter
    from harness.modules.gateway.config import ChannelConfig
    from harness.modules.gateway.contracts import PROBE_OK, Credential

    monkeypatch.setenv("MOCK_KEY", "sk-test")
    channel = ChannelConfig(
        id="qoder-cn",
        base_url="https://gateway.qoder.com.cn",
        auth="qoder-device",
        models=["qfmodel", "dfmodel"],
        chat_path="/algo/api/v2/service/pro/sse/agent_chat_generation?Encode=1",
        probe_url="https://gateway.qoder.com.cn/algo/api/v2/model/list?Encode=1",
    )
    credential = Credential(
        id="qoder-cn",
        secret="dt-token",
        meta={"uid": "uid-1", "name": "安迪", "email": "", "machine_id": "mid-1"},
    )
    adapter = QoderCosyAdapter()

    # 清单桩：两个免费模型
    listing = {
        "chat": [
            {"key": "qfmodel", "enable": True, "price_factor": 0.0, "source": "system"},
            {"key": "dfmodel", "enable": True, "price_factor": 0.0, "source": "system"},
        ]
    }

    async def fake_fetch_listing(ch: Any, cred: Any) -> dict[str, Any] | None:
        return listing

    monkeypatch.setattr(adapter, "fetch_listing", fake_fetch_listing)
    await adapter.prepare(channel, credential)

    req = adapter.build_request(
        channel,
        credential,
        {"model": "qoder-cn/qfmodel", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        session_key="s1",
    )
    assert req.raw_body is not None and req.json_body is None  # body 不是 JSON
    assert b"hi" not in req.raw_body  # 已被私有 base64 编码
    assert req.headers["X-Model-Key"] == "qfmodel"
    assert req.headers["Authorization"].startswith("Bearer COSY.")
    assert "https://gateway.qoder.com.cn/algo/api/v2/service/pro/sse/agent_chat_generation" in req.url

    # probe：清单可达 → ok
    assert await adapter.probe(channel, credential) == PROBE_OK

    # 清单里没有免费模型 → model_denied（长冷却）
    async def fake_listing_no_free(ch: Any, cred: Any) -> dict[str, Any] | None:
        return {"chat": [{"key": "qmodel_38max", "enable": True, "price_factor": 0.5}]}

    monkeypatch.setattr(adapter, "fetch_listing", fake_listing_no_free)
    adapter2 = QoderCosyAdapter()
    monkeypatch.setattr(adapter2, "fetch_listing", fake_listing_no_free)
    from harness.modules.gateway.contracts import PROBE_MODEL_DENIED

    assert await adapter2.probe(channel, credential) == PROBE_MODEL_DENIED


def test_qoder_envelope_error_translation() -> None:
    """信封错误翻译：排队 10605→503 可重试；额度→429；签名错→凭据冷却。"""
    import json as _json

    from harness.modules.gateway.adapters.qoder_cosy_adapter import QoderCosyAdapter

    adapter = QoderCosyAdapter()

    # 实测形态：外层 403，body 嵌套 code=10605 + isQueued
    queued_inner = _json.dumps(
        {
            "isQueued": True,
            "modelKey": "qfmodel",
            "queueCount": 0,
            "queueType": "p3",
            "retryAfterSeconds": 30,
            "serviceAvailable": False,
        }
    )
    err = adapter._envelope_error(403, _json.dumps({"code": "10605", "message": queued_inner}))
    assert err.http_status == 503
    assert err.retryable is True
    assert err.cooldown_scope == "none"  # 排队不是账号/通道的错
    assert err.retry_after_ms == 30_000

    # 额度类：冷却凭据
    err = adapter._envelope_error(403, _json.dumps({"code": "110", "message": "quota used up"}))
    assert err.http_status == 429
    assert err.cooldown_scope == "credential"

    # 其它 401/403：登录态问题，长冷却
    err = adapter._envelope_error(401, _json.dumps({"code": "999", "message": "login expired"}))
    assert err.cooldown_scope == "credential"
    assert err.retryable is False

    # 无法解析的 body：退化为通用错误但不炸（上游 5xx 对客户端表现为 502）
    err = adapter._envelope_error(500, "not json at all")
    assert err.http_status == 502
    assert err.retryable is True


@pytest.mark.asyncio
async def test_qoder_aggregate_stream_parses_envelope() -> None:
    """SSE 解析：外层信封 {statusCodeValue, body}，body 内是 OpenAI chunk。"""
    from harness.modules.gateway.adapters.qoder_cosy_adapter import QoderCosyAdapter

    async def lines() -> Any:
        yield 'data:{"statusCodeValue":200,"body":"{\\"choices\\":[{\\"delta\\":{\\"content\\":\\"你好\\"}}]}"}'
        yield 'data:{"statusCodeValue":200,"body":"{\\"choices\\":[{\\"delta\\":{\\"reasoning_content\\":\\"想\\"}}]}"}'
        yield (
            'data:{"statusCodeValue":200,"body":"{\\"choices\\":[{\\"delta\\":{},'
            '\\"finish_reason\\":\\"stop\\"}],'
            '\\"usage\\":{\\"prompt_tokens\\":3,\\"completion_tokens\\":2,\\"total_tokens\\":5}}"}'
        )
        yield "data: [DONE]"

    adapter = QoderCosyAdapter()
    outcome = await adapter.aggregate_stream(None, lines(), "qoder-cn/qfmodel")
    message = outcome.payload["choices"][0]["message"]
    assert message["content"] == "你好"
    assert message["reasoning_content"] == "想"
    assert outcome.payload["usage"]["total_tokens"] == 5
    assert outcome.saw_text is True
    assert outcome.saw_reasoning is True


def test_qoder_map_error_parses_envelope() -> None:
    """回归：HTTP 4xx 承载扁平信封 `{"code":...,"message":...}` 时，map_error 必须
    解析信封再翻译，不能把排队 10605 误判成 403 凭据错。

    实测 2026-10-22：Qoder 排队以 HTTP 403 + `{"code":"10605","message":"{...}"}`
    返回，原 map_error 只做 classify_failure → CRED_INVALID 502 并长冷却账号，
    导致前端把 Qoder 误标为永久不可用。"""
    import json as _json

    from harness.modules.gateway.adapters.qoder_cosy_adapter import QoderCosyAdapter

    adapter = QoderCosyAdapter()

    queued_inner = _json.dumps(
        {
            "isQueued": True,
            "modelKey": "qfmodel",
            "queueType": "p3",
            "retryAfterSeconds": 30,
            "serviceAvailable": False,
        }
    )
    # HTTP 403 携带扁平信封
    err = adapter.map_error(
        403, _json.dumps({"code": "10605", "message": queued_inner}).encode("utf-8")
    )
    assert err.http_status == 503
    assert err.retryable is True
    assert err.cooldown_scope == "none"  # 排队不是账号/通道的错，不得冷却
    assert err.retry_after_ms == 30_000

    # 非信封的普通 403 仍走通用分类
    err = adapter.map_error(403, b"plain text error")
    assert err.code  # 仍产出合法 GatewayError，不炸


def test_qoder_envelope_error_nested_queue() -> None:
    """回归：排队码在**第三层**嵌套 message 里（实测抓包），必须逐层解包。

    实测形态：外层 statusCodeValue=403 → body 的 code="403" → message 里
    code="10605" → 再一层 message 才有 isQueued。只解一层会误判成 403 凭据错，
    导致长冷却账号 + 前端把 Qoder 标成永久不可用。"""
    import json as _json

    from harness.modules.gateway.adapters.qoder_cosy_adapter import QoderCosyAdapter

    adapter = QoderCosyAdapter()
    inner = {"isQueued": True, "modelKey": "qfmodel", "retryAfterSeconds": 30}
    level2 = {"code": "10605", "message": _json.dumps(inner), "subCode": "RESOURCE_LIMIT"}
    level1 = {"code": "403", "message": _json.dumps(level2)}
    err = adapter._envelope_error(403, _json.dumps(level1))
    assert err.http_status == 503
    assert err.retryable is True
    assert err.cooldown_scope == "none"
    assert err.retry_after_ms == 30_000
    assert "排队中" in err.message


def test_qoder_normalize_stream_line() -> None:
    """信封 SSE → 标准 OpenAI SSE；自定义 event 丢弃；错误信封转 error 块。"""
    import json as _json

    from harness.modules.gateway.adapters.qoder_cosy_adapter import QoderCosyAdapter

    adapter = QoderCosyAdapter()
    env = {
        "headers": {},
        "statusCodeValue": 200,
        "statusCode": "OK",
        "body": _json.dumps({"choices": [{"index": 0, "delta": {"content": "Hi"}}]}),
    }
    assert adapter.normalize_stream_line("data:" + _json.dumps(env)) == [
        'data: {"choices":[{"index":0,"delta":{"content":"Hi"}}]}'
    ]
    assert adapter.normalize_stream_line("event:finish") == []
    assert adapter.normalize_stream_line('data:{"statusCodeValue":200,"body":"[DONE]"}') == [
        "data: [DONE]"
    ]

    inner = {"isQueued": True, "retryAfterSeconds": 30}
    level2 = {"code": "10605", "message": _json.dumps(inner)}
    level1 = {"code": "403", "message": _json.dumps(level2)}
    err_env = {"statusCodeValue": 403, "body": _json.dumps(level1)}
    err_lines = adapter.normalize_stream_line("data:" + _json.dumps(err_env))
    assert len(err_lines) == 1 and '"error"' in err_lines[0]


@pytest.mark.asyncio
async def test_gateway_qoder_stream_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """网关流式：Qoder 信封归一化后 provider 能收到块；错误信封终止且不吐后续内容。"""
    from plugins.provider_gateway.main import GatewayProvider

    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "qoder-cn",
                        "base_url": "https://qoder.local",
                        "adapter": "qoder-cosy",
                        "auth": "bearer",
                        "models": ["qfmodel"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def envelope(body: str, status: int = 200) -> str:
        return "data:" + json.dumps(
            {"headers": {}, "statusCodeValue": status, "statusCode": "OK", "body": body}
        )

    ok_sse = (
        envelope(json.dumps({"choices": [{"index": 0, "delta": {"content": "Hi"}}]}))
        + "\n"
        + envelope(json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
        + "\n"
        + envelope("[DONE]")
        + "\n"
        + "event:finish\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=ok_sse.encode()
        )

    from harness.kernel.services import ServiceRegistry as _Reg

    svc = GatewayService(
        FakeDB(),
        services=_Reg(),
        config_path=cfg,
        transport=httpx.MockTransport(handler),
        bootstrap_key="agw-test",
        watch_config=False,
    )
    await svc.start()
    from harness.modules.gateway.contracts import Credential as _Cred

    monkeypatch.setattr(
        svc.credentials,
        "resolve",
        lambda channel, last_error=None: [
            _Cred(
                id="c",
                secret="sk-test",
                meta={"machine_id": "m1", "uid": "u1", "name": "n", "email": "e"},
            )
        ],
    )
    try:
        svc.services.register(GatewayService, svc, owner="test")
        provider = GatewayProvider(
            models=["qoder-cn/qfmodel"],
            extra_params={"_gateway_services": svc.services, "_gateway_channel": "qoder-cn"},
        )
        blocks = [
            b
            async for b in provider.chat(
                [{"role": "user", "content": "hi"}], "qoder-cn/qfmodel", stream=True
            )
        ]
        assert "".join(b.get("delta", "") for b in blocks) == "Hi"
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_gateway_qoder_stream_error_terminates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """流内错误信封：发出 error 块后立即终止，绝不继续吐后续行（否则页面永远转圈）。"""
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "qoder-cn",
                        "base_url": "https://qoder.local",
                        "adapter": "qoder-cosy",
                        "auth": "bearer",
                        "models": ["qfmodel"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    inner = {"isQueued": True, "retryAfterSeconds": 30}
    level2 = {"code": "10605", "message": json.dumps(inner)}
    level1 = {"code": "403", "message": json.dumps(level2)}

    def envelope(body: str, status: int = 200) -> str:
        return "data:" + json.dumps(
            {"headers": {}, "statusCodeValue": status, "statusCode": "OK", "body": body}
        )

    err_sse = (
        envelope(json.dumps(level1), 403)
        + "\n"
        + envelope(json.dumps({"choices": [{"index": 0, "delta": {"content": "SHOULD_NOT_APPEAR"}}]}))
        + "\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=err_sse.encode()
        )

    from harness.kernel.services import ServiceRegistry as _Reg

    svc = GatewayService(
        FakeDB(),
        services=_Reg(),
        config_path=cfg,
        transport=httpx.MockTransport(handler),
        bootstrap_key="agw-test",
        watch_config=False,
    )
    await svc.start()
    from harness.modules.gateway.contracts import Credential as _Cred

    monkeypatch.setattr(
        svc.credentials,
        "resolve",
        lambda channel, last_error=None: [
            _Cred(
                id="c",
                secret="sk-test",
                meta={"machine_id": "m1", "uid": "u1", "name": "n", "email": "e"},
            )
        ],
    )
    try:
        res = await svc.chat(
            {
                "model": "qoder-cn/qfmodel",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            svc.internal_context(),
        )
        lines: list[str] = []
        assert res.iterator is not None
        async for raw in res.iterator:
            lines.extend(raw.decode("utf-8", "replace").splitlines())
        joined = "\n".join(lines)
        assert '"error"' in joined  # 错误事件已发给客户端
        assert "SHOULD_NOT_APPEAR" not in joined  # 错误后立即终止
    finally:
        await svc.stop()


def _simple_cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "workbuddy",
                        "base_url": "https://workbuddy.local/v2",
                        "auth": "bearer",
                        "models": ["hy3"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return cfg


@pytest.mark.asyncio
async def test_gateway_provider_empty_stream_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：网关流零内容时 provider 必须报错，不能静默返回空回复。

    现象：朋友新装机器、Cline 未登录/上游返回非 SSE 错误体时，页面只出现
    空白回复（"提问后没有任何回复"）——根因就是零块被当成成功。"""
    from plugins.provider_gateway.main import GatewayProvider

    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = _simple_cfg(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=b"data: [DONE]\n"
        )

    from harness.kernel.services import ServiceRegistry as _Reg

    svc = GatewayService(
        FakeDB(),
        services=_Reg(),
        config_path=cfg,
        transport=httpx.MockTransport(handler),
        bootstrap_key="agw-test",
        watch_config=False,
    )
    await svc.start()
    try:
        svc.services.register(GatewayService, svc, owner="test")
        provider = GatewayProvider(
            models=["workbuddy/hy3"],
            extra_params={"_gateway_services": svc.services, "_gateway_channel": "workbuddy"},
        )
        with pytest.raises(Exception) as excinfo:
            async for _blk in provider.chat(
                [{"role": "user", "content": "hi"}], "workbuddy/hy3", stream=True
            ):
                pass
        assert "未返回任何内容" in str(excinfo.value)
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_gateway_stream_non_sse_error_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：HTTP 200 + 非 SSE 错误体（{"error":...}）须转成 error 块并终止。

    这类响应若原样透传，provider 只认 `data: ` 会零块输出 → 页面无反馈。"""
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = _simple_cfg(tmp_path)
    body = b'{"error": {"message": "upstream boom", "code": "BOOM"}}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=body
        )

    from harness.kernel.services import ServiceRegistry as _Reg

    svc = GatewayService(
        FakeDB(),
        services=_Reg(),
        config_path=cfg,
        transport=httpx.MockTransport(handler),
        bootstrap_key="agw-test",
        watch_config=False,
    )
    await svc.start()
    try:
        res = await svc.chat(
            {
                "model": "workbuddy/hy3",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            svc.internal_context(),
        )
        assert res.iterator is not None
        lines: list[str] = []
        async for raw in res.iterator:
            lines.extend(raw.decode("utf-8", "replace").splitlines())
        joined = "\n".join(lines)
        assert '"error"' in joined
        assert "upstream boom" in joined
    finally:
        await svc.stop()


def test_credential_resolver_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """凭据就绪判定：只查存在性，不刷新 token、不发请求。"""
    from harness.modules.gateway.config import ChannelConfig, CredentialRef
    from harness.modules.gateway.credentials import ClineTokenManager, CredentialResolver

    resolver = CredentialResolver()

    ch_public = ChannelConfig(id="zen", base_url="https://x", auth="public", models=["m"])
    assert resolver.present(ch_public) is True

    ch_env = ChannelConfig(
        id="e",
        base_url="https://x",
        auth="bearer",
        models=["m"],
        credentials=[CredentialRef(id="c", source="env:GW_TEST_KEY_PRESENT")],
    )
    monkeypatch.delenv("GW_TEST_KEY_PRESENT", raising=False)
    assert resolver.present(ch_env) is False
    monkeypatch.setenv("GW_TEST_KEY_PRESENT", "sk-x")
    assert resolver.present(ch_env) is True

    ch_cline = ChannelConfig(id="cline", base_url="https://x", auth="cline-cli", models=["m"])
    monkeypatch.setattr(ClineTokenManager, "path", tmp_path / "missing.json")
    assert resolver.present(ch_cline) is False
    good = tmp_path / "cline.json"
    good.write_text(
        json.dumps({"providers": {"cline": {"settings": {"auth": {"accessToken": "a"}}}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ClineTokenManager, "path", good)
    assert resolver.present(ch_cline) is True


@pytest.mark.asyncio
async def test_provider_gateway_skips_unready_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """凭据未就绪的通道不注册 provider（避免「选得到、发出去、只收到报错」）。"""
    from harness.modules.gateway.credentials import ClineTokenManager

    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "zen",
                        "base_url": "https://zen.local",
                        "auth": "public",
                        "models": ["big-pickle"],
                    },
                    {
                        "id": "cline",
                        "base_url": "https://cline.local",
                        "auth": "cline-cli",
                        "models": ["cline-free/x"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ClineTokenManager, "path", tmp_path / "no-cline.json")

    from harness.kernel.services import ServiceRegistry as _Reg

    svc = GatewayService(
        FakeDB(), services=_Reg(), config_path=cfg, bootstrap_key="agw-test", watch_config=False
    )
    await svc.start()
    try:
        from plugins.provider_gateway.main import GatewayProviderPlugin

        assert GatewayProviderPlugin._models_of(svc, "zen") == ["zen/big-pickle"]
        assert GatewayProviderPlugin._models_of(svc, "cline") == []
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_config_hot_reload_resyncs_provider_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：配置热更新后 provider 注册表必须同步（曾因只在启动时注册，
    下拉里留着已删除的模型，用户点它们得到无意义的报错）。"""
    from harness.kernel.services import ServiceRegistry as _Reg
    from harness.modules.model_manager.provider_registry import ProviderRegistry
    from plugins.provider_gateway.main import GatewayProviderPlugin

    cfg_path = tmp_path / "gateway.json"
    cfg_path.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "zen",
                        "base_url": "https://zen.local",
                        "auth": "public",
                        "models": ["big-pickle", "stale-model"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    services = _Reg()
    svc = GatewayService(
        FakeDB(), services=services, config_path=cfg_path, bootstrap_key="agw-test",
        watch_config=False,
    )
    await svc.start()
    try:
        services.register(GatewayService, svc, owner="test")
        registry = ProviderRegistry()
        services.register(ProviderRegistry, registry, owner="test")
        plugin = GatewayProviderPlugin()
        plugin._provider_registry = registry
        ctx_fake = _FakeCtx(services)
        # 与 activate() 相同的生产接线
        svc.login.on_credential_saved = lambda _c: plugin._sync_channels(ctx_fake, svc)
        svc.on_config_applied = lambda: plugin._sync_channels(ctx_fake, svc)
        plugin._sync_channels(ctx_fake, svc)
        assert "zen/stale-model" in registry.list_providers()[0]["models"]

        # 模拟热更新：删掉 stale-model 再应用配置
        cfg_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "channels": [
                        {
                            "id": "zen",
                            "base_url": "https://zen.local",
                            "auth": "public",
                            "models": ["big-pickle"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc.apply_config(json.loads(cfg_path.read_text(encoding="utf-8")), actor="test")
        models = registry.list_providers()[0]["models"]
        assert models == ["zen/big-pickle"]
        # 通道整体禁用后 provider 必须注销（不是留着报错入口）
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["channels"][0]["enabled"] = False
        svc.apply_config(cfg, actor="test")
        assert registry.list_providers() == []
    finally:
        await svc.stop()


class _FakeCtx:
    """最小 PluginContext 替身（只带 provider 同步用到的 services/logger）。"""

    class _Logger:
        def info(self, *_a: Any, **_k: Any) -> None: ...
        def warning(self, *_a: Any, **_k: Any) -> None: ...

    def __init__(self, services: Any) -> None:
        self.services = services
        self.logger = self._Logger()


def test_gateway_example_config_official_mainline() -> None:
    """示例配置：官方免费额度通道默认启用；客户端通道默认关闭；zen 因上游收紧也默认关。"""
    from harness.modules.gateway.config import ConfigService

    example = Path(__file__).resolve().parents[1] / "gateway.example.json"
    snap = ConfigService(example).load()
    for cid in ("qwen-bailian", "doubao-ark"):
        assert snap.channels[cid].enabled is True
    # zen 2026-10-23 起上游把免费档收归 OpenCode 客户端内部（第三方调用一律
    # 403 FreeTierError），故默认禁用、界面不再显示；保留通道定义便于政策回摆时启用。
    for cid in ("cline", "workbuddy", "qoder-cn", "zen"):
        assert snap.channels[cid].enabled is False
    assert snap.channels["qwen-bailian"].credentials[0].source == "env:DASHSCOPE_API_KEY"
    assert snap.channels["doubao-ark"].credentials[0].source == "env:ARK_API_KEY"
    assert snap.aliases["free/auto"][0] == "qwen-bailian/qwen-plus"


def test_dead_models_removed_and_catalog_probe_gated() -> None:
    """真机确认不可达的模型已移除；WorkBuddy 目录端点不可靠、跳过清单判定。

    - cline/poolside/laguna-s-2.1-free：Vercel 路由流式 100% 失败（会话页全走流式）
    - zen/flint-40b-free：上游持续 503「Endpoint is unavailable」
    两者都不该再出现在示例配置里（界面不再显示）。workbuddy 的 /v3/config 不是
    模型目录，catalog_probe 必须为 False，否则可用的 step-5-preview 会被误置灰。
    """
    from harness.modules.gateway.config import ConfigService

    example = Path(__file__).resolve().parents[1] / "gateway.example.json"
    snap = ConfigService(example).load()
    assert "poolside/laguna-s-2.1-free" not in snap.channels["cline"].models
    assert "flint-40b-free" not in snap.channels["zen"].models
    assert snap.channels["workbuddy"].catalog_probe is False
    # 别名成员必须是仍存在的模型，否则 free/auto 会稳定报 GW_NO_CAPACITY
    for member in snap.aliases["free/auto"]:
        head, _, rest = member.partition("/")
        assert rest in snap.channels[head].models, f"free/auto 引用了已移除模型: {member}"


@pytest.mark.asyncio
async def test_stream_error_only_cools_that_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：某模型流式报错只冷却它自己，同通道其它模型仍可路由。

    这正是用户报的现象——laguna/flint 流式失败曾把整条通道连坐，导致
    big-pickle 等健康模型也报「没有可用通道」。"""
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "mock",
                        "base_url": "https://mock.local/v1",
                        "auth": "bearer",
                        "models": ["bad", "good"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body["model"] == "bad":
            # 流内错误信封（HTTP 200，与真实 laguna 一致）
            sse = 'data: {"error":{"code":"stream_init_failed","message":"bad"}}\n'
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=sse.encode()
            )
        sse = (
            'data: {"choices":[{"delta":{"content":"ok"}}]}\n'
            "data: [DONE]\n"
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse.encode()
        )

    from harness.kernel.services import ServiceRegistry as _Reg

    svc = GatewayService(
        FakeDB(),
        services=_Reg(),
        config_path=cfg,
        transport=httpx.MockTransport(handler),
        bootstrap_key="agw-test",
        watch_config=False,
    )
    await svc.start()
    try:
        ctx = svc.internal_context()
        # 触发 bad 的流式错误（消费掉迭代器让 finally 落地冷却）
        res = await svc.chat(
            {"model": "mock/bad", "messages": [{"role": "user", "content": "x"}], "stream": True},
            ctx,
        )
        assert res.iterator is not None
        async for _ in res.iterator:
            pass
        # bad 应被冷却（模型级），但 good 必须仍然可用（通道未被连坐）
        assert svc.model_available("mock", "bad") is False
        assert svc.model_available("mock", "good") is True
        # good 能正常路由
        res2 = await svc.chat(
            {"model": "mock/good", "messages": [{"role": "user", "content": "x"}], "stream": True},
            ctx,
        )
        assert res2.iterator is not None
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_client_abort_does_not_cooldown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：客户端中断流式（status=499）不冷却——否则用户点「停止生成」
    会把整条通道健康模型连坐成「没有可用通道」（真机 step-5 被误杀）。"""
    monkeypatch.setenv("MOCK_KEY", "sk-test")
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {
                        "id": "mock",
                        "base_url": "https://mock.local/v1",
                        "auth": "bearer",
                        "models": ["m1"],
                        "credentials": [{"id": "c", "source": "env:MOCK_KEY"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        sse = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n'
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse.encode()
        )

    from harness.kernel.services import ServiceRegistry as _Reg

    svc = GatewayService(
        FakeDB(),
        services=_Reg(),
        config_path=cfg,
        transport=httpx.MockTransport(handler),
        bootstrap_key="agw-test",
        watch_config=False,
    )
    await svc.start()
    try:
        ctx = svc.internal_context()
        res = await svc.chat(
            {"model": "mock/m1", "messages": [{"role": "user", "content": "x"}], "stream": True},
            ctx,
        )
        assert res.iterator is not None
        it = res.iterator.__aiter__()
        await it.__anext__()  # 读到第一块
        # 模拟客户端中断：关闭生成器会抛 GeneratorExit，走 _stream 的 499 分支
        await it.aclose()
        # 中断不应把模型冷却
        assert svc.model_available("mock", "m1") is True
    finally:
        await svc.stop()


# ---------------------------------------------------------------------------
# 自建登录（免装 Magpie）：凭据库 + Qoder CN 设备授权 + 解析器优先级
# ---------------------------------------------------------------------------


def test_credential_store_roundtrip_and_ciphertext() -> None:
    """凭据库：加解密往返一致，且表里只存密文。"""
    from harness.modules.gateway.credential_store import GatewayCredentialStore

    store = GatewayCredentialStore(FakeDB())
    store.ensure_schema()
    assert store.load("qoder-cn") is None
    store.save(
        "qoder-cn",
        {"access": "dt-secret", "machine_id": "m1", "uid": "u1", "email": "u@example.com"},
    )
    loaded = store.load("qoder-cn")
    assert loaded is not None
    assert loaded["access"] == "dt-secret"
    blob = store.db.query_one(
        "SELECT payload_enc FROM gw_credentials WHERE channel_id = ?", ("qoder-cn",)
    )
    assert blob is not None
    assert "dt-secret" not in str(blob["payload_enc"])
    assert "u@example.com" not in str(blob["payload_enc"])
    assert store.channels() == ["qoder-cn"]
    store.delete("qoder-cn")
    assert store.load("qoder-cn") is None


def test_credential_store_decrypt_failure_is_silent() -> None:
    """machine key 变更导致解密失败 → 按未登录处理，不抛异常、不泄漏明文。"""
    from harness.infra.crypto import APIKeyEncryptor
    from harness.modules.gateway.credential_store import GatewayCredentialStore

    db = FakeDB()
    writer = GatewayCredentialStore(db, encryptor=APIKeyEncryptor(machine_id="machine-A"))
    writer.ensure_schema()
    writer.save("qoder-cn", {"access": "dt-x"})
    reader = GatewayCredentialStore(db, encryptor=APIKeyEncryptor(machine_id="machine-B"))
    assert reader.load("qoder-cn") is None


@pytest.mark.asyncio
async def test_qoder_device_login_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Qoder CN 自建登录：授权 URL → 轮询 deviceToken → jobToken 4xx 回落 → userinfo → 加密落库。"""
    import asyncio

    from harness.modules.gateway import login as login_mod
    from harness.modules.gateway.credential_store import GatewayCredentialStore
    from harness.modules.gateway.login import DeviceLoginManager, QoderCNDeviceLogin

    monkeypatch.setattr(login_mod, "POLL_INTERVAL_SECONDS", 0.01)
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/deviceToken/poll"):
            polls["n"] += 1
            if polls["n"] < 2:
                return httpx.Response(404, json={"message": "pending"})
            return httpx.Response(
                200,
                json={
                    "token": "dt-abc",
                    "refresh_token": "rt-abc",
                    "expires_at": "2027-01-01T00:00:00Z",
                    "user_id": "u-42",
                },
            )
        if path.endswith("/me/jobToken"):
            # Qoder CN：CLI 本身不申请 job token，4xx 即回落用 device token
            return httpx.Response(400, json={"message": "unsupported for CN"})
        if path.endswith("/userinfo"):
            assert request.headers.get("Authorization") == "Bearer dt-abc"
            return httpx.Response(
                200, json={"id": "u-42", "name": "Qoder 用户", "email": "u@example.com"}
            )
        raise AssertionError(f"unexpected upstream path: {path}")

    store = GatewayCredentialStore(FakeDB())
    store.ensure_schema()
    manager = DeviceLoginManager(
        store, [QoderCNDeviceLogin()], transport=httpx.MockTransport(handler)
    )
    session = manager.start("qoder-cn")
    assert "qoder.cn/device/selectAccounts" in session.login_url
    assert "challenge_method=S256" in session.login_url
    assert "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb" in session.login_url

    for _ in range(300):
        if manager.status(session.session_id)["state"] != "pending":
            break
        await asyncio.sleep(0.02)
    state = manager.status(session.session_id)
    assert state["state"] == "ok", state

    saved = store.load("qoder-cn")
    assert saved is not None
    assert saved["access"] == "dt-abc"  # jobToken 4xx → device token 即对话 token
    assert saved["machine_id"] == session.params["machine_id"]
    assert saved["uid"] == "u-42"
    assert saved["email"] == "u@example.com"
    channels = {c["channel_id"]: c for c in manager.channels()}
    assert channels["qoder-cn"]["has_stored_credential"] is True
    assert channels["qoder-cn"]["account"] == "u@example.com"
    await manager.stop()


@pytest.mark.asyncio
async def test_resolver_prefers_stored_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """解析器：自存凭据优先；无自存时回落 Magpie 文件；两者皆无判未就绪。"""
    from harness.modules.gateway.config import ChannelConfig
    from harness.modules.gateway.credential_store import GatewayCredentialStore
    from harness.modules.gateway.credentials import CredentialResolver, QoderDeviceTokenManager

    store = GatewayCredentialStore(FakeDB())
    store.ensure_schema()
    store.save("qoder-cn", {"access": "dt-stored", "machine_id": "m-stored", "uid": "u-stored"})

    magpie = tmp_path / "plugin-auth.json"
    magpie.write_text(
        json.dumps(
            {"qoder-cn": {"access": "dt-magpie", "machineId": "m-magpie", "uid": "u-magpie"}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(QoderDeviceTokenManager, "path", magpie)

    channel = ChannelConfig(
        id="qoder-cn", base_url="https://x", auth="qoder-device", models=["qfmodel"]
    )
    resolver = CredentialResolver(store=store)
    assert resolver.present(channel) is True
    creds = resolver.resolve(channel)
    assert creds[0].secret == "dt-stored"
    assert creds[0].meta["machine_id"] == "m-stored"

    store.delete("qoder-cn")
    creds = resolver.resolve(channel)
    assert creds[0].secret == "dt-magpie"  # 兜底仍可用
    assert creds[0].meta["machine_id"] == "m-magpie"

    magpie.unlink()
    assert resolver.present(channel) is False

