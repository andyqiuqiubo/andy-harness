"""Computer Use 插件测试。

用 FakeModelClient + FakeDesktopController 驱动 Agent 循环，覆盖：
- 配置注入优先级（环境变量覆盖默认值）
- 安全围栏：危险命令/受限目录硬性拦截、作用域约束、白名单放行、交互式授权
- 端到端：命令执行 + finish 终答、截图多模态回填、结果截断
- 终止条件：最大轮数、整体超时（用负超时确定性触发）
- 交互式人工授权续跑流程

全部无需真实桌面或网络。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from harness.kernel.context import PluginConfig
from plugins.computer_use.agent import _SYSTEM_PROMPT, ComputerUseAgent
from plugins.computer_use.client import ComputerUseModelError, FakeModelClient
from plugins.computer_use.config import ComputerUseConfig, load_computer_use_config
from plugins.computer_use.desktop import FakeDesktopController
from plugins.computer_use.main import ComputerUseService
from plugins.computer_use.safety import SafetyPolicy


def _tc(name: str, args: dict) -> dict:
    """构造一个 tool_call 结构（与 OpenAI 非流式响应一致）。"""
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def _base_config(tmp_path: Path) -> ComputerUseConfig:
    """构造一个测试用配置（白名单模式，开放 echo，开启桌面控制）。"""
    return ComputerUseConfig(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        max_iterations=10,
        overall_timeout=300.0,
        step_timeout=5.0,
        http_timeout=30.0,
        max_retries=1,
        max_image_dimension=640,
        jpeg_quality=60,
        max_output_chars=200,
        approval_mode="whitelist",
        allow_desktop=True,
        plan_first=False,  # 测试默认关闭计划阶段，避免每个用例都要补一条计划响应
        max_llm_calls=10,
        allow_commands={"echo", "type"},
        allow_paths=[],
        deny_commands=["rm -rf"],
        deny_paths=["c:/secret"],
        working_dir=str(tmp_path),
    )


# ── 配置注入 ────────────────────────────────────────
def test_config_env_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量应覆盖默认值。"""
    monkeypatch.setenv("COMPUTER_USE_MODEL", "custom-model")
    monkeypatch.setenv("COMPUTER_USE_MAX_ITERATIONS", "7")
    monkeypatch.setenv("COMPUTER_USE_ALLOW_COMMANDS", "ls,cat,git")
    cfg = load_computer_use_config(PluginConfig())
    assert cfg.model == "custom-model"
    assert cfg.max_iterations == 7
    assert cfg.allow_commands == {"ls", "cat", "git"}


def test_config_defaults_no_hardcode(monkeypatch: pytest.MonkeyPatch) -> None:
    """无环境变量时回到默认值，且 api_key 回退到 DEEPSEEK_API_KEY。"""
    for key in (
        "COMPUTER_USE_API_KEY",
        "DEEPSEEK_API_KEY",
        "COMPUTER_USE_MODEL",
        "COMPUTER_USE_MAX_ITERATIONS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fallback-key")
    cfg = load_computer_use_config(PluginConfig())
    assert cfg.api_key == "fallback-key"
    assert cfg.model == "deepseek-flash"  # 默认值（非硬编码魔法字符串散落各处）
    assert cfg.max_iterations == 15  # 默认值下调为 15（P0：缩小默认动作循环上限）
    assert cfg.max_llm_calls == 15  # 模型调用次数硬预算，未单独配置时回退到 max_iterations
    assert cfg.plan_first is True  # P1：计划先行默认开启


# ── 安全围栏 ────────────────────────────────────────
def test_safety_deny_command() -> None:
    sp = SafetyPolicy(_base_config(Path("/tmp")))
    d = sp.evaluate("command_exec", {"command": "rm -rf /"}, False)
    assert not d.allowed
    assert "危险命令" in d.reason


def test_safety_scope_block(tmp_path: Path) -> None:
    sp = SafetyPolicy(_base_config(tmp_path))
    # 读取工作目录之外的文件应被作用域拦截
    d = sp.evaluate("file_read", {"path": str(tmp_path.parent / "outside.txt")}, False)
    assert not d.allowed
    assert "越界" in d.reason


def test_safety_whitelist_allow_and_block(tmp_path: Path) -> None:
    sp = SafetyPolicy(_base_config(tmp_path))
    # 白名单内命令放行
    assert sp.evaluate("command_exec", {"command": "echo hi"}, False).allowed
    # 白名单外命令拦截
    d = sp.evaluate("command_exec", {"command": "python -c 'x=1'"}, False)
    assert not d.allowed


def test_safety_restricted_dir_deny(tmp_path: Path) -> None:
    sp = SafetyPolicy(_base_config(tmp_path))
    d = sp.evaluate("file_read", {"path": "C:/secret/secret.txt"}, False)
    assert not d.allowed
    assert "受限目录" in d.reason


def test_safety_interactive_approval(tmp_path: Path) -> None:
    cfg = dataclasses.replace(_base_config(tmp_path), approval_mode="interactive")
    sp = SafetyPolicy(cfg)
    d = sp.evaluate("command_exec", {"command": "echo hi"}, False)
    assert not d.allowed and d.requires_approval
    # 已授权后应放行
    assert sp.evaluate("command_exec", {"command": "echo hi"}, True).allowed


def test_safety_desktop_toggle(tmp_path: Path) -> None:
    sp = SafetyPolicy(_base_config(tmp_path))
    assert sp.evaluate("mouse_click", {"x": 1, "y": 1}, False).allowed
    cfg_off = dataclasses.replace(_base_config(tmp_path), allow_desktop=False)
    sp_off = SafetyPolicy(cfg_off)
    d = sp_off.evaluate("mouse_click", {"x": 1, "y": 1}, False)
    assert not d.allowed
    assert "桌面控制未开启" in d.reason


# ── 端到端：命令执行 + 终答 ─────────────────────────
async def test_agent_runs_command_and_finishes(tmp_path: Path) -> None:
    svc = ComputerUseService(_base_config(tmp_path))
    cfg = _base_config(tmp_path)
    client = FakeModelClient(
        [
            {"tool_calls": [_tc("command_exec", {"command": "echo ok > result.txt"})]},
            {"tool_calls": [_tc("finish", {"summary": "done"})]},
        ]
    )
    agent = ComputerUseAgent(cfg, SafetyPolicy(cfg), FakeDesktopController(), client, svc, False)
    summary = await agent.run("do it", "run1")
    assert "done" in summary
    assert (tmp_path / "result.txt").exists()


# ── 端到端：截图多模态回填 ───────────────────────────
async def test_agent_injects_screenshot_as_image(tmp_path: Path) -> None:
    svc = ComputerUseService(_base_config(tmp_path))
    cfg = _base_config(tmp_path)
    client = FakeModelClient(
        [
            {"tool_calls": [_tc("screenshot", {})]},
            {"tool_calls": [_tc("finish", {"summary": "shot done"})]},
        ]
    )
    desktop = FakeDesktopController()
    agent = ComputerUseAgent(cfg, SafetyPolicy(cfg), desktop, client, svc, False)
    summary = await agent.run("screenshot", "run2")
    assert "shot done" in summary
    # 真实截图被调用
    assert any(c["op"] == "screenshot" for c in desktop.calls)
    # 截图作为 user 消息中的图像被回填（多模态观察）
    image_seen = False
    for msgs in client.captured:
        for m in msgs:
            if m.get("role") == "user" and isinstance(m.get("content"), list):
                if any(p.get("type") == "image_url" for p in m["content"]):
                    image_seen = True
    assert image_seen


# ── 端到端：结果截断 ────────────────────────────────
async def test_agent_truncates_large_output(tmp_path: Path) -> None:
    svc = ComputerUseService(_base_config(tmp_path))
    cfg = _base_config(tmp_path)
    big_path = tmp_path / "big.txt"
    client = FakeModelClient(
        [
            {"tool_calls": [_tc("file_write", {"path": str(big_path), "content": "X" * 10000})]},
            {"tool_calls": [_tc("file_read", {"path": str(big_path)})]},
            {"tool_calls": [_tc("finish", {"summary": "read done"})]},
        ]
    )
    agent = ComputerUseAgent(cfg, SafetyPolicy(cfg), FakeDesktopController(), client, svc, False)
    summary = await agent.run("read big", "run3")
    assert "read done" in summary
    # 工具结果应被截断（文件读取结果中出现截断标记或长度受限）
    truncated = False
    for msgs in client.captured:
        for m in msgs:
            if m.get("role") == "tool" and isinstance(m.get("content"), str):
                if "[输出已截断" in m["content"] or len(m["content"]) <= cfg.max_output_chars + 60:
                    truncated = True
    assert truncated


# ── 终止条件：最大轮数 ──────────────────────────────
async def test_agent_max_iterations_terminates(tmp_path: Path) -> None:
    svc = ComputerUseService(_base_config(tmp_path))
    cfg = dataclasses.replace(_base_config(tmp_path), max_iterations=1, max_llm_calls=1)
    client = FakeModelClient(
        [
            {"tool_calls": [_tc("command_exec", {"command": "echo loop"})]},
            {"tool_calls": [_tc("command_exec", {"command": "echo loop"})]},
        ]
    )
    agent = ComputerUseAgent(cfg, SafetyPolicy(cfg), FakeDesktopController(), client, svc, False)
    summary = await agent.run("loop", "run4")
    assert "最大迭代" in summary or "终止" in summary


# ── 终止条件：整体超时（负超时确定性触发） ───────────
async def test_agent_overall_timeout(tmp_path: Path) -> None:
    svc = ComputerUseService(_base_config(tmp_path))
    cfg = dataclasses.replace(_base_config(tmp_path), overall_timeout=-1.0, max_iterations=100, max_llm_calls=100)
    client = FakeModelClient([{"tool_calls": [_tc("command_exec", {"command": "echo x"})]}])
    agent = ComputerUseAgent(cfg, SafetyPolicy(cfg), FakeDesktopController(), client, svc, False)
    summary = await agent.run("t", "run5")
    assert "整体超时" in summary


# ── 交互式人工授权续跑 ──────────────────────────────
async def test_interactive_approval_flow(tmp_path: Path) -> None:
    cfg = dataclasses.replace(_base_config(tmp_path), approval_mode="interactive")
    svc = ComputerUseService(cfg)
    run_id = "run6"
    client1 = FakeModelClient(
        [
            {"tool_calls": [_tc("command_exec", {"command": "echo need-approval"})]},
            {"tool_calls": [_tc("finish", {"summary": "approved-done"})]},
        ]
    )
    agent1 = ComputerUseAgent(cfg, SafetyPolicy(cfg), FakeDesktopController(), client1, svc, False)
    summary1 = await agent1.run("do", run_id)
    assert "需要人工授权" in summary1 and run_id in summary1
    assert run_id in svc.pending  # 已记录待审批

    # 人工授权后，用 resume_run_id 续跑
    svc.approve_run(run_id)
    client2 = FakeModelClient(
        [
            {"tool_calls": [_tc("command_exec", {"command": "echo need-approval"})]},
            {"tool_calls": [_tc("finish", {"summary": "approved-done"})]},
        ]
    )
    agent2 = ComputerUseAgent(cfg, SafetyPolicy(cfg), FakeDesktopController(), client2, svc, True)
    summary2 = await agent2.run("do", run_id)
    assert "approved-done" in summary2


# ── 卡死检测：连续相同操作应提前中止（不烧光轮数） ──
async def test_agent_stagnation_detection(tmp_path: Path) -> None:
    """模型反复执行完全相同的操作（如点同一个坐标）应在 stagnation_limit
    轮内提前中止，并给出明确的「卡死」提示，而非耗尽 max_iterations。"""
    svc = ComputerUseService(_base_config(tmp_path))
    # 放宽 max_iterations，证明是被卡死检测中止而非轮数耗尽
    cfg = dataclasses.replace(_base_config(tmp_path), max_iterations=20, stagnation_limit=4)
    # 脚本：每轮都点同一个坐标，且永不直接 finish
    script = [{"tool_calls": [_tc("mouse_click", {"x": 100, "y": 100})]}] * 20
    client = FakeModelClient(script)
    agent = ComputerUseAgent(cfg, SafetyPolicy(cfg), FakeDesktopController(), client, svc, False)
    summary = await agent.run("stuck", "run7")
    assert "卡死" in summary or "完全相同的操作" in summary
    # 不应耗尽到 20 轮才停：实际迭代轮数应 <= stagnation_limit 附近
    # captured 记录每轮发给模型的消息；轮数 = len(captured) - 1（首条是 system+user）
    assert len(client.captured) - 1 <= 6  # 4 轮卡死 + 收尾，留有少量余量


# ── 截图应回传屏幕分辨率，供模型推算坐标 ─────────────
async def test_agent_screenshot_reports_resolution(tmp_path: Path) -> None:
    svc = ComputerUseService(_base_config(tmp_path))
    cfg = _base_config(tmp_path)
    client = FakeModelClient(
        [
            {"tool_calls": [_tc("screenshot", {})]},
            {"tool_calls": [_tc("finish", {"summary": "seen screen"})]},
        ]
    )
    agent = ComputerUseAgent(cfg, SafetyPolicy(cfg), FakeDesktopController(), client, svc, False)
    summary = await agent.run("look", "run8")
    assert "seen screen" in summary
    # 工具结果文本应包含屏幕分辨率（FakeDesktopController 截图 2x2）
    found = False
    for msgs in client.captured:
        for m in msgs:
            if m.get("role") == "tool" and "屏幕分辨率 2x2" in str(m.get("content", "")):
                found = True
    assert found


# ── wait 工具：等待后返回，且不计入卡死检测 ──────────
async def test_agent_wait_tool(tmp_path: Path) -> None:
    svc = ComputerUseService(_base_config(tmp_path))
    cfg = _base_config(tmp_path)
    client = FakeModelClient(
        [
            {"tool_calls": [_tc("wait", {"seconds": 1})]},
            {"tool_calls": [_tc("finish", {"summary": "waited"})]},
        ]
    )
    # wait 由 agent 直接 asyncio.sleep 处理，不经过 desktop 控制器
    agent = ComputerUseAgent(cfg, SafetyPolicy(cfg), FakeDesktopController(), client, svc, False)
    summary = await agent.run("wait a bit", "run9")
    assert "waited" in summary


# ── 纠偏：模型把整段指令当输入内容时，应拦截并提示重填 ──
async def test_keyboard_type_rejects_full_instruction(tmp_path: Path) -> None:
    """回归：模型曾把『打开chrome…点击百度一下』整段指令当成搜索词填进框里。
    键盘纠偏应在 run 时（已设置 _goal）拦截该调用并返回纠正提示，且不真正打字。"""
    svc = ComputerUseService(_base_config(tmp_path))
    cfg = _base_config(tmp_path)
    desktop = FakeDesktopController()
    agent = ComputerUseAgent(cfg, SafetyPolicy(cfg), desktop, FakeModelClient([]), svc, False)
    goal = "打开本地chrome浏览器，打开百度首页，输入框输入刘德华，点击按钮：百度一下"
    agent._goal = goal
    # 把整段指令当 text 传入 —— 应被拦截
    result, image = await agent._dispatch("keyboard_type", {"text": goal})
    assert image is None
    assert "纠正" in result
    assert not any(c["op"] == "keyboard_type" for c in desktop.calls)
    # 正确的短内容应正常放行并真正打字
    result2, _ = await agent._dispatch("keyboard_type", {"text": "刘德华"})
    assert "刘德华" in result2
    assert any(c["op"] == "keyboard_type" for c in desktop.calls)


def test_system_prompt_is_app_agnostic() -> None:
    """Computer Use 是通用桌面 Agent，系统提示不得硬编码具体 App / 网站
    （如某浏览器、某搜索引擎、baidu.com）。通用护栏（完整 http(s) 网址、
    文本只填内容本身）对所有桌面任务成立即可。"""
    prompt = _SYSTEM_PROMPT
    # 不得出现具体网站/引擎绑定
    assert "baidu" not in prompt.lower()
    assert "baidu.com" not in prompt.lower()
    # 但仍须保留通用导航护栏：要求完整 http(s) 网址、禁止裸域名
    assert "http(s)://" in prompt
    assert "裸域名" in prompt


def test_system_prompt_commands_use_whitelist_first_token() -> None:
    """系统提示必须教会模型：command_exec 首词用白名单内命令本身，
    严禁 `cmd /c` 等外壳前缀（白名单按首词匹配，cmd 不在默认白名单，
    实测曾导致 `cmd /c echo ... > 文件` 被拦截、内部空烧重试轮次）。"""
    prompt = _SYSTEM_PROMPT
    assert "cmd /c" in prompt
    assert "白名单" in prompt


async def test_run_does_not_inject_browser_specific_url(tmp_path: Path) -> None:
    """执行阶段发给模型的消息里，不得出现为某网站硬编码的搜索 URL
    （如 https://www.baidu.com/s?wd=）。方法应交给模型自行决定。"""
    svc = ComputerUseService(_base_config(tmp_path))
    cfg = _base_config(tmp_path)
    desktop = FakeDesktopController()
    client = FakeModelClient([{"content": "已处理", "tool_calls": [_tc("finish", {"summary": "done"})]}])
    agent = ComputerUseAgent(cfg, SafetyPolicy(cfg), desktop, client, svc, False)
    await agent.run("从百度搜索刘德华相关信息", "run_x")
    assert client.captured, "模型至少应被调用一次"
    joined = "\n".join(str(m.get("content", "")) for call in client.captured for m in call if isinstance(m, dict))
    # 关键：不再由服务端注入 baidu 专属 URL——保持 App 无关
    assert "baidu.com/s?wd=" not in joined
    assert "https://www.baidu.com/s?wd=" not in joined


# ── 计划先行（P1）：先用一次禁用工具的调用产出步骤清单，并随结果回传 ──
async def test_plan_first_prepends_plan(tmp_path: Path) -> None:
    """P1 计划先行：plan_first=True 时先用一次『禁用工具』的调用生成步骤清单，
    该清单随最终结果回传（成本透明，用户可在执行前先看计划）。

    脚本：第 1 条被 _make_plan 消费（该次调用 tools=[]，强制只出文本计划）；
    第 2 条被执行循环消费，直接 finish。
    """
    cfg = dataclasses.replace(_base_config(tmp_path), plan_first=True)
    svc = ComputerUseService(cfg)
    client = FakeModelClient(
        [
            {
                "content": "1. start chrome\n2. ctrl+l 输网址回车\n3. 完成",
                "tool_calls": [],
            },
            {"tool_calls": [_tc("finish", {"summary": "executed"})]},
        ]
    )
    agent = ComputerUseAgent(cfg, SafetyPolicy(cfg), FakeDesktopController(), client, svc, False)
    summary = await agent.run("搜刘德华", "runp")
    # 计划应被回传并前置，执行结果其后
    assert "【执行计划】" in summary
    assert "【执行结果】" in summary
    assert "start chrome" in summary  # 计划内容确实被带回
    assert "executed" in summary  # 执行结果也在
    # 计划阶段与执行阶段应为两次独立调用（计划先行多花 1 次调用换执行更线性）
    assert len(client.captured) >= 2


# ── 计划阶段失败应优雅降级（不阻塞执行） ─────────────
async def test_plan_first_falls_back_on_error(tmp_path: Path) -> None:
    """若计划阶段调用抛错，应静默跳过规划、直接执行，不应让整个任务失败。"""
    cfg = dataclasses.replace(_base_config(tmp_path), plan_first=True)
    svc = ComputerUseService(cfg)

    class _BoomClient(FakeModelClient):
        def __init__(self) -> None:
            # 脚本只给执行阶段一条 finish，计划阶段调用会抛错（被 _make_plan 捕获）
            super().__init__([{"tool_calls": [_tc("finish", {"summary": "no-plan"})]}])
            self._plan_called = False

        async def complete(self, messages, tools, temperature=0.2):
            # 第一条（计划阶段）故意抛错（模拟模型网络/API 失败）
            if not self._plan_called:
                self._plan_called = True
                raise ComputerUseModelError("plan boom")
            return await super().complete(messages, tools, temperature)

    agent = ComputerUseAgent(cfg, SafetyPolicy(cfg), FakeDesktopController(), _BoomClient(), svc, False)
    summary = await agent.run("do without plan", "runp2")
    assert "no-plan" in summary  # 降级后仍能正常执行完成


# ── 凭据兜底：复用项目已配置的 DeepSeek provider ─────
def test_create_agent_uses_provider_resolver(tmp_path: Path) -> None:
    """api_key 为空时，应从 resolver 取项目 DeepSeek provider 的密钥。"""
    svc = ComputerUseService(
        dataclasses.replace(_base_config(tmp_path), api_key=""),
        credential_resolver=lambda: ("provider-key", "https://api.deepseek.com"),
    )
    agent = svc.create_agent("run-x", False)
    assert agent.client._api_key == "provider-key"


def test_create_agent_resolver_empty_raises(tmp_path: Path) -> None:
    """resolver 也无密钥时，仍应抛出明确的「未配置」错误。"""
    svc = ComputerUseService(
        dataclasses.replace(_base_config(tmp_path), api_key=""),
        credential_resolver=lambda: ("", ""),
    )
    with pytest.raises(ValueError, match="未配置 Computer Use 的 API Key"):
        svc.create_agent("run-y", False)


def test_create_agent_explicit_key_priority(tmp_path: Path) -> None:
    """显式配置的 api_key 应优先于 resolver。"""
    svc = ComputerUseService(
        dataclasses.replace(_base_config(tmp_path), api_key="explicit"),
        credential_resolver=lambda: ("provider-key", "https://api.deepseek.com"),
    )
    agent = svc.create_agent("run-z", False)
    assert agent.client._api_key == "explicit"
