"""HookManager 单元测试。"""

import pytest

from harness.kernel.contracts.hook import HookContext, HookResult
from harness.kernel.hooks import HookManager


@pytest.mark.asyncio
async def test_single_hook_executes() -> None:
    """单个钩子执行。"""
    mgr = HookManager()
    called: list[bool] = []

    async def handler(ctx: HookContext) -> HookResult:
        called.append(True)
        return HookResult(data=ctx.data)

    mgr.register("pre_model_call", handler, owner="plugin-x")

    result = await mgr.execute(
        "pre_model_call", HookContext(hook_name="pre_model_call", data="original")
    )
    assert called == [True]
    assert result.data == "original"


@pytest.mark.asyncio
async def test_multiple_hooks_in_order() -> None:
    """多个钩子按注册顺序执行，前一个的输出作为后一个的输入。"""
    mgr = HookManager()
    order: list[str] = []

    async def handler1(ctx: HookContext) -> HookResult:
        order.append("first")
        return HookResult(data=ctx.data + " -> first")

    async def handler2(ctx: HookContext) -> HookResult:
        order.append("second")
        return HookResult(data=ctx.data + " -> second")

    mgr.register("pre_model_call", handler1, owner="plugin-a")
    mgr.register("pre_model_call", handler2, owner="plugin-b")

    result = await mgr.execute(
        "pre_model_call", HookContext(hook_name="pre_model_call", data="start")
    )
    assert order == ["first", "second"]
    assert result.data == "start -> first -> second"


@pytest.mark.asyncio
async def test_short_circuit() -> None:
    """short_circuit=True 终止管线。"""
    mgr = HookManager()
    called: list[str] = []

    async def handler1(ctx: HookContext) -> HookResult:
        called.append("first")
        return HookResult(data=ctx.data + " -> first", short_circuit=True)

    async def handler2(ctx: HookContext) -> HookResult:
        called.append("second")
        return HookResult(data=ctx.data + " -> second")

    mgr.register("pre_model_call", handler1, owner="plugin-a")
    mgr.register("pre_model_call", handler2, owner="plugin-b")

    result = await mgr.execute(
        "pre_model_call", HookContext(hook_name="pre_model_call", data="start")
    )
    assert called == ["first"]  # second 未执行
    assert result.data == "start -> first"
    assert result.short_circuit is True


@pytest.mark.asyncio
async def test_unregister_all_by_owner() -> None:
    """按 owner 批量注销钩子。"""
    mgr = HookManager()
    called: list[str] = []

    async def handler(ctx: HookContext) -> HookResult:
        called.append("called")
        return HookResult(data=ctx.data)

    mgr.register("pre_model_call", handler, owner="plugin-a")
    mgr.register("post_model_call", handler, owner="plugin-a")

    mgr.unregister_all("plugin-a")

    await mgr.execute(
        "pre_model_call", HookContext(hook_name="pre_model_call", data="x")
    )
    await mgr.execute(
        "post_model_call", HookContext(hook_name="post_model_call", data="x")
    )
    assert called == []  # 已注销，未执行


@pytest.mark.asyncio
async def test_no_handlers_returns_data_unchanged() -> None:
    """无处理器时返回原始数据。"""
    mgr = HookManager()
    result = await mgr.execute(
        "nonexistent", HookContext(hook_name="nonexistent", data="original")
    )
    assert result.data == "original"
    assert result.short_circuit is False
