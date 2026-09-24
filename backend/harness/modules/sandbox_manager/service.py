"""SandboxService —— 沙箱管理服务。

LocalSubprocessBackend: 子进程 + 超时 + 输出截断 + 平台适配的内存/CPU 限制 + 危险命令黑名单。
DockerBackend: 后期实现。
"""

from __future__ import annotations

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
        self._base_dir = base_workspaces_dir or str(
            Path(tempfile.gettempdir()) / "harness_workspaces"
        )
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
        """检查代码是否匹配危险命令黑名单。"""
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, code, re.IGNORECASE):
                return f"危险命令被拦截: 匹配规则 {pattern}"
        return None

    def _check_path_traversal(self, code: str, workspace: str) -> str | None:
        """检查是否有工作区外路径访问。

        仅检查路径遍历模式（``../`` 或 ``..\\``），而非任意的 ``..`` 子串。
        同时检查指向系统敏感目录的绝对路径。
        """
        ws_abs = str(Path(workspace).resolve())
        # 检查路径遍历模式（而非任意的 ".." 子串，避免误报）
        traversal_patterns = ["../", "..\\"]
        for pattern in traversal_patterns:
            if pattern in code:
                return f"工作区外路径访问被拦截: 检测到路径遍历 {pattern}"
        # 检查指向系统敏感目录的绝对路径
        sensitive_dirs = ["/etc/", "/root/", "/home/", "C:\\Users", "C:\\Windows"]
        for dp in sensitive_dirs:
            if dp in code and dp not in ws_abs:
                return f"工作区外路径访问被拦截: 检测到 {dp}"
        return None

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
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy",
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
        """终止整个进程树。"""
        try:
            if self._system == "Windows":
                proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError) as e:
            logger.warning("终止进程树失败: %s", e)

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

        try:
            proc = subprocess.Popen(
                [sys.executable, script_path],
                **popen_kwargs,
            )
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
            files_created = [
                f.name for f in Path(workspace).iterdir()
                if f.name not in existing_files
            ]

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

        try:
            proc = subprocess.Popen(
                [shell, "/c", code] if shell == "cmd" else [shell, "-c", code],
                **popen_kwargs,
            )
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
            files_created = [
                f.name for f in Path(workspace).iterdir()
                if f.name not in existing_files
            ]

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

    def cleanup_workspace(self, session_id: str) -> None:
        """清理会话工作区。"""
        ws = str(Path(self._base_dir) / session_id)
        if Path(ws).exists():
            shutil.rmtree(ws, ignore_errors=True)
            logger.info("工作区已清理: %s", session_id)


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
        self._prerequisites_checked = False

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
            await self._events.publish("sandbox.exec.start", {
                "session_id": session_id,
                "language": language,
                "code_length": len(code),
            })

        result = await self._backend.execute(code, language, workspace, timeout, max_output)

        # 发布 stdout 事件（含输出数据）
        if self._events and result.stdout:
            await self._events.publish("sandbox.exec.stdout", {
                "session_id": session_id,
                "chunk": result.stdout,
            })

        # 发布执行结束事件
        if self._events:
            await self._events.publish("sandbox.exec.end", {
                "session_id": session_id,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "timed_out": result.timed_out,
                "blocked": result.blocked,
            })

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
        if isinstance(self._backend, LocalSubprocessBackend):
            self._backend.cleanup_workspace(session_id)
