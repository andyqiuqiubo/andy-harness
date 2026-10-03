"""桌面壳（Tauri）测试：启动器解析 / 端到端拉起后端 / 配置一致性（E11）。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP = REPO_ROOT / "desktop"
SRC_TAURI = DESKTOP / "src-tauri"
LAUNCHER = DESKTOP / "launcher" / "launch-backend.py"


def _run_launcher(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LAUNCHER), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


class TestLauncherCheck:
    """--check 解析与校验。"""

    def test_check_resolves_repo(self) -> None:
        result = _run_launcher("--check")
        assert result.returncode == 0, result.stderr
        config = json.loads(result.stdout)
        assert Path(config["python"]).exists()
        assert Path(config["backend_dir"]).is_dir()
        assert config["problems"] == []
        assert config["host"] == "127.0.0.1"
        assert config["port"] == 8000

    def test_check_port_and_data_dir(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = _run_launcher("--check", "--port", "8765", "--data-dir", tmp)
            assert result.returncode == 0
            config = json.loads(result.stdout)
            assert config["port"] == 8765
            assert Path(config["data_dir"]) == Path(tmp).resolve()
            assert config["url"] == "http://127.0.0.1:8765"

    def test_check_reports_missing_python(self) -> None:
        result = _run_launcher("--check", "--python", "Z:/nope/python.exe")
        assert result.returncode == 1
        config = json.loads(result.stdout)
        assert config["problems"]
        assert any("解释器不存在" in p for p in config["problems"])


class TestTauriConfig:
    """Tauri 配置 / 能力 / Cargo 清单的一致性。"""

    def test_tauri_conf_is_valid_and_grounded(self) -> None:
        conf = json.loads((SRC_TAURI / "tauri.conf.json").read_text("utf-8"))
        # 标识符合法（反向域名）
        assert conf["identifier"] == "work.andy.harness"
        # 前端产物路径存在（pnpm build 后）
        dist = (SRC_TAURI / conf["build"]["frontendDist"]).resolve()
        assert dist.is_dir()
        assert (dist / "index.html").exists()
        # devUrl 与 Vite 端口一致
        assert conf["build"]["devUrl"] == "http://localhost:5173"
        # 主窗口标签
        assert conf["app"]["windows"][0]["label"] == "main"
        # 图标文件齐全
        for icon in conf["bundle"]["icon"]:
            assert (SRC_TAURI / icon).is_file(), icon
        # 构建前命令会先构建前端
        assert "pnpm" in conf["build"]["beforeBuildCommand"]

    def test_capabilities_match_window(self) -> None:
        caps = json.loads((SRC_TAURI / "capabilities" / "default.json").read_text("utf-8"))
        assert caps["windows"] == ["main"]
        perms = caps["permissions"]
        assert "core:default" in perms

    def test_cargo_manifest(self) -> None:
        cargo = tomllib.loads((SRC_TAURI / "Cargo.toml").read_text("utf-8"))
        assert cargo["package"]["name"] == "andy-harness-gy"
        assert cargo["lib"]["name"] == "andy_harness_gy_lib"
        assert str(cargo["build-dependencies"]["tauri-build"]["version"]) == "2"
        assert str(cargo["dependencies"]["tauri"]["version"]) == "2"

    def test_rust_command_matches_frontend_invoke(self) -> None:
        lib = (SRC_TAURI / "src" / "lib.rs").read_text("utf-8")
        # Rust 注册的命令名必须与前端 invoke 一致
        assert "get_backend_url" in lib
        # 退出时终止后端进程树
        assert "taskkill" in lib
        assert "RunEvent::Exit" in lib

    def test_exit_handler_takes_child_via_mutex(self) -> None:
        """退出回调必须经 Mutex 取子进程句柄（编译期回归的静态防线）。

        `tauri::State` 只提供共享借用（`Deref`，没有 `DerefMut`），直接写
        `state.0.take()` 会报 `error[E0596]`、壳根本编译不过。E11 落地时
        因本机缺少 Rust 工具链而漏检，2026-09-30 首次真实编译才暴露。
        """
        lib = (SRC_TAURI / "src" / "lib.rs").read_text("utf-8")
        # 先剥掉行注释 / 文档注释，避免被说明文字里的示例误伤
        code = "\n".join(line for line in lib.splitlines() if not line.lstrip().startswith("//"))
        assert "Mutex<Option<Child>>" in code
        assert "state.0.lock()" in code
        assert "state.0.take()" not in code


@pytest.mark.skipif(
    os.environ.get("HARNESS_SHELL_CARGO_CHECK") != "1",
    reason="编译壳较慢：设 HARNESS_SHELL_CARGO_CHECK=1 且本机有 Rust 工具链时才运行",
)
@pytest.mark.skipif(shutil.which("cargo") is None, reason="未找到 cargo")
class TestShellCompiles:
    """编译期回归：`cargo check` 真正编译桌面壳（默认跳过）。"""

    def test_cargo_check_compiles(self) -> None:
        cargo = shutil.which("cargo") or "cargo"
        env = dict(os.environ)
        cargo_home = os.environ.get("CARGO_HOME")
        if cargo_home:
            env["PATH"] = str(Path(cargo_home) / "bin") + os.pathsep + env.get("PATH", "")
        result = subprocess.run(
            [cargo, "check", "--message-format", "short"],
            cwd=SRC_TAURI,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=900,
        )
        assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]


class TestLauncherEndToEnd:
    """启动器真正拉起后端，健康检查通过后整树退出。"""

    def test_launch_backend_and_stop(self) -> None:
        port = _free_port()
        import tempfile

        data_dir = tempfile.mkdtemp(prefix="ah_desktop_e2e_")
        proc = subprocess.Popen(
            [
                sys.executable,
                str(LAUNCHER),
                "--port",
                str(port),
                "--data-dir",
                data_dir,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            # 轮询健康检查（首次启动需加载插件，给足时间）
            deadline = time.monotonic() + 45
            ok = False
            last_err: Exception | None = None
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                        if resp.status == 200:
                            body = json.loads(resp.read().decode())
                            if body.get("status") == "ok":
                                ok = True
                                break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    time.sleep(1)
            assert ok, f"后端未就绪: {last_err}"
            # 数据目录在指定临时目录，而非仓库默认位置
            assert Path(data_dir, "harness.db").exists()
        finally:
            # 整棵树终止（启动器下还挂着 uvicorn）
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

        # 进程已退出
        assert proc.poll() is not None
