"""EventBus 单元测试。"""

import pytest

from harness.kernel.eventbus import EventBus


@pytest.mark.asyncio
async def test_subscribe_and_publish() -> None:
    """精确主题订阅与发布。"""
    bus = EventBus()
    received: list[dict] = []

    await bus.subscribe("test.topic", lambda data: received.append(data))
    await bus.publish("test.topic", {"msg": "hello"})

    assert len(received) == 1
    assert received[0]["msg"] == "hello"


@pytest.mark.asyncio
async def test_unsubscribe() -> None:
    """注销订阅后不再收到事件。"""
    bus = EventBus()
    received: list[dict] = []

    sub_id = await bus.subscribe("test.topic", lambda data: received.append(data))
    await bus.publish("test.topic", {"msg": "first"})
    assert len(received) == 1

    await bus.unsubscribe(sub_id)
    await bus.publish("test.topic", {"msg": "second"})
    assert len(received) == 1  # 仍然只有 first


@pytest.mark.asyncio
async def test_wildcard_subscription() -> None:
    """通配符主题订阅。"""
    bus = EventBus()
    received: list[str] = []

    await bus.subscribe(
        "model.*", lambda data: received.append(data.get("topic", ""))
    )
    await bus.publish("model.request", {"topic": "model.request"})
    await bus.publish("model.delta", {"topic": "model.delta"})
    await bus.publish("session.created", {"topic": "session.created"})

    assert len(received) == 2
    assert "model.request" in received
    assert "model.delta" in received


@pytest.mark.asyncio
async def test_unsubscribe_all_by_owner() -> None:
    """按 owner 批量注销订阅。"""
    bus = EventBus()
    received: list[dict] = []

    await bus.subscribe("topic.a", lambda data: received.append(data), owner="plugin-x")
    await bus.subscribe("topic.b", lambda data: received.append(data), owner="plugin-x")

    await bus.publish("topic.a", {"n": 1})
    await bus.publish("topic.b", {"n": 2})
    assert len(received) == 2

    await bus.unsubscribe_all("plugin-x")
    await bus.publish("topic.a", {"n": 3})
    await bus.publish("topic.b", {"n": 4})
    assert len(received) == 2  # 注销后不再收到


@pytest.mark.asyncio
async def test_handler_exception_isolated() -> None:
    """单个处理器异常不影响其他处理器。"""
    bus = EventBus()
    received: list[dict] = []

    async def bad_handler(data: dict) -> None:
        raise ValueError("bad handler")

    async def good_handler(data: dict) -> None:
        received.append(data)

    await bus.subscribe("test.topic", bad_handler)
    await bus.subscribe("test.topic", good_handler)

    await bus.publish("test.topic", {"msg": "hello"})

    assert len(received) == 1
