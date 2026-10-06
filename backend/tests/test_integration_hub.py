"""第三方系统集成插件（integration_hub）单元测试。

覆盖：统一注册表、飞书 API 认证与调用、飞书 CLI 检测与调用、
以及三个 Agent 工具（integration_list / integration_connect / integration_call）。

HTTP 与子进程均通过 monkeypatch 替换为本地桩，无需真实网络或飞书 CLI。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any
from unittest.mock import AsyncMock

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from plugins.integration_hub.feishu_provider import FeishuIntegration  # noqa: E402
from plugins.integration_hub.integration_base import (  # noqa: E402
    AuthResult,
    IntegrationResult,
    get_registry,
)
from plugins.integration_hub.main import (  # noqa: E402
    IntegrationCallTool,
    IntegrationConnectTool,
    IntegrationListTool,
)


@pytest.fixture
def registry():
    reg = get_registry()
    reg._providers.clear()
    yield reg
    reg._providers.clear()


@pytest.fixture
def feishu(tmp_path):
    return FeishuIntegration(state_dir=str(tmp_path))


# ── 注册表 ──────────────────────────────────────────
def test_registry_register_get(registry):
    f = FeishuIntegration(state_dir="/tmp/integration_hub_test_none")
    registry.register(f)
    assert registry.get("feishu") is f
    assert len(registry.list()) == 1
    desc = registry.list_descriptions()[0]
    assert desc["provider_id"] == "feishu"
    assert "api" in desc["supported_modes"] and "cli" in desc["supported_modes"]


def test_registry_rejects_empty_id(registry):
    class NoId:
        provider_id = ""
        provider_name = "x"
        supported_modes = []

        def list_actions(self):  # noqa: D401
            return []

        async def authenticate(self, mode, creds):  # noqa: ANN001
            return AuthResult(ok=True)

        def is_authenticated(self):
            return True

        async def call(self, action, params):  # noqa: ANN001
            return IntegrationResult(ok=True)

    with pytest.raises(ValueError):
        registry.register(NoId())  # type: ignore[arg-type]


# ── 飞书 API 接入 ───────────────────────────────────
async def test_feishu_api_auth_success(feishu, monkeypatch):
    async def fake_api(*_a, **_k):
        return {"code": 0, "msg": "ok", "tenant_access_token": "t-123", "expire": 7200}

    monkeypatch.setattr(feishu, "_api_call", fake_api)
    res = await feishu.authenticate("api", {"app_id": "a", "app_secret": "s"})
    assert res.ok
    assert feishu.is_authenticated()


async def test_feishu_api_auth_missing_creds(feishu):
    res = await feishu.authenticate("api", {})
    assert not res.ok and "app_id" in res.error


async def test_feishu_api_auth_failure_code(feishu, monkeypatch):
    async def fake_api(*_a, **_k):
        return {"code": 1, "msg": "invalid app"}

    monkeypatch.setattr(feishu, "_api_call", fake_api)
    res = await feishu.authenticate("api", {"app_id": "a", "app_secret": "s"})
    assert not res.ok


async def test_feishu_api_unsupported_mode(feishu):
    res = await feishu.authenticate("xyz", {})
    assert not res.ok


async def test_feishu_api_send_message(feishu, monkeypatch):
    async def fake_api(method, path, *, params=None, json_body=None, auth=True):
        if "tenant_access_token" in path:
            return {"code": 0, "tenant_access_token": "t-1", "expire": 7200}
        return {"code": 0, "msg": "ok", "data": {"message_id": "m1"}}

    monkeypatch.setattr(feishu, "_api_call", fake_api)
    await feishu.authenticate("api", {"app_id": "a", "app_secret": "s"})
    res = await feishu.call("send_message", {"receive_id": "c1", "content": "hi"})
    assert res.ok and res.data["message_id"] == "m1"


async def test_feishu_api_get_bot_info(feishu, monkeypatch):
    async def fake_api(method, path, *, params=None, json_body=None, auth=True):
        if "tenant_access_token" in path:
            return {"code": 0, "tenant_access_token": "t-1", "expire": 7200}
        return {"code": 0, "bot": {"name": "mybot"}}

    monkeypatch.setattr(feishu, "_api_call", fake_api)
    await feishu.authenticate("api", {"app_id": "a", "app_secret": "s"})
    res = await feishu.call("get_bot_info", {})
    assert res.ok and res.data["name"] == "mybot"


async def test_feishu_api_get_user(feishu, monkeypatch):
    async def fake_api(method, path, *, params=None, json_body=None, auth=True):
        if "tenant_access_token" in path:
            return {"code": 0, "tenant_access_token": "t-1", "expire": 7200}
        return {"code": 0, "data": {"user_id": "u1"}}

    monkeypatch.setattr(feishu, "_api_call", fake_api)
    await feishu.authenticate("api", {"app_id": "a", "app_secret": "s"})
    res = await feishu.call("get_user", {"user_id": "u1"})
    assert res.ok and res.data["user_id"] == "u1"


async def test_feishu_create_doc_with_content(feishu, monkeypatch):
    async def fake_api(method, path, *, params=None, json_body=None, auth=True):
        if "tenant_access_token" in path:
            return {"code": 0, "tenant_access_token": "t-1", "expire": 7200}
        if path == "/docx/v1/documents":
            return {"code": 0, "data": {"document": {"document_id": "d1", "title": "T"}}}
        if "/blocks/" in path:  # write_doc children
            return {"code": 0, "data": {"children": [{"block_id": "b1"}]}}
        if "/wiki/v2/spaces" in path:  # 自动定位我的文档库：返回空，跳过
            return {"code": 0, "data": {"items": []}}
        return {"code": 0, "msg": "ok"}

    monkeypatch.setattr(feishu, "_api_call", fake_api)
    await feishu.authenticate("api", {"app_id": "a", "app_secret": "s"})
    res = await feishu.call("create_doc", {"title": "andy-harness", "content": "andy"})
    assert res.ok
    assert res.data["document_id"] == "d1"
    assert res.data["write"]["ok"]


async def test_feishu_write_doc(feishu, monkeypatch):
    async def fake_api(method, path, *, params=None, json_body=None, auth=True):
        if "tenant_access_token" in path:
            return {"code": 0, "tenant_access_token": "t-1", "expire": 7200}
        if "/blocks/" in path:
            return {"code": 0, "data": {"children": [{"block_id": "b1"}]}}
        return {"code": 0, "msg": "ok"}

    monkeypatch.setattr(feishu, "_api_call", fake_api)
    await feishu.authenticate("api", {"app_id": "a", "app_secret": "s"})
    res = await feishu.call("write_doc", {"document_id": "d1", "content": "andy\nline2"})
    assert res.ok
    assert len(res.data["children"]) == 1


async def test_feishu_send_webhook(feishu, monkeypatch):
    class _Resp:
        def json(self):
            return {"code": 0, "msg": "success"}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            self.sent = (url, json)
            return _Resp()

    monkeypatch.setattr(
        __import__("plugins.integration_hub.feishu_provider", fromlist=["x"]).httpx,
        "AsyncClient",
        _Client,
    )
    await feishu.authenticate("api", {"app_id": "a", "app_secret": "s", "webhook_url": "https://hook.test/x"})
    res = await feishu.call("send_webhook", {"text": "hello"})
    assert res.ok


async def test_feishu_send_webhook_no_url(feishu):
    # 未配置 webhook_url 时直接报错（无需认证）
    res = await feishu.call("send_webhook", {"text": "hi"})
    assert not res.ok and "webhook_url" in res.error


async def test_feishu_call_requires_auth(feishu):
    res = await feishu.call("send_message", {"receive_id": "x", "content": "y"})
    assert not res.ok and "尚未认证" in res.error


async def test_feishu_unknown_action(feishu):
    feishu._auth_mode = "api"
    feishu._token = "t"
    feishu._token_expire_at = asyncio.get_event_loop().time() + 999
    res = await feishu.call("nope", {})
    assert not res.ok


# ── 飞书 CLI 接入 ───────────────────────────────────
async def test_feishu_cli_auth_success(feishu, monkeypatch):
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    res = await feishu.authenticate("cli", {})
    assert res.ok and feishu.is_authenticated()


async def test_feishu_cli_auth_no_binary(feishu, monkeypatch):
    monkeypatch.setattr(feishu, "_detect_cli", lambda: None)
    res = await feishu.authenticate("cli", {})
    assert not res.ok


async def test_feishu_cli_detects_lark_cli_first(monkeypatch):
    # 官方二进制为 lark-cli，应优先被检测到
    monkeypatch.setattr(
        "plugins.integration_hub.feishu_provider.shutil.which",
        lambda name: "/usr/local/bin/lark-cli" if name == "lark-cli" else None,
    )
    # 隔离「已知安装位置」兜底（依赖本机文件系统，非通用断言对象），聚焦 which 逻辑
    monkeypatch.setattr("plugins.integration_hub.feishu_provider.FEISHU_CLI_KNOWN_LOCATIONS", ())
    f = FeishuIntegration(state_dir="/tmp/integration_hub_test_none")
    assert f._detect_cli() == "/usr/local/bin/lark-cli"


async def test_feishu_cli_raw(feishu, monkeypatch):
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {})
    feishu._run_cli = AsyncMock(return_value=(0, "v1.0", ""))
    res = await feishu.call("raw", {"args": ["--version"]})
    assert res.ok and res.data["stdout"] == "v1.0"


async def test_feishu_cli_send(feishu, monkeypatch):
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {})
    feishu._run_cli = AsyncMock(return_value=(0, "sent", ""))
    res = await feishu.call("send_message", {"chat_id": "oc_1", "text": "hi"})
    assert res.ok
    args = feishu._run_cli.call_args[0][0]
    assert "send-message" in args and "--chat-id" in args and "hi" in args


async def test_feishu_cli_auth_ignores_app_id(feishu, monkeypatch):
    # CLI 模式不依赖 app_id/app_secret（那是应用身份 / API 模式的凭证）
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    res = await feishu.authenticate("cli", {"app_id": "a", "app_secret": "s"})
    assert res.ok and feishu.is_authenticated()


async def test_feishu_cli_raw_with_profile_in_args(feishu, monkeypatch):
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {})
    feishu._run_cli = AsyncMock(return_value=(0, "ok", ""))
    # profile 现在作为连接期凭证；调用时直接在 args 里透传即可
    res = await feishu.call("raw", {"args": ["--profile", "bot-writer", "docs", "+create"]})
    assert res.ok
    args = feishu._run_cli.call_args[0][0]
    assert "--profile" in args and "bot-writer" in args


async def test_feishu_cli_mode_lists_raw_send(feishu, monkeypatch):
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {})
    names = {a.name for a in feishu.list_actions()}
    assert {"install", "auth_status", "send_message", "raw"} <= names
    # CLI 自举能力：身份/体检/能力说明书/接口 schema，供 Agent 查清语法后再执行
    assert {"whoami", "doctor", "skills", "schema"} <= names


async def test_feishu_cli_mode_lists_create_doc_and_get_bot_info(feishu, monkeypatch):
    """CLI 模式也应暴露 create_doc / get_bot_info（此前仅在 api 模式列出）。"""
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {})
    names = {a.name for a in feishu.list_actions()}
    assert "create_doc" in names and "get_bot_info" in names


async def test_feishu_cli_create_doc_dispatched(feishu, monkeypatch):
    """CLI 模式 create_doc 应拼出 docs +create 并默认以 user 身份落 my_library。"""
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {})
    feishu._run_cli = AsyncMock(
        return_value=(
            0,
            '{"ok":true,"identity":"user","data":{"document":{"document_id":"d123","url":"https://x/docx/d123"}}}',
            "",
        )
    )
    res = await feishu.call("create_doc", {"title": "T", "content": "C"})
    assert res.ok and res.data["document_id"] == "d123"
    assert res.data["url"] == "https://x/docx/d123"
    args = feishu._run_cli.call_args[0][0]
    assert "docs" in args and "+create" in args
    assert "--as" in args and args[args.index("--as") + 1] == "user"
    assert "--title" in args and "T" in args
    assert "--parent-position" in args and "my_library" in args


async def test_feishu_cli_get_bot_info_dispatched(feishu, monkeypatch):
    """CLI 模式 get_bot_info 应走 auth status 并解析 bot/user 双身份状态。"""
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {})
    feishu._run_cli = AsyncMock(
        return_value=(
            0,
            '{"appId":"cli_x","brand":"feishu","identities":{'
            '"bot":{"status":"ready","available":true},'
            '"user":{"status":"ready","available":true,"userName":"邱波","openId":"ou_1"}}}',
            "",
        )
    )
    res = await feishu.call("get_bot_info", {})
    assert res.ok and res.data["appId"] == "cli_x"
    args = feishu._run_cli.call_args[0][0]
    assert "auth" in args and "status" in args
    assert res.data["bot"]["status"] == "ready"
    assert res.data["user"]["userName"] == "邱波"


async def test_feishu_cli_bootstrap_actions_dispatch(feishu, monkeypatch):
    """whoami / doctor / skills / schema 应映射为对应的 lark-cli 子命令。"""
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {})
    feishu._run_cli = AsyncMock(return_value=(0, "{}", ""))

    for action, params, expected in (
        ("whoami", {}, ["whoami"]),
        ("doctor", {}, ["doctor"]),
        ("skills", {}, ["skills", "list"]),
        ("skills", {"name": "lark-doc"}, ["skills", "read", "lark-doc"]),
        (
            "skills",
            {"name": "lark-doc", "path": "references/lark-doc-xml.md"},
            ["skills", "read", "lark-doc", "references/lark-doc-xml.md"],
        ),
        ("schema", {"method": "im.messages.patch"}, ["schema", "im.messages.patch"]),
    ):
        res = await feishu.call(action, params)
        assert res.ok, f"{action} 调用失败: {res.error}"
        assert feishu._run_cli.call_args[0][0] == expected

    # schema 缺 method 应明确报错，而不是拼出错误命令
    assert not (await feishu.call("schema", {})).ok


async def test_feishu_describe_exposes_cli_path(feishu, monkeypatch):
    """前端要展示「已检测到：<绝对路径>」，故 describe 需带出 cli_path。"""
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/opt/larkcli/lark-cli")
    d = feishu.describe()
    assert d["cli_available"] is True
    assert d["cli_path"] == "/opt/larkcli/lark-cli"


async def test_feishu_cli_detect_prefers_persisted_path(tmp_path, monkeypatch):
    """CLI 常装在非 PATH 目录：持久化路径应优先于 which 的结果。"""
    persisted = tmp_path / "lark-cli"
    persisted.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        "plugins.integration_hub.feishu_provider.shutil.which",
        lambda name: "/usr/local/bin/lark-cli",  # PATH 上另有旧版本
    )
    # 隔离「已知安装位置」兜底（依赖本机文件系统，避免本机装有 F 盘 CLI 时误选真身）
    monkeypatch.setattr("plugins.integration_hub.feishu_provider.FEISHU_CLI_KNOWN_LOCATIONS", ())
    f = FeishuIntegration(state_dir=str(tmp_path / "state"))
    f._cli_binary = str(persisted)
    assert f._detect_cli() == str(persisted)

    # 持久化路径失效（文件被删）时应回落到 PATH
    persisted.unlink()
    assert f._detect_cli() == "/usr/local/bin/lark-cli"


async def test_feishu_cli_resolve_path_accepts_dir_and_file(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "lark-cli.exe").write_text("", encoding="utf-8")
    assert FeishuIntegration._resolve_cli_path(str(bin_dir)) == str(bin_dir / "lark-cli.exe")
    assert FeishuIntegration._resolve_cli_path(str(bin_dir / "lark-cli.exe")) == str(bin_dir / "lark-cli.exe")
    assert FeishuIntegration._resolve_cli_path(str(tmp_path / "nope")) is None


async def test_feishu_cli_auth_with_explicit_path(feishu, monkeypatch, tmp_path):
    """连接表单里显式指定路径：可用则采纳，不可用则明确报错（不静默回退）。"""
    target = tmp_path / "lark-cli"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(feishu, "_detect_cli", lambda: None)

    res = await feishu.authenticate("cli", {"cli_path": str(target)})
    assert res.ok and feishu._cli_binary == str(target)

    bad = FeishuIntegration(state_dir=str(tmp_path / "s2"))
    monkeypatch.setattr(bad, "_detect_cli", lambda: None)
    res2 = await bad.authenticate("cli", {"cli_path": str(tmp_path / "missing")})
    assert not res2.ok and "路径不可用" in (res2.error or "")


async def test_feishu_run_cli_defaults_to_configured_profile(feishu, monkeypatch):
    """已配置 profile 时，任何调用路径都必须带上 --profile，否则会打到别的应用。"""
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {"profile": "andy-harness"})

    captured: list[str] = []

    async def fake_exec(cmd: list[str], env: dict[str, str], stdin_data: bytes | None = None) -> tuple[int, str, str]:
        captured.extend(cmd)
        return (0, "{}", "")

    monkeypatch.setattr(feishu, "_exec_cli", fake_exec)
    rc, _, _ = await feishu._run_cli(["whoami"])
    assert rc == 0
    assert captured == ["/usr/bin/lark", "--profile", "andy-harness", "whoami"]


async def test_feishu_run_cli_retries_without_broken_profile(feishu, monkeypatch):
    """profile 在 CLI 配置里失效（not_configured）时：自动去掉 profile 降级重试。

    not_configured 发生在请求发出**之前**（CLI 配置解析阶段），重试无副作用。
    """
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {"profile": "ghost"})

    calls: list[list[str]] = []

    async def fake_exec(cmd: list[str], env: dict[str, str], stdin_data: bytes | None = None) -> tuple[int, str, str]:
        calls.append(list(cmd))
        if "--profile" in cmd:
            return (1, "", '{"error":{"subtype":"not_configured"}}')
        return (0, '{"ok":true}', "")

    monkeypatch.setattr(feishu, "_exec_cli", fake_exec)
    rc, out, err = await feishu._run_cli(["whoami"])
    assert rc == 0
    assert out == '{"ok":true}'
    assert calls[0] == ["/usr/bin/lark", "--profile", "ghost", "whoami"]
    # 第二次调用不带 --profile，且带降级提示
    assert calls[1] == ["/usr/bin/lark", "whoami"]
    assert "ghost" in err and "按默认应用执行" in err


async def test_feishu_run_cli_no_retry_for_other_errors(feishu, monkeypatch):
    """非 not_configured 失败（如业务报错）不得重试，避免写操作重复执行。"""
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {"profile": "andy-harness"})

    calls: list[list[str]] = []

    async def fake_exec(cmd: list[str], env: dict[str, str], stdin_data: bytes | None = None) -> tuple[int, str, str]:
        calls.append(list(cmd))
        return (3, "", "permission denied")

    monkeypatch.setattr(feishu, "_exec_cli", fake_exec)
    rc, _out, err = await feishu._run_cli(["docs", "+create"])
    assert rc == 3
    assert "permission denied" in err
    assert len(calls) == 1  # 只调用一次，无重试


async def test_feishu_cli_create_doc_multiline_content_via_stdin(feishu, monkeypatch):
    """多行正文改走 stdin（--content -）：避免 Windows argv 转义损坏与长度上限。"""
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {})
    captured: dict[str, Any] = {}

    async def fake_exec(cmd: list[str], env: dict[str, str], stdin_data: bytes | None = None) -> tuple[int, str, str]:
        captured["cmd"] = list(cmd)
        captured["stdin"] = stdin_data
        return (
            0,
            '{"ok":true,"identity":"user","data":{"document":{"document_id":"d1","url":"u"}}}',
            "",
        )

    monkeypatch.setattr(feishu, "_exec_cli", fake_exec)
    res = await feishu.call("create_doc", {"title": "T", "content": "第一行\n第二行\n第三行"})
    assert res.ok and res.data["document_id"] == "d1"
    args = captured["cmd"]
    assert "--content" in args and args[args.index("--content") + 1] == "-"
    assert captured["stdin"] == "第一行\n第二行\n第三行".encode()


async def test_feishu_cli_create_doc_short_content_inline(feishu, monkeypatch):
    """短正文保持 argv 直传（不引入 stdin 复杂度）。"""
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {})
    captured: dict[str, Any] = {}

    async def fake_exec(cmd: list[str], env: dict[str, str], stdin_data: bytes | None = None) -> tuple[int, str, str]:
        captured["cmd"] = list(cmd)
        captured["stdin"] = stdin_data
        return (0, '{"ok":true,"data":{"document":{"document_id":"d1"}}}', "")

    monkeypatch.setattr(feishu, "_exec_cli", fake_exec)
    res = await feishu.call("create_doc", {"title": "T", "content": "你好"})
    assert res.ok
    args = captured["cmd"]
    assert args[args.index("--content") + 1] == "你好"
    assert captured["stdin"] is None


async def test_feishu_cli_create_doc_not_configured_hint(feishu, monkeypatch):
    """创建失败且输出含 not_configured 时，错误信息必须带可操作的授权提示。"""
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {})
    feishu._run_cli = AsyncMock(return_value=(1, "", '{"error":{"subtype":"not_configured"}}'))
    res = await feishu.call("create_doc", {"title": "T"})
    assert not res.ok
    assert "auth login" in (res.error or "")


async def test_feishu_describe_includes_cli_auth_snapshot(feishu, monkeypatch):
    """describe 需带出 CLI 授权就绪状态，供设置页提示「已连接但未授权」。"""
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {})

    monkeypatch.setattr(
        feishu,
        "_cli_auth_status_sync",
        lambda: '{"appId":"cli_x","identities":{"bot":{"available":true},"user":{"available":true,"userName":"邱波"}}}',
    )
    feishu._cli_auth_cache = None  # 清 TTL 缓存，强制重算
    d = feishu.describe()
    assert d["cli_auth"]["ready"] is True
    assert "邱波" in d["cli_auth"]["summary"]

    monkeypatch.setattr(
        feishu,
        "_cli_auth_status_sync",
        lambda: '{"error":{"type":"config","subtype":"not_configured"}}',
    )
    feishu._cli_auth_cache = None
    d2 = feishu.describe()
    assert d2["cli_auth"]["ready"] is False

    # 非 CLI 模式不应带出该字段
    feishu2 = FeishuIntegration(state_dir="/tmp/integration_hub_test_none")
    assert "cli_auth" not in feishu2.describe()


async def test_feishu_describe_includes_install_and_instructions(feishu, monkeypatch):
    # CLI 未安装：cli_available=False（前端据此在 CLI 页签直接提示，避免填完表单才报错）
    monkeypatch.setattr(feishu, "_detect_cli", lambda: None)
    d = feishu.describe()
    assert d["cli_install_command"].startswith("npx")
    assert "cli" in d["auth_instructions"] and "api" in d["auth_instructions"]
    # mcp 模式已摘除：官方 CLI 无 mcp 子命令，点了必然失败（详见 feishu_provider 注释）
    assert "mcp" not in d["supported_modes"]
    assert "mcp_registration" not in d
    assert "channel" in d["supported_modes"]
    assert d["channel_capabilities"]
    assert d["cli_available"] is False

    # CLI 已安装：cli_available=True
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    assert feishu.describe()["cli_available"] is True


async def test_feishu_cli_nonzero_exit(feishu, monkeypatch):
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {})
    feishu._run_cli = AsyncMock(return_value=(2, "", "error"))
    res = await feishu.call("raw", {"args": ["bad"]})
    assert not res.ok


async def test_feishu_mcp_mode_removed(feishu):
    """mcp 模式已摘除：官方 CLI 无 mcp 子命令，必须明确拒绝而不是半途失败。"""
    assert "mcp" not in feishu.supported_modes
    res = await feishu.authenticate("mcp", {})
    assert not res.ok
    assert "不支持的接入方式" in (res.error or "")


# ── 飞书 Channel 入站 ───────────────────────────────
from plugins.integration_hub.feishu_channel import (  # noqa: E402
    normalize_message,
)


def test_channel_normalize_text_from_content_string():
    event = {
        "message": {"content": '{"text":"hello"}', "chat_id": "oc_1", "message_id": "m1"},
        "sender": {"sender_id": {"open_id": "ou_1"}},
    }
    text, ctx = normalize_message(event)
    assert text == "hello"
    assert ctx["chat_id"] == "oc_1"
    assert ctx["sender"]["sender_id"]["open_id"] == "ou_1"


def test_channel_normalize_empty_when_no_text():
    text, _ = normalize_message({"message": {"content": "{}"}})
    assert text == ""


async def test_channel_start_without_sdk(feishu, monkeypatch):
    # 未安装 SDK：优雅降级，返回明确提示而非崩溃
    # （lark-oapi 可能已安装，故显式让 _import_sdk 抛 ImportError 来模拟缺失）
    def _raise_import_error() -> Any:
        raise ImportError("No module named 'lark_oapi'")

    monkeypatch.setattr(feishu._channel, "_import_sdk", _raise_import_error)
    feishu.set_message_handler(lambda text, ctx: text)
    res = await feishu.start_channel({"app_id": "a", "app_secret": "s"})
    assert not res.ok
    assert "SDK" in (res.error or "")


async def test_channel_start_missing_creds(feishu):
    res = await feishu.start_channel({})
    assert not res.ok


async def test_channel_start_installs_dedicated_loop(feishu, monkeypatch):
    """SDK 的 ws 模块 loop 绝不能是主循环（否则子线程 start() 会报 loop already running）。

    同时校验：事件回调已注册、凭证已注入 provider（供 reply_message 换 token）。
    """
    import asyncio as _asyncio
    import types as _types

    builder = _types.SimpleNamespace(handler=None)

    def _register(f: Any) -> Any:
        builder.handler = f
        return builder

    builder.register_p2_im_message_receive_v1 = _register  # type: ignore[attr-defined]
    builder.build = lambda: "EVENT_HANDLER"  # type: ignore[attr-defined]

    created: dict[str, Any] = {}

    class _StubClient:
        def __init__(self, app_id: str, app_secret: str, **kwargs: Any) -> None:
            created["app_id"] = app_id
            created["kwargs"] = kwargs

        def start(self) -> None:
            created["started"] = True

    async def _no_bot(app_id: str, app_secret: str) -> None:
        return None

    sdk = _types.SimpleNamespace(
        LogLevel=_types.SimpleNamespace(INFO="INFO"),
        EventDispatcherHandler=_types.SimpleNamespace(builder=lambda a, b: builder),
        ws=_types.SimpleNamespace(Client=_StubClient),
    )
    monkeypatch.setattr(feishu._channel, "_import_sdk", lambda: sdk)
    monkeypatch.setattr(feishu._channel, "_fetch_bot_open_id", _no_bot)
    feishu.set_message_handler(lambda text, ctx: text)

    res = await feishu.start_channel({"app_id": "a", "app_secret": "s"})
    assert res.ok, res.error
    # im.message.receive_v1 回调已注册
    assert builder.handler is not None
    # 专用 loop 必须存在且不等于主循环
    ws_loop = feishu._channel._ws_loop
    assert ws_loop is not None
    assert ws_loop is not _asyncio.get_running_loop()
    # 凭证注入 provider，reply_message 才能换 token
    assert feishu._app_id == "a"

    await feishu.stop_channel()
    assert feishu._channel.status()["running"] is False


# ── Agent 工具 ──────────────────────────────────────
async def test_tool_list_and_call(registry, feishu, monkeypatch):
    registry.register(feishu)
    monkeypatch.setattr(feishu, "_detect_cli", lambda: "/usr/bin/lark")
    await feishu.authenticate("cli", {})
    feishu._run_cli = AsyncMock(return_value=(0, "v", ""))

    out = await IntegrationListTool().execute({})
    data = json.loads(out)
    assert any(p["provider_id"] == "feishu" for p in data)

    out = await IntegrationCallTool().execute(
        {"provider": "feishu", "action": "raw", "params": {"args": ["--version"]}}
    )
    assert json.loads(out)["ok"] is True


async def test_tool_connect_unknown_provider(registry, feishu):
    registry.register(feishu)
    out = await IntegrationConnectTool().execute({"provider": "nope", "mode": "api"})
    assert json.loads(out)["ok"] is False


async def test_tool_call_unknown_provider(registry):
    out = await IntegrationCallTool().execute({"provider": "nope", "action": "raw"})
    assert json.loads(out)["ok"] is False


# ── 飞书渠道（channel）的 provider 解析 ─────────────
async def test_channel_provider_resolution_prefers_default_model(monkeypatch, tmp_path):
    """channel 模式解析 provider：default_model 命中的 provider 优先，模型随设置。

    回归场景：旧实现 import 了不存在的模块（model_providers.service）并调用了
    不存在的 get_default_provider()，导致飞书渠道永远回复「未配置模型 Provider」。
    """
    from harness.kernel.services import ServiceRegistry
    from harness.modules.model_manager.provider_registry import ProviderRegistry
    from plugins.integration_hub.main import _resolve_channel_provider_and_model

    # 设置文件指向临时目录：default_model 命中第二个 provider 的模型表
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"default_model": "model-b1"}), encoding="utf-8")
    monkeypatch.setattr("harness.api.rest.settings._SETTINGS_FILE", settings_file)

    class _P1:
        base_url = "https://p1"
        default_models = ["model-a1", "model-a2"]

        def __init__(self, api_key: str = "", base_url: str | None = None, models=None, extra_params=None):
            self.models = models or list(self.default_models)

    class _P2:
        base_url = "https://p2"
        default_models = ["model-b1"]

        def __init__(self, api_key: str = "", base_url: str | None = None, models=None, extra_params=None):
            self.models = models or list(self.default_models)

    reg = ProviderRegistry()
    reg.register_provider("p1", _P1, {"api_key": "k", "enabled": True})
    reg.register_provider("p2", _P2, {"api_key": "k", "enabled": True})

    services = ServiceRegistry()
    services.register(ProviderRegistry, reg, owner="test")

    provider, model = await _resolve_channel_provider_and_model(services)
    assert model == "model-b1"
    # get_provider 返回的是实例缓存，能用 provider_id 对应的类推断
    assert provider is not None

    # 禁用的 provider 应被跳过
    reg.register_provider("p3", _P1, {"api_key": "k", "enabled": False})
    provider2, model2 = await _resolve_channel_provider_and_model(services)
    assert provider2 is not None and model2 == "model-b1"


async def test_channel_provider_resolution_falls_back_to_first_enabled(monkeypatch):
    """无 default_model（或未命中）时回落到第一个启用 provider 的首个模型。"""
    from types import SimpleNamespace

    from harness.kernel.services import ServiceRegistry
    from harness.modules.model_manager.provider_registry import ProviderRegistry
    from plugins.integration_hub.main import _resolve_channel_provider_and_model

    monkeypatch.setattr(
        "harness.api.rest.settings._SETTINGS_FILE",
        SimpleNamespace(exists=lambda: False),
    )

    class _P1:
        base_url = "https://p1"
        default_models = ["model-a1"]

        def __init__(self, api_key: str = "", base_url: str | None = None, models=None, extra_params=None):
            self.models = models or list(self.default_models)

    reg = ProviderRegistry()
    reg.register_provider("p1", _P1, {"api_key": "k", "enabled": True})

    services = ServiceRegistry()
    services.register(ProviderRegistry, reg, owner="test")

    provider, model = await _resolve_channel_provider_and_model(services)
    assert provider is not None and model == "model-a1"
