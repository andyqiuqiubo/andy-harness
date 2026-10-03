"""沙箱管理测试。"""

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

from harness.modules.sandbox_manager.service import (
    LocalSubprocessBackend,
    SandboxServiceImpl,
)


@pytest.fixture
def sandbox() -> SandboxServiceImpl:
    """创建沙箱服务。"""
    backend = LocalSubprocessBackend(base_workspaces_dir=str(Path(tempfile.gettempdir()) / "test_harness_sandbox"))
    return SandboxServiceImpl(backend=backend)


class TestPythonExecution:
    """Python 代码执行测试。"""

    @pytest.mark.asyncio
    async def test_simple_print(self, sandbox: SandboxServiceImpl) -> None:
        """执行简单 Python 打印。"""
        result = await sandbox.execute(
            code="print('Hello, Sandbox!')",
            language="python",
            session_id="test-1",
        )
        assert result.exit_code == 0
        assert "Hello, Sandbox!" in result.stdout
        assert not result.blocked

    @pytest.mark.asyncio
    async def test_math_calculation(self, sandbox: SandboxServiceImpl) -> None:
        """执行数学计算。"""
        result = await sandbox.execute(
            code="print(123 * 456)",
            language="python",
            session_id="test-2",
        )
        assert result.exit_code == 0
        assert "56088" in result.stdout

    @pytest.mark.asyncio
    async def test_stderr_capture(self, sandbox: SandboxServiceImpl) -> None:
        """stderr 被正确捕获。"""
        result = await sandbox.execute(
            code="import sys; sys.stderr.write('error msg')",
            language="python",
            session_id="test-3",
        )
        assert result.exit_code == 0
        assert "error msg" in result.stderr

    @pytest.mark.asyncio
    async def test_file_creation(self, sandbox: SandboxServiceImpl) -> None:
        """文件创建被检测到。"""
        # 清理可能存在的旧工作区
        from harness.modules.sandbox_manager.service import LocalSubprocessBackend

        if isinstance(sandbox._backend, LocalSubprocessBackend):
            import shutil

            ws = sandbox._backend.get_workspace("test-file-creation")
            shutil.rmtree(ws, ignore_errors=True)
        result = await sandbox.execute(
            code="open('output.txt', 'w').write('test')",
            language="python",
            session_id="test-file-creation",
        )
        assert result.exit_code == 0
        assert "output.txt" in result.files_created


class TestTimeout:
    """超时测试。"""

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, sandbox: SandboxServiceImpl) -> None:
        """超时进程被终止。"""
        result = await sandbox.execute(
            code="import time; time.sleep(60)",
            language="python",
            session_id="test-timeout",
            timeout=2,
        )
        assert result.timed_out is True
        assert result.duration_ms < 5000  # 2 秒超时 + 少量开销


class TestDangerousCommands:
    """危险命令拦截测试。"""

    @pytest.mark.asyncio
    async def test_rm_rf_blocked(self, sandbox: SandboxServiceImpl) -> None:
        """rm -rf / 被拦截。"""
        result = await sandbox.execute(
            code="rm -rf /",
            language="shell",
            session_id="test-danger-1",
        )
        assert result.blocked is True
        assert "危险命令" in result.blocked_reason

    @pytest.mark.asyncio
    async def test_fork_bomb_blocked(self, sandbox: SandboxServiceImpl) -> None:
        """fork bomb 被拦截。"""
        result = await sandbox.execute(
            code=":(){ :|:& };:",
            language="shell",
            session_id="test-danger-2",
        )
        assert result.blocked is True

    @pytest.mark.asyncio
    async def test_mkfs_blocked(self, sandbox: SandboxServiceImpl) -> None:
        """mkfs 被拦截。"""
        result = await sandbox.execute(
            code="mkfs.ext4 /dev/sda1",
            language="shell",
            session_id="test-danger-3",
        )
        assert result.blocked is True


class TestPathTraversal:
    """工作区外路径访问拦截测试。"""

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self, sandbox: SandboxServiceImpl) -> None:
        """工作区外路径访问被拦截。"""
        result = await sandbox.execute(
            code="open('/etc/passwd').read()",
            language="python",
            session_id="test-traversal-1",
        )
        assert result.blocked is True
        assert "工作区外" in result.blocked_reason


class TestSineWavePlot:
    """正弦曲线绘制测试（DoD 场景）。"""

    @pytest.mark.asyncio
    async def test_plot_sine_wave(self, sandbox: SandboxServiceImpl) -> None:
        """绘制正弦曲线，图片落工作区。"""
        # 清理可能存在的旧工作区
        from harness.modules.sandbox_manager.service import LocalSubprocessBackend

        if isinstance(sandbox._backend, LocalSubprocessBackend):
            import shutil

            ws = sandbox._backend.get_workspace("test-sine-fresh")
            shutil.rmtree(ws, ignore_errors=True)
        code = """
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

x = np.linspace(0, 2 * np.pi, 100)
y = np.sin(x)

plt.figure()
plt.plot(x, y)
plt.title('Sine Wave')
plt.xlabel('x')
plt.ylabel('sin(x)')
plt.savefig('sine_wave.png')
plt.close()
print('正弦曲线已保存')
"""
        result = await sandbox.execute(
            code=code,
            language="python",
            session_id="test-sine-fresh",
            timeout=30,
        )
        assert result.exit_code == 0
        assert "sine_wave.png" in result.files_created


class TestNoLeak:
    """资源泄漏测试。"""

    @pytest.mark.asyncio
    async def test_no_residual_processes(self, sandbox: SandboxServiceImpl) -> None:
        """连续执行 20 次后无残留。"""
        for i in range(20):
            await sandbox.execute(
                code=f"print('iteration {i}')",
                language="python",
                session_id=f"test-leak-{i}",
            )

        # 清理
        for i in range(20):
            sandbox.cleanup(f"test-leak-{i}")

        # 验证工作区已清理（使用 _workspace_exists 不创建目录）
        backend = sandbox._backend
        assert all(not backend._workspace_exists(f"test-leak-{i}") for i in range(20))


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 验证进程树终止")
class TestWindowsProcessTreeKill:
    """Windows 进程树终止验证（修复孙进程残留）。"""

    @pytest.mark.asyncio
    async def test_grandchild_killed_on_timeout(self, sandbox: SandboxServiceImpl) -> None:
        """超时后，脚本派生的孙进程也被一并终止（taskkill /T 覆盖整棵树）。"""
        assert isinstance(sandbox._backend, LocalSubprocessBackend)
        ws = Path(sandbox._backend.get_workspace("test-tree-kill"))
        # 父进程派生子进程，子进程再派生孙进程（均长时间 sleep）
        code = (
            "import subprocess, sys, os, time\n"
            "gp = subprocess.Popen([sys.executable, '-c',\n"
            '    \'import time,os; open("GRANDCHILD_PID","w").write(str(os.getpid())); time.sleep(600)\'])\n'
            "open('CHILD_PID','w').write(str(os.getpid()))\n"
            "time.sleep(600)\n"
        )
        result = await sandbox.execute(
            code=code,
            language="python",
            session_id="test-tree-kill",
            timeout=2,
        )
        assert result.timed_out is True

        grandchild_pid = (ws / "GRANDCHILD_PID").read_text(encoding="utf-8").strip()

        # 轮询确认孙进程 PID 已不存在（taskkill /T 已杀掉整棵树）
        killed = False
        for _ in range(10):
            proc = await asyncio.create_subprocess_exec(
                "tasklist",
                "/FI",
                f"PID eq {grandchild_pid}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
            if grandchild_pid not in out.decode("gbk", errors="replace"):
                killed = True
                break
            await asyncio.sleep(0.5)
        assert killed, f"孙进程 {grandchild_pid} 在超时后仍存活（进程树未彻底终止）"
