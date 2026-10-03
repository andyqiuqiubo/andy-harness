"""SandboxService —— 沙箱管理服务。

LocalSubprocessBackend: 子进程 + 超时 + 输出截断 + 平台适配的内存/CPU 限制 + 危险命令黑名单。
DockerBackend: 一次性容器（docker run --rm）+ bind mount 工作区 + 禁网 + 镜像白名单 + 资源限制。
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib
import importlib.util
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from harness.kernel.eventbus import EventBus

logger = logging.getLogger("harness.sandbox")

# 危险命令黑名单（正则匹配）
DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\s+/",
    r"\brm\s+-rf\s+~",
    r"\bmkfs\b",
    r"\bdd\b.*\bof=/dev/",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r":\(\)\{.*\};:",  # fork bomb
    r"\b>\s*/dev/sda",
]

# 默认超时（秒）
DEFAULT_TIMEOUT = 30

# 默认输出截断长度
DEFAULT_MAX_OUTPUT = 10000  # 字符

# Windows Job Object 资源限制：默认每进程内存上限（512MB），超限即被系统杀掉。
WINDOWS_JOB_MEMORY_LIMIT = 512 * 1024 * 1024

# Docker 后端默认值
DEFAULT_DOCKER_IMAGE = "python:3.12-slim"
DEFAULT_DOCKER_MEMORY = "512m"
DEFAULT_DOCKER_CPUS = 1.0
# 容器内工作区挂载点
DOCKER_WORKSPACE_MOUNT = "/workspace"


# Job Object 相关的 ctypes 结构（纯 Python 定义，跨平台可安全声明；
# 仅在实际调用 Windows 内核时才依赖 _KERNEL32，其余平台静默跳过，回退为超时控制）。
class _IO_COUNTERS(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_void_p),
        ("MaximumWorkingSetSize", ctypes.c_void_p),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_ulonglong),
        ("JobMemoryLimit", ctypes.c_ulonglong),
        ("PeakProcessMemoryUsed", ctypes.c_ulonglong),
        ("PeakJobMemoryUsed", ctypes.c_ulonglong),
    ]


_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

# Windows 内核句柄（仅 Windows 可用）；其余平台保持 None，相关逻辑静默跳过。
# 用于在 Windows 上实现文档承诺的「进程级内存限制 + 进程树终止」，无需引入 psutil。
_KERNEL32: Any = None
if platform.system() == "Windows":
    try:
        _KERNEL32 = ctypes.windll.kernel32
        _KERNEL32.CreateJobObjectW.restype = ctypes.c_void_p
        _KERNEL32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _KERNEL32.SetInformationJobObject.restype = ctypes.c_int
        _KERNEL32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        _KERNEL32.AssignProcessToJobObject.restype = ctypes.c_int
        _KERNEL32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _KERNEL32.OpenProcess.restype = ctypes.c_void_p
        _KERNEL32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        _KERNEL32.CloseHandle.restype = ctypes.c_int
        _KERNEL32.CloseHandle.argtypes = [ctypes.c_void_p]
    except Exception:  # noqa: BLE001
        _KERNEL32 = None
else:
    _KERNEL32 = None


@dataclass
class SandboxResult:
    """沙箱执行结果。"""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    blocked: bool = False
    blocked_reason: str = ""
    duration_ms: int = 0
    files_created: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 共享安全检查（两个后端复用）
# ---------------------------------------------------------------------------


def detect_dangerous_command(code: str) -> str | None:
    """检查代码是否匹配危险命令黑名单。"""
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE):
            return f"危险命令被拦截: 匹配规则 {pattern}"
    return None


def detect_path_traversal(code: str, workspace: str) -> str | None:
    """检查是否有工作区外路径访问。

    仅检查路径遍历模式（``../`` 或 ``..\\``），而非任意的 ``..`` 子串。
    同时检查指向系统敏感目录的绝对路径。
    """
    ws_abs = str(Path(workspace).resolve())
    # 检查路径遍历模式（而非任意的 ".." 子串，避免误报）
    for pattern in ("../", "..\\"):
        if pattern in code:
            return f"工作区外路径访问被拦截: 检测到路径遍历 {pattern}"
    # 检查指向系统敏感目录的绝对路径
    for sensitive in ("/etc/", "/root/", "/home/", "C:\\Users", "C:\\Windows"):
        if sensitive in code and sensitive not in ws_abs:
            return f"工作区外路径访问被拦截: 检测到 {sensitive}"
    return None


def _decode_bytes(data: bytes | None) -> str:
    """安全解码子进程输出。"""
    return (data or b"").decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Docker 命令运行器（默认走 asyncio 子进程；测试可注入替身）
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    """一条 docker 命令的执行结果（内部用）。"""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


# docker 命令运行器：接收参数与超时，返回结果。用 Protocol 保留命名参数，
# 便于以 timeout= 关键字调用。
class CommandRunner(Protocol):
    """docker 命令运行器协议。"""

    async def __call__(self, args: list[str], timeout: float) -> CommandResult: ...


async def _default_command_runner(args: list[str], timeout: float) -> CommandResult:
    """默认通过 asyncio 子进程执行命令。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        # docker 可执行文件不存在
        return CommandResult(returncode=-1, stdout="", stderr=str(e))
    except OSError as e:
        return CommandResult(returncode=-1, stdout="", stderr=str(e))

    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=3)
        except TimeoutError:
            out_b, err_b = b"", b""
        return CommandResult(
            returncode=-1,
            stdout=_decode_bytes(out_b),
            stderr=_decode_bytes(err_b),
            timed_out=True,
        )
    return CommandResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=_decode_bytes(out_b),
        stderr=_decode_bytes(err_b),
    )


class SandboxBackend(ABC):
    """沙箱后端抽象接口。"""

    @abstractmethod
    async def execute(
        self,
        code: str,
        language: str,
        workspace: str,
        timeout: int = DEFAULT_TIMEOUT,
        max_output: int = DEFAULT_MAX_OUTPUT,
    ) -> SandboxResult:
        """执行代码。"""
        ...

    @abstractmethod
    def get_workspace(self, session_id: str) -> str:
        """获取会话工作区目录。"""
        ...


class LocalSubprocessBackend(SandboxBackend):
    """本地子进程沙箱后端。

    - 会话级工作区隔离
    - 超时进程终止
    - 输出截断
    - 内存/CPU 限制（Linux: resource 模块, Windows: psutil）
    - 危险命令黑名单
    - 工作区外路径访问拦截
    - 默认禁网（通过环境变量）
    """

    def __init__(self, base_workspaces_dir: str = "") -> None:
        self._base_dir = base_workspaces_dir or str(Path(tempfile.gettempdir()) / "harness_workspaces")
        Path(self._base_dir).mkdir(parents=True, exist_ok=True)
        self._system = platform.system()

    def get_workspace(self, session_id: str) -> str:
        """获取或创建会话工作区。"""
        ws = str(Path(self._base_dir) / session_id)
        Path(ws).mkdir(parents=True, exist_ok=True)
        return ws

    def _workspace_exists(self, session_id: str) -> bool:
        """检查工作区是否存在（不创建）。"""
        ws = str(Path(self._base_dir) / session_id)
        return Path(ws).exists()

    def _check_dangerous(self, code: str) -> str | None:
        """检查代码是否匹配危险命令黑名单（委托模块级函数）。"""
        return detect_dangerous_command(code)

    def _check_path_traversal(self, code: str, workspace: str) -> str | None:
        """检查是否有工作区外路径访问（委托模块级函数）。"""
        return detect_path_traversal(code, workspace)

    def _build_env(self) -> dict[str, str]:
        """构建沙箱环境变量（默认禁网）。

        注意：真正的网络隔离需要操作系统级配置（如网络命名空间）。
        此处通过清空代理变量和设置标记变量来提供软限制。
        """
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": "",
            "HOME": os.environ.get("HOME", ""),
            "MPLBACKEND": "Agg",  # matplotlib 无头模式
        }
        # 禁网：清空代理相关环境变量
        for key in [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        ]:
            env[key] = ""
        # 设置标记变量，子进程代码可据此跳过网络操作
        env["HARNESS_NO_NETWORK"] = "1"
        return env

    def _get_preexec_fn(self) -> Any:
        """返回适用于 ``subprocess.Popen(preexec_fn=...)`` 的可调用对象。

        Linux/macOS: 返回设置 RLIMIT_AS 和 RLIMIT_CPU 的函数。
        Windows: 返回 None（Windows 不支持 preexec_fn，资源限制通过超时控制）。
        """
        if self._system in ("Linux", "Darwin"):

            def _set_limits() -> None:
                try:
                    import resource

                    # 内存限制 512MB
                    mem_limit = 512 * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))  # type: ignore[attr-defined]
                    # CPU 时间限制 10 秒
                    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))  # type: ignore[attr-defined]
                    logger.debug("已应用 Unix 资源限制")
                except Exception as e:
                    logger.warning("无法应用 Unix 资源限制: %s", e)

            return _set_limits
        # Windows 不支持 preexec_fn
        return None

    def _kill_process_tree(self, proc: subprocess.Popen[str]) -> None:
        """终止整个进程树。

        Windows：用 ``taskkill /F /T /PID`` 强制杀掉整棵树（含孙进程），避免子进程残留。
        Unix：沿用 killpg 杀进程组。
        """
        if self._system == "Windows":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # type: ignore[attr-defined]
            except (ProcessLookupError, OSError) as e:
                logger.warning("终止进程树失败: %s", e)

    def _apply_job_limits(self, proc: subprocess.Popen[str]) -> None:
        """Windows：把子进程加入 Job Object 并施加内存上限（best-effort）。

        失败时静默跳过，回退为超时控制（与历史行为一致），不影响正常执行。
        """
        if self._system != "Windows" or _KERNEL32 is None:
            return
        try:
            job = _KERNEL32.CreateJobObjectW(None, None)
            if not job:
                return
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = WINDOWS_JOB_MEMORY_LIMIT
            if not _KERNEL32.SetInformationJobObject(
                job,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                _KERNEL32.CloseHandle(job)
                return
            hproc = _KERNEL32.OpenProcess(0x1F0FFF, False, proc.pid)
            if not hproc:
                _KERNEL32.CloseHandle(job)
                return
            assigned = _KERNEL32.AssignProcessToJobObject(job, hproc)
            _KERNEL32.CloseHandle(hproc)
            if not assigned:
                _KERNEL32.CloseHandle(job)
                return
            proc._job_handle = job  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            logger.warning("Windows 资源限制应用失败，回退超时控制: %s", e)

    @staticmethod
    def _close_job(proc: subprocess.Popen[str]) -> None:
        """关闭 Job Object 句柄（进程已退出，仅释放内核对象）。"""
        job = getattr(proc, "_job_handle", None)
        if job is not None:
            try:
                if _KERNEL32 is not None:
                    _KERNEL32.CloseHandle(job)
            except Exception:  # noqa: BLE001
                pass
            proc._job_handle = None  # type: ignore[attr-defined]

    async def execute(
        self,
        code: str,
        language: str,
        workspace: str,
        timeout: int = DEFAULT_TIMEOUT,
        max_output: int = DEFAULT_MAX_OUTPUT,
    ) -> SandboxResult:
        """执行代码。"""
        start_time = time.time()

        # 安全检查
        dangerous = self._check_dangerous(code)
        if dangerous:
            return SandboxResult(
                blocked=True,
                blocked_reason=dangerous,
                duration_ms=int((time.time() - start_time) * 1000),
            )

        traversal = self._check_path_traversal(code, workspace)
        if traversal:
            return SandboxResult(
                blocked=True,
                blocked_reason=traversal,
                duration_ms=int((time.time() - start_time) * 1000),
            )

        # 准备执行
        if language == "python":
            return await self._execute_python(code, workspace, timeout, max_output, start_time)
        elif language == "shell":
            return await self._execute_shell(code, workspace, timeout, max_output, start_time)
        else:
            return SandboxResult(
                stderr=f"不支持的语言: {language}",
                exit_code=-1,
                duration_ms=int((time.time() - start_time) * 1000),
            )

    async def _execute_python(
        self,
        code: str,
        workspace: str,
        timeout: int,
        max_output: int,
        start_time: float,
    ) -> SandboxResult:
        """执行 Python 代码。"""
        script_path = str(Path(workspace) / "script.py")
        Path(script_path).write_text(code, encoding="utf-8")

        env = self._build_env()

        # 快照执行前已有的文件，用于后续对比找出真正新增的文件
        existing_files = {f.name for f in Path(workspace).iterdir()}

        # 构建 Popen 参数（平台适配 preexec_fn 和进程组）
        popen_kwargs: dict[str, Any] = {
            "cwd": workspace,
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if self._system == "Windows":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["preexec_fn"] = self._get_preexec_fn()
            popen_kwargs["start_new_session"] = True

        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                [sys.executable, script_path],
                **popen_kwargs,
            )
            # Windows：施加 Job Object 内存上限，并能在超时/失败时整树终止
            self._apply_job_limits(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                self._kill_process_tree(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
                timed_out = True

            # 输出截断
            if len(stdout) > max_output:
                stdout = stdout[:max_output] + "\n... [输出已截断]"
            if len(stderr) > max_output:
                stderr = stderr[:max_output] + "\n... [输出已截断]"

            # 列出执行后实际新增的文件（对比快照）
            files_created = [f.name for f in Path(workspace).iterdir() if f.name not in existing_files]

            return SandboxResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=proc.returncode,
                timed_out=timed_out,
                duration_ms=int((time.time() - start_time) * 1000),
                files_created=files_created,
            )
        except Exception as e:
            return SandboxResult(
                stderr=f"执行失败: {e}",
                exit_code=-1,
                duration_ms=int((time.time() - start_time) * 1000),
            )
        finally:
            if proc is not None:
                self._close_job(proc)

    async def _execute_shell(
        self,
        code: str,
        workspace: str,
        timeout: int,
        max_output: int,
        start_time: float,
    ) -> SandboxResult:
        """执行 Shell 代码。"""
        env = self._build_env()
        shell = "cmd" if self._system == "Windows" else "bash"

        # 快照执行前已有的文件
        existing_files = {f.name for f in Path(workspace).iterdir()}

        # 构建 Popen 参数（平台适配 preexxec_fn 和进程组）
        popen_kwargs: dict[str, Any] = {
            "cwd": workspace,
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if self._system == "Windows":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["preexec_fn"] = self._get_preexec_fn()
            popen_kwargs["start_new_session"] = True

        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                [shell, "/c", code] if shell == "cmd" else [shell, "-c", code],
                **popen_kwargs,
            )
            # Windows：施加 Job Object 内存上限，并能在超时/失败时整树终止
            self._apply_job_limits(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                self._kill_process_tree(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
                timed_out = True

            if len(stdout) > max_output:
                stdout = stdout[:max_output] + "\n... [输出已截断]"
            if len(stderr) > max_output:
                stderr = stderr[:max_output] + "\n... [输出已截断]"

            # 列出执行后实际新增的文件
            files_created = [f.name for f in Path(workspace).iterdir() if f.name not in existing_files]

            return SandboxResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=proc.returncode,
                timed_out=timed_out,
                duration_ms=int((time.time() - start_time) * 1000),
                files_created=files_created,
            )
        except Exception as e:
            return SandboxResult(
                stderr=f"执行失败: {e}",
                exit_code=-1,
                duration_ms=int((time.time() - start_time) * 1000),
            )
        finally:
            if proc is not None:
                self._close_job(proc)

    def cleanup_workspace(self, session_id: str) -> None:
        """清理会话工作区。"""
        ws = str(Path(self._base_dir) / session_id)
        if Path(ws).exists():
            shutil.rmtree(ws, ignore_errors=True)
            logger.info("工作区已清理: %s", session_id)


class DockerBackend(SandboxBackend):
    """Docker 容器沙箱后端。

    - 每次执行以一次性容器运行（``docker run --rm``），结束即销毁，天然无残留；
    - 会话工作区以 bind mount 挂载到容器内 ``/workspace``，文件在多次执行间持久；
    - 默认 ``--network none`` 禁网（可用 ``network=True`` 打开）；
    - 镜像白名单：设置 ``allowed_images`` 后仅允许其中的镜像；
    - 通过 ``--memory`` / ``--cpus`` 施加资源限制；
    - 超时 / 异常时强制删除本次容器，避免容器泄漏。

    零新增依赖：仅调用本机 ``docker`` CLI；docker 不可用时给出明确错误。
    """

    def __init__(
        self,
        base_workspaces_dir: str = "",
        image: str = DEFAULT_DOCKER_IMAGE,
        allowed_images: list[str] | None = None,
        network: bool = False,
        memory_limit: str = DEFAULT_DOCKER_MEMORY,
        cpus: float = DEFAULT_DOCKER_CPUS,
        runner: CommandRunner | None = None,
    ) -> None:
        self._base_dir = base_workspaces_dir or str(Path(tempfile.gettempdir()) / "harness_docker_workspaces")
        Path(self._base_dir).mkdir(parents=True, exist_ok=True)
        self._image = image
        self._allowed_images = list(allowed_images) if allowed_images else []
        self._network = network
        self._memory = memory_limit
        self._cpus = cpus
        self._run: CommandRunner = runner or _default_command_runner
        self._availability: bool | None = None

    def get_workspace(self, session_id: str) -> str:
        """获取或创建会话工作区（宿主机目录，挂载进容器）。"""
        ws = str(Path(self._base_dir) / session_id)
        Path(ws).mkdir(parents=True, exist_ok=True)
        return ws

    def _workspace_exists(self, session_id: str) -> bool:
        """检查工作区是否存在（不创建）。"""
        return Path(str(Path(self._base_dir) / session_id)).exists()

    async def is_available(self) -> bool:
        """检测 docker 是否可用（已安装且守护进程在运行），结果缓存。"""
        if self._availability is not None:
            return self._availability
        res = await self._run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=10)
        self._availability = res.returncode == 0 and not res.timed_out
        return self._availability

    def _check_image_allowed(self) -> str | None:
        """检查镜像是否在白名单内。"""
        if self._allowed_images and self._image not in self._allowed_images:
            return f"镜像未在白名单中: {self._image}；允许: {', '.join(self._allowed_images)}"
        return None

    @staticmethod
    def _container_name(session_slug: str, run_id: str) -> str:
        """生成合法容器名（仅允许字母数字与 _.-）。"""
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", session_slug)
        return f"harness-{safe}-{run_id}"

    def _build_run_args(
        self,
        workspace: str,
        image: str,
        command: list[str],
        name: str,
    ) -> list[str]:
        """构建 ``docker run`` 参数（纯函数，便于测试）。"""
        network = "bridge" if self._network else "none"
        args = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "-v",
            f"{workspace}:{DOCKER_WORKSPACE_MOUNT}",
            "-w",
            DOCKER_WORKSPACE_MOUNT,
            "--network",
            network,
            "--memory",
            self._memory,
            "--cpus",
            str(self._cpus),
            image,
        ]
        args.extend(command)
        return args

    def _python_command(self, code: str, workspace: str) -> list[str]:
        """把 Python 代码写入工作区，返回容器内执行命令。"""
        (Path(workspace) / "script.py").write_text(code, encoding="utf-8")
        # 容器内工作区即 /workspace
        return ["python", "script.py"]

    @staticmethod
    def _shell_command(code: str) -> list[str]:
        """返回容器内 shell 执行命令（slim 镜像自带 /bin/sh）。"""
        return ["sh", "-c", code]

    async def _force_remove(self, name: str) -> None:
        """强制删除容器（超时后兜底，防止泄漏）。"""
        try:
            await self._run(["docker", "rm", "-f", name], timeout=10)
        except Exception:  # noqa: BLE001
            pass

    async def execute(
        self,
        code: str,
        language: str,
        workspace: str,
        timeout: int = DEFAULT_TIMEOUT,
        max_output: int = DEFAULT_MAX_OUTPUT,
    ) -> SandboxResult:
        """执行代码。"""
        start_time = time.time()

        # 安全检查（与本地后端一致，在容器外先拦截）
        dangerous = detect_dangerous_command(code)
        if dangerous:
            return SandboxResult(
                blocked=True,
                blocked_reason=dangerous,
                duration_ms=int((time.time() - start_time) * 1000),
            )

        traversal = detect_path_traversal(code, workspace)
        if traversal:
            return SandboxResult(
                blocked=True,
                blocked_reason=traversal,
                duration_ms=int((time.time() - start_time) * 1000),
            )

        not_allowed = self._check_image_allowed()
        if not_allowed:
            return SandboxResult(
                blocked=True,
                blocked_reason=not_allowed,
                duration_ms=int((time.time() - start_time) * 1000),
            )

        if not await self.is_available():
            return SandboxResult(
                blocked=True,
                blocked_reason="Docker 不可用：未安装 docker 或守护进程未运行",
                duration_ms=int((time.time() - start_time) * 1000),
            )

        # 组装容器内命令
        if language == "python":
            command = self._python_command(code, workspace)
        elif language == "shell":
            command = self._shell_command(code)
        else:
            return SandboxResult(
                stderr=f"不支持的语言: {language}",
                exit_code=-1,
                duration_ms=int((time.time() - start_time) * 1000),
            )

        run_id = uuid.uuid4().hex[:12]
        session_slug = Path(workspace).name
        name = self._container_name(session_slug, run_id)
        args = self._build_run_args(workspace, self._image, command, name)

        existing_files = {f.name for f in Path(workspace).iterdir()}

        res = await self._run(args, timeout=timeout)

        if res.timed_out:
            # docker CLI 被杀后容器可能仍在运行，强制删除
            await self._force_remove(name)

        stdout = res.stdout
        stderr = res.stderr
        if len(stdout) > max_output:
            stdout = stdout[:max_output] + "\n... [输出已截断]"
        if len(stderr) > max_output:
            stderr = stderr[:max_output] + "\n... [输出已截断]"

        files_created = [f.name for f in Path(workspace).iterdir() if f.name not in existing_files]

        return SandboxResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=res.returncode,
            timed_out=res.timed_out,
            duration_ms=int((time.time() - start_time) * 1000),
            files_created=files_created,
        )

    def cleanup_workspace(self, session_id: str) -> None:
        """清理会话工作区（一次性容器无需额外清理容器）。"""
        ws = str(Path(self._base_dir) / session_id)
        if Path(ws).exists():
            shutil.rmtree(ws, ignore_errors=True)
            logger.info("Docker 工作区已清理: %s", session_id)


class SandboxService(Protocol):
    """沙箱服务接口。"""

    async def execute(
        self,
        code: str,
        language: str,
        session_id: str,
        timeout: int = DEFAULT_TIMEOUT,
        max_output: int = DEFAULT_MAX_OUTPUT,
    ) -> SandboxResult: ...


class SandboxServiceImpl:
    """沙箱服务实现。"""

    def __init__(
        self,
        backend: SandboxBackend | None = None,
        events: EventBus | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_output: int = DEFAULT_MAX_OUTPUT,
    ) -> None:
        self._backend = backend or LocalSubprocessBackend()
        self._events = events
        self._timeout = timeout
        self._max_output = max_output
        # 宿主依赖检查仅对本地子进程后端有意义；Docker 后端依赖在镜像内，跳过。
        self._prerequisites_checked = not isinstance(self._backend, LocalSubprocessBackend)

    def _check_prerequisites(self) -> None:
        """检查 requirements-sandbox.txt 中列出的包是否已安装，尝试安装缺失的包。

        requirements-sandbox.txt 列出了沙箱环境中应预装的包。
        由于沙箱使用宿主 Python（sys.executable），这些包应已安装。
        此方法在首次 execute 时调用一次，对缺失的包尝试 pip install。
        """
        req_path = Path(__file__).resolve().parent.parent.parent.parent / "requirements-sandbox.txt"
        if not req_path.exists():
            return

        missing: list[str] = []
        for line in req_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 提取包名（去除版本 specifier）
            pkg_name = re.split(r"[>=<\[!]", line)[0].strip()
            import_name = pkg_name.replace("-", "_")
            try:
                if importlib.util.find_spec(import_name) is None:
                    missing.append(pkg_name)
            except (ImportError, ModuleNotFoundError):
                missing.append(pkg_name)

        if missing:
            logger.info("沙箱缺失包，尝试安装: %s", ", ".join(missing))
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", *missing],
                    timeout=120,
                )
            except Exception as e:
                logger.warning("自动安装沙箱依赖失败: %s", e)

    async def execute(
        self,
        code: str,
        language: str,
        session_id: str,
        timeout: int | None = None,
        max_output: int | None = None,
    ) -> SandboxResult:
        """执行代码。"""
        if timeout is None:
            timeout = self._timeout
        if max_output is None:
            max_output = self._max_output

        # 首次执行时检查依赖
        if not self._prerequisites_checked:
            self._check_prerequisites()
            self._prerequisites_checked = True

        workspace = self._backend.get_workspace(session_id)

        # 发布执行开始事件
        if self._events:
            await self._events.publish(
                "sandbox.exec.start",
                {
                    "session_id": session_id,
                    "language": language,
                    "code_length": len(code),
                },
            )

        result = await self._backend.execute(code, language, workspace, timeout, max_output)

        # 发布 stdout 事件（含输出数据）
        if self._events and result.stdout:
            await self._events.publish(
                "sandbox.exec.stdout",
                {
                    "session_id": session_id,
                    "chunk": result.stdout,
                },
            )

        # 发布执行结束事件
        if self._events:
            await self._events.publish(
                "sandbox.exec.end",
                {
                    "session_id": session_id,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "timed_out": result.timed_out,
                    "blocked": result.blocked,
                },
            )

        logger.info(
            "沙箱执行: session=%s, language=%s, exit=%d, duration=%dms, blocked=%s",
            session_id,
            language,
            result.exit_code,
            result.duration_ms,
            result.blocked,
        )

        return result

    def cleanup(self, session_id: str) -> None:
        """清理会话工作区。"""
        if isinstance(self._backend, (LocalSubprocessBackend, DockerBackend)):
            self._backend.cleanup_workspace(session_id)
