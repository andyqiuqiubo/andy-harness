"""Docker 沙箱后端测试。

本机未安装 / 未启动 Docker，因此通过注入「命令运行器替身」验证
docker 命令构造、可用性检测、镜像白名单、超时强制清理、输出截断等全部逻辑；
真实端到端用例在检测到 docker 可用时才运行（见 TestDockerRealIntegration）。
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from harness.modules.sandbox_manager.service import (
    CommandResult,
    DockerBackend,
    SandboxServiceImpl,
)


def _make_runner(
    *,
    available: bool = True,
    run_result: CommandResult | Callable[[list[str]], CommandResult] | None = None,
) -> tuple[Callable[[list[str], float], object], list[list[str]]]:
    """构造一个记录调用的 docker 命令替身。

    返回 (runner, 调用记录)。对 ``docker info`` / ``docker rm`` 自动应答，
    对 ``docker run`` 返回 run_result（或默认成功）。
    """
    calls: list[list[str]] = []

    async def runner(args: list[str], timeout: float) -> CommandResult:
        calls.append(list(args))
        if args[:2] == ["docker", "info"]:
            if available:
                return CommandResult(0, "20.10.0\n", "")
            return CommandResult(1, "", "Cannot connect to the Docker daemon")
        if args[:2] == ["docker", "rm"]:
            return CommandResult(0, "", "")
        # docker run
        if callable(run_result):
            return run_result(args)
        if run_result is not None:
            return run_result
        return CommandResult(0, "Hello, Docker!", "")

    return runner, calls


def _backend(runner: object, **kwargs: object) -> DockerBackend:
    return DockerBackend(
        base_workspaces_dir=str(Path(tempfile.gettempdir()) / "test_harness_docker"),
        runner=runner,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


class TestBuildRunArgs:
    """docker run 参数构造。"""

    def test_python_args_defaults(self) -> None:
        """默认：禁网、资源限制、挂载、工作目录、镜像、命令齐全。"""
        runner, _ = _make_runner()
        b = _backend(runner)
        args = b._build_run_args(
            workspace="/tmp/ws",
            image="python:3.12-slim",
            command=["python", "script.py"],
            name="harness-s1-abc",
        )
        assert args[:3] == ["docker", "run", "--rm"]
        assert "--name" in args and "harness-s1-abc" in args
        assert "-v" in args and "/tmp/ws:/workspace" in args
        assert "-w" in args and "/workspace" in args
        assert "--network" in args and args[args.index("--network") + 1] == "none"
        assert "--memory" in args and args[args.index("--memory") + 1] == "512m"
        assert "--cpus" in args and args[args.index("--cpus") + 1] == "1.0"
        assert args[-2:] == ["python", "script.py"]
        assert args[args.index("--cpus") + 2] == "python:3.12-slim"

    def test_shell_command(self) -> None:
        """shell 命令以 sh -c 结尾。"""
        assert DockerBackend._shell_command("echo hi") == ["sh", "-c", "echo hi"]

    def test_network_enabled_uses_bridge(self) -> None:
        """network=True 时网络为 bridge。"""
        runner, _ = _make_runner()
        b = _backend(runner, network=True)
        args = b._build_run_args("/tmp/ws", "img", ["sh", "-c", "x"], "n")
        assert args[args.index("--network") + 1] == "bridge"

    def test_container_name_valid(self) -> None:
        """非法字符被替换为合法容器名。"""
        name = DockerBackend._container_name("s/1 中", "id12")
        assert name.startswith("harness-")
        for ch in name:
            assert ch.isalnum() or ch in "._-"


class TestExecute:
    """执行流程。"""

    @pytest.mark.asyncio
    async def test_python_success_writes_script_and_runs(self) -> None:
        """成功：script.py 写入工作区，stdout 捕获，exit 0。"""
        runner, calls = _make_runner()
        b = _backend(runner)
        b._availability = True
        svc = SandboxServiceImpl(backend=b)
        result = await svc.execute(
            code="print('Hello, Docker!')",
            language="python",
            session_id="sess-ok",
        )
        assert result.exit_code == 0
        assert "Hello, Docker!" in result.stdout
        # script.py 已落盘
        ws = Path(b.get_workspace("sess-ok"))
        assert (ws / "script.py").exists()
        # 存在一次 docker run
        run_calls = [c for c in calls if c[:2] == ["docker", "run"]]
        assert len(run_calls) == 1
        assert "python" in run_calls[0] and "script.py" in run_calls[0]

    @pytest.mark.asyncio
    async def test_exit_code_propagated(self) -> None:
        """容器非零退出码透传。"""
        runner, _ = _make_runner(run_result=CommandResult(7, "", "boom"))
        b = _backend(runner)
        b._availability = True
        result = await b.execute("x", "python", b.get_workspace("s"))
        assert result.exit_code == 7
        assert "boom" in result.stderr

    @pytest.mark.asyncio
    async def test_unavailable_blocked(self) -> None:
        """docker 不可用：blocked 并给出明确原因。"""
        runner, _ = _make_runner(available=False)
        b = _backend(runner)
        svc = SandboxServiceImpl(backend=b)
        result = await svc.execute("print(1)", "python", "sess-no-docker")
        assert result.blocked is True
        assert "Docker 不可用" in result.blocked_reason

    @pytest.mark.asyncio
    async def test_timeout_force_removes_container(self) -> None:
        """超时：标记 timed_out 并强制 docker rm -f 容器。"""
        runner, calls = _make_runner(run_result=CommandResult(-1, "", "", timed_out=True))
        b = _backend(runner)
        b._availability = True
        result = await b.execute(
            "import time; time.sleep(600)",
            "python",
            b.get_workspace("sess-timeout"),
            timeout=2,
        )
        assert result.timed_out is True
        rm_calls = [c for c in calls if c[:2] == ["docker", "rm"]]
        assert rm_calls and rm_calls[0][:3] == ["docker", "rm", "-f"]

    @pytest.mark.asyncio
    async def test_image_not_whitelisted_blocked(self) -> None:
        """镜像不在白名单：拦截。"""
        runner, _ = _make_runner()
        b = _backend(runner, allowed_images=["other:1.0"])
        b._availability = True
        result = await b.execute("print(1)", "python", b.get_workspace("s"))
        assert result.blocked is True
        assert "白名单" in result.blocked_reason

    @pytest.mark.asyncio
    async def test_image_whitelisted_runs(self) -> None:
        """镜像在白名单：正常执行。"""
        runner, _ = _make_runner()
        b = _backend(runner, allowed_images=["python:3.12-slim"])
        b._availability = True
        result = await b.execute("print(1)", "python", b.get_workspace("s"))
        assert result.blocked is False

    @pytest.mark.asyncio
    async def test_dangerous_command_blocked(self) -> None:
        """危险命令在容器外即被拦截。"""
        runner, _ = _make_runner()
        b = _backend(runner)
        result = await b.execute("rm -rf /", "shell", b.get_workspace("s"))
        assert result.blocked is True

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self) -> None:
        """路径遍历在容器外即被拦截。"""
        runner, _ = _make_runner()
        b = _backend(runner)
        result = await b.execute(
            "print(open('../../etc/passwd').read())",
            "python",
            b.get_workspace("s"),
        )
        assert result.blocked is True

    @pytest.mark.asyncio
    async def test_unsupported_language(self) -> None:
        """不支持的语言返回错误。"""
        runner, _ = _make_runner()
        b = _backend(runner)
        b._availability = True
        result = await b.execute("x", "ruby", b.get_workspace("s"))
        assert result.exit_code == -1
        assert "不支持的语言" in result.stderr

    @pytest.mark.asyncio
    async def test_output_truncated(self) -> None:
        """超长输出被截断。"""
        big = "x" * 500
        runner, _ = _make_runner(run_result=CommandResult(0, big, ""))
        b = _backend(runner)
        b._availability = True
        result = await b.execute(
            "x",
            "python",
            b.get_workspace("s"),
            max_output=50,
        )
        assert "输出已截断" in result.stdout
        assert len(result.stdout) < 100

    @pytest.mark.asyncio
    async def test_shell_executes_sh(self) -> None:
        """shell 执行以 sh -c 调用。"""
        runner, calls = _make_runner()
        b = _backend(runner)
        b._availability = True
        result = await b.execute("echo hello", "shell", b.get_workspace("s"))
        assert result.exit_code == 0
        run_calls = [c for c in calls if c[:2] == ["docker", "run"]]
        assert run_calls[0][-3:] == ["sh", "-c", "echo hello"]


class TestAvailabilityAndCleanup:
    """可用性检测与清理。"""

    @pytest.mark.asyncio
    async def test_availability_detected_and_cached(self) -> None:
        """可用时返回 True，且只检测一次（缓存）。"""
        runner, calls = _make_runner(available=True)
        b = _backend(runner)
        assert await b.is_available() is True
        assert await b.is_available() is True
        info_calls = [c for c in calls if c[:2] == ["docker", "info"]]
        assert len(info_calls) == 1

    @pytest.mark.asyncio
    async def test_availability_false(self) -> None:
        """守护进程不可用返回 False。"""
        runner, _ = _make_runner(available=False)
        b = _backend(runner)
        assert await b.is_available() is False

    def test_get_workspace_and_cleanup(self) -> None:
        """工作区创建与清理。"""
        runner, _ = _make_runner()
        b = _backend(runner)
        ws = Path(b.get_workspace("sess-cleanup"))
        (ws / "f.txt").write_text("x", encoding="utf-8")
        assert ws.exists()
        b.cleanup_workspace("sess-cleanup")
        assert not ws.exists()


def _docker_real_available() -> bool:
    """探测真实 docker 是否可用（用于端到端集成测试）。"""
    import subprocess

    try:
        return (
            subprocess.run(
                ["docker", "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(
    not _docker_real_available(),
    reason="仅在真实 docker 可用时运行端到端集成",
)
class TestDockerRealIntegration:
    """真实 docker 端到端（无 docker 时整体跳过）。"""

    @pytest.mark.asyncio
    async def test_real_python_run(self) -> None:
        """在真实容器中打印并写文件。"""
        b = DockerBackend(
            base_workspaces_dir=str(Path(tempfile.gettempdir()) / "test_harness_docker_real"),
        )
        result = await b.execute(
            "print('real'); open('out.txt','w').write('ok')",
            "python",
            b.get_workspace("real-sess"),
            timeout=60,
        )
        assert result.exit_code == 0
        assert "real" in result.stdout
        assert "out.txt" in result.files_created
        b.cleanup_workspace("real-sess")
