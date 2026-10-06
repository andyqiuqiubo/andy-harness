"""事件循环无关的子进程执行工具。

背景（Windows 专属坑）：uvicorn 在 ``--reload`` 或多 worker 模式下
（``config.use_subprocess=True``）会把 Windows 事件循环选成
``SelectorEventLoop``（见 uvicorn/loops/asyncio.py），而
:func:`asyncio.create_subprocess_exec` **只在 ProactorEventLoop 下可用**——
在 Selector 下调用会抛**不带任何消息的 NotImplementedError**（``str(e) == ""``），
于是所有经 asyncio 子进程的能力（飞书 CLI、MCP stdio、沙箱执行、Computer Use
命令）在 ``--reload`` 开发模式下全部静默失败且错误信息为空；pytest 与
``python -m uvicorn``（不带 --reload）走 Proactor，测试全绿，问题只在线上暴露。

方案：统一改为「线程 + 同步 :func:`subprocess.run`」。同步 subprocess 不依赖
事件循环种类，任何循环（Selector / Proactor / uvloop）下行为一致，同时保留
async 接口不阻塞主循环。
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass
class SubprocResult:
    """一次子进程执行的归一化结果。"""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def _run_sync(
    cmd: str | list[str],
    *,
    env: dict[str, str] | None,
    cwd: str | None,
    stdin_data: bytes | None,
    timeout: float | None,
    shell: bool,
    merge_stderr: bool,
) -> SubprocResult:
    try:
        # capture_output 与 stderr=STDOUT 互斥（run() 会抛 ValueError），手动展开
        kwargs: dict[str, Any] = {
            "input": stdin_data,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            "env": env,
            "cwd": cwd,
            "timeout": timeout,
            "shell": shell,
        }
        proc = subprocess.run(cmd, **kwargs)
    except FileNotFoundError as e:
        return SubprocResult(returncode=-1, stdout="", stderr=f"可执行文件不存在: {e}")
    except subprocess.TimeoutExpired as e:
        # run() 超时后已 kill 子进程；已产生的部分输出挂在异常上，尽量带回
        return SubprocResult(
            returncode=-1,
            stdout=_decode(e.stdout),
            stderr=_decode(e.stderr),
            timed_out=True,
        )
    except OSError as e:
        return SubprocResult(returncode=-1, stdout="", stderr=str(e))
    return SubprocResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=_decode(proc.stdout),
        stderr=_decode(proc.stderr),
    )


async def run(
    cmd: str | list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    stdin_data: bytes | None = None,
    timeout: float | None = None,
    shell: bool = False,
    merge_stderr: bool = False,
) -> SubprocResult:
    """在线程内同步执行子进程，返回归一化结果（不向调用方抛子进程异常）。

    - ``shell=True`` 时 ``cmd`` 为字符串（等价 ``create_subprocess_shell``）；
    - ``timeout`` 到期会 kill 子进程并返回 ``timed_out=True``（尽量带回已产出内容）；
    - ``merge_stderr=True`` 时 stderr 并入 stdout（等价 ``stderr=STDOUT``）。
    """
    return await asyncio.to_thread(
        _run_sync,
        cmd,
        env=env,
        cwd=cwd,
        stdin_data=stdin_data,
        timeout=timeout,
        shell=shell,
        merge_stderr=merge_stderr,
    )
