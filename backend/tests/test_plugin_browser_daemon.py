"""browser_daemon 插件测试 —— CLI 定位 / 探测归因 / 拉起决策 / 工具输出契约。

真实 bsk 与 daemon 不参与：全部通过 mock（subproc.run / Popen / find_cli）
验证确定性逻辑，保证 CI 无环境依赖。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harness.infra.subproc import SubprocResult
from harness.kernel.services import ServiceRegistry
from plugins.browser_daemon.main import BrowserDaemonService, BrowserDaemonStatusTool

DAEMON_JSON = json.dumps(
    {
        "daemon_version": "0.3.3",
        "protocol_version": "1.3",
        "pid": 4242,
        "browsers": [
            {
                "instance_id": "abc",
                "browser_name": "chrome",
                "extension_version": "0.3.3",
                "extension_protocol_version": "1.3",
                "version_skew": False,
                "unresponsive": False,
            }
        ],
        "sessions": [],
    }
)

ABSENT_JSON = json.dumps({"code": None, "message": "automatic daemon startup is disabled", "exit_code": 2})


def _service(tmp_path: Path) -> BrowserDaemonService:
    return BrowserDaemonService(tmp_path / "browser-daemon.log")


def _tool(service: BrowserDaemonService) -> BrowserDaemonStatusTool:
    reg = ServiceRegistry()
    reg.register(BrowserDaemonService, service, owner="test")
    return BrowserDaemonStatusTool(reg)


class TestFindCli:
    def test_path_hit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BSK_INSTALL_DIR", "")
        for name in ("bsk", "bsk.exe"):
            (tmp_path / name).write_bytes(b"")
        monkeypatch.setenv("PATH", str(tmp_path))
        found = BrowserDaemonService.find_cli()
        assert found is not None and "bsk" in Path(found).name.lower()

    def test_home_fallback(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setenv("BSK_INSTALL_DIR", "")
        fake_home = tmp_path / "home"
        bin_dir = fake_home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        for name in ("bsk", "bsk.exe"):
            (bin_dir / name).write_bytes(b"")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        found = BrowserDaemonService.find_cli()
        assert found is not None

    def test_none_when_absent(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setenv("BSK_INSTALL_DIR", str(tmp_path / "none"))
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        assert BrowserDaemonService.find_cli() is None


class TestProbe:
    async def test_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run(cmd: Any, **kw: Any) -> SubprocResult:
            return SubprocResult(returncode=0, stdout=DAEMON_JSON, stderr="")

        monkeypatch.setattr("harness.infra.subproc.run", fake_run)
        result = await _service(tmp_path).probe()
        assert result["ok"] and result["status"]["daemon_version"] == "0.3.3"

    async def test_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run(cmd: Any, **kw: Any) -> SubprocResult:
            return SubprocResult(returncode=2, stdout=ABSENT_JSON, stderr="")

        monkeypatch.setattr("harness.infra.subproc.run", fake_run)
        result = await _service(tmp_path).probe()
        assert not result["ok"] and result["error"] == "daemon_absent"

    async def test_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run(cmd: Any, **kw: Any) -> SubprocResult:
            return SubprocResult(returncode=-1, stdout="", stderr="", timed_out=True)

        monkeypatch.setattr("harness.infra.subproc.run", fake_run)
        result = await _service(tmp_path).probe()
        assert not result["ok"] and result["error"] == "probe_timeout"

    async def test_cli_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(BrowserDaemonService, "find_cli", staticmethod(lambda: None))
        result = await _service(tmp_path).probe()
        assert not result["ok"] and result["error"] == "cli_not_found"


class TestEnsureRunning:
    async def test_reuse_existing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run(cmd: Any, **kw: Any) -> SubprocResult:
            return SubprocResult(returncode=0, stdout=DAEMON_JSON, stderr="")

        monkeypatch.setattr("harness.infra.subproc.run", fake_run)
        svc = _service(tmp_path)
        result = await svc.ensure_running()
        assert result["state"] == "reused"
        assert not svc.started_by_us

    async def test_start_and_confirm(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        async def fake_run(cmd: Any, **kw: Any) -> SubprocResult:
            calls["n"] += 1
            # 第一次 probe 失败，ensure 轮询期第二次成功
            if calls["n"] == 1:
                return SubprocResult(returncode=2, stdout=ABSENT_JSON, stderr="")
            return SubprocResult(returncode=0, stdout=DAEMON_JSON, stderr="")

        class FakeProc:
            pid = 9999

        popened: dict[str, Any] = {}

        def fake_popen(cmd: Any, **kw: Any) -> FakeProc:
            popened["cmd"] = cmd
            popened["kw"] = kw
            return FakeProc()

        monkeypatch.setattr("harness.infra.subproc.run", fake_run)
        monkeypatch.setattr("plugins.browser_daemon.main.subprocess.Popen", fake_popen)
        monkeypatch.setattr("plugins.browser_daemon.main.asyncio.sleep", _fast_sleep)
        svc = _service(tmp_path)
        result = await svc.ensure_running()
        assert result["state"] == "started" and result["pid"] == 9999
        assert svc.started_by_us
        assert "daemon" in popened["cmd"] and "--foreground" in popened["cmd"]
        assert popened["kw"]["env"]["BSK_AUTO_START"] == "0"

    async def test_probe_timeout_not_restarted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run(cmd: Any, **kw: Any) -> SubprocResult:
            return SubprocResult(returncode=-1, stdout="", stderr="", timed_out=True)

        monkeypatch.setattr("harness.infra.subproc.run", fake_run)
        svc = _service(tmp_path)
        result = await svc.ensure_running()
        # 探测超时不能证明 daemon 不存在，绝不盲目再拉
        assert result["state"] == "probe_timeout" and not svc.started_by_us

    async def test_start_unconfirmed_brings_log_tail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run(cmd: Any, **kw: Any) -> SubprocResult:
            return SubprocResult(returncode=2, stdout=ABSENT_JSON, stderr="")

        class FakeProc:
            pid = 1

        def fake_popen(cmd: Any, **kw: Any) -> FakeProc:
            return FakeProc()

        monkeypatch.setattr("harness.infra.subproc.run", fake_run)
        monkeypatch.setattr("plugins.browser_daemon.main.subprocess.Popen", fake_popen)
        monkeypatch.setattr("plugins.browser_daemon.main.asyncio.sleep", _fast_sleep)
        log = tmp_path / "browser-daemon.log"
        log.write_text("daemon lock held by another process", encoding="utf-8")
        result = await BrowserDaemonService(log).ensure_running(wait_secs=1)
        assert result["state"] == "start_unconfirmed"
        assert "lock" in result["log_tail"]


class TestToolContract:
    async def test_ready_with_browser(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run(cmd: Any, **kw: Any) -> SubprocResult:
            return SubprocResult(returncode=0, stdout=DAEMON_JSON, stderr="")

        monkeypatch.setattr("harness.infra.subproc.run", fake_run)
        data = json.loads(await _tool(_service(tmp_path)).execute({"action": "ensure"}))
        assert data["ready"] is True
        assert data["browser_instances"][0]["instance_id"] == "abc"
        assert "session start" in data["next"]

    async def test_daemon_ready_no_browser(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        no_browser = json.loads(DAEMON_JSON)
        no_browser["browsers"] = []

        async def fake_run(cmd: Any, **kw: Any) -> SubprocResult:
            return SubprocResult(returncode=0, stdout=json.dumps(no_browser), stderr="")

        monkeypatch.setattr("harness.infra.subproc.run", fake_run)
        data = json.loads(await _tool(_service(tmp_path)).execute({}))
        assert data["ready"] is False
        assert "扩展" in data["next"] or "浏览器" in data["next"]

    async def test_cli_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(BrowserDaemonService, "find_cli", staticmethod(lambda: None))
        data = json.loads(await _tool(_service(tmp_path)).execute({"action": "ensure"}))
        assert data["state"] == "cli_missing" and data["ready"] is False
        assert "安装" in data["next"]

    async def test_plugin_unavailable(self) -> None:
        data = json.loads(await BrowserDaemonStatusTool(ServiceRegistry()).execute({}))
        assert data["state"] == "plugin_unavailable"


_REAL_SLEEP = None  # 绑定真实 asyncio.sleep，避免被 monkeypatch 递归替换
import asyncio as _asyncio  # noqa: E402

_REAL_SLEEP = _asyncio.sleep


async def _fast_sleep(_secs: float) -> None:
    """加速轮询等待（不真的睡 1 秒）。"""
    await _REAL_SLEEP(0)
