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
import os
import subprocess
from dataclasses import dataclass
from typing import Any

# ── 子进程环境变量白名单 ──────────────────────────────────────────────
#
# 背景（体检 P0-2 / P1-6）：项目红线明确「禁止在子进程中透传完整 os.environ」，
# 但仓库里存在三套标准：
#   ✅ sandbox_manager / reverse_engineering._run_isolated —— 已做 env 白名单
#   ❌ desktop_repeat._spawn_recorder —— 整体透传 os.environ（P0-2）
#   ❌ reverse_engineering 的 javap/反编译器/rebuild —— 未传 env=，隐式继承（P1-6）
#
# 本函数把「白名单」收敛到一处，四个调用点共用。

#: 基础系统变量白名单（Windows / POSIX 通用子集）
BASE_ENV_ALLOWLIST: frozenset[str] = frozenset({
    "PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "LANG", "LC_ALL", "TZ",
    "JAVA_HOME", "NODE_PATH",
})

#: 项目自定义配置变量的前缀白名单（HARNESS_* / ANDY_* 均为本项目自有命名空间）
PROJECT_ENV_PREFIXES: tuple[str, ...] = ("HARNESS_", "ANDY_")

#: 前缀放行时仍要排除的敏感变量（``HARNESS_KB_ASR_API_KEY`` 这类也带项目前缀，
#: 但它们是真实凭据，绝不能透传给子进程）。
SENSITIVE_ENV_DENYLIST: frozenset[str] = frozenset({
    "OPENAI_API_KEY", "DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY",
    "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID",
    "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "HARNESS_KB_ASR_API_KEY",
    "HARNESS_REPEAT_VISION_API_KEY", "HARNESS_MCP_TOKEN",
})


def build_env_allowlist(
    *,
    extra_allow: tuple[str, ...] = (),
    extra_vars: dict[str, str] | None = None,
    include_project_prefixes: bool = True,
) -> dict[str, str]:
    """构造子进程环境变量字典（**白名单**，不透传完整 ``os.environ``）。

    :param extra_allow: 额外允许透传的具体变量名（精确匹配）。
    :param extra_vars: 额外显式写入的变量（``{name: value}``，不读宿主环境）。
    :param include_project_prefixes: 是否放行 ``HARNESS_*`` / ``ANDY_*`` 前缀变量。

    用法::

        env = build_env_allowlist(extra_allow=("REA_LOG_LEVEL",))
        subprocess.run(cmd, env=env)

    注意：白名单**不会**包含任何 API Key（``OPENAI_API_KEY``、
    ``DASHSCOPE_API_KEY`` 等一律不在名单内）——若某个子进程确实需要密钥，
    应通过 ``extra_vars`` 显式传入并自行决定是否落盘。
    """
    allowed = set(BASE_ENV_ALLOWLIST) | set(extra_allow)
    env: dict[str, str] = {}
    for key in allowed:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    if include_project_prefixes:
        for key, val in os.environ.items():
            if key.startswith(PROJECT_ENV_PREFIXES) and key not in SENSITIVE_ENV_DENYLIST:
                env[key] = val
    if extra_vars:
        env.update({str(k): str(v) for k, v in extra_vars.items()})
    # 即使被显式要求传入，也拦截敏感凭据（extra_vars 需自行承担，见 docstring）
    for key in list(env):
        if key in SENSITIVE_ENV_DENYLIST:
            env.pop(key, None)
    return env


def assert_env_isolated(env: dict[str, str], *, forbidden: tuple[str, ...] = ()) -> None:
    """断言 env 不含敏感变量（测试用）。

    :param env: 待检查的环境字典。
    :param forbidden: 额外的禁用变量名；默认覆盖常见 API Key 形态。
    """
    bad = [k for k in {*SENSITIVE_ENV_DENYLIST, *forbidden} if k in env]
    if bad:
        raise AssertionError(f"子进程环境泄漏敏感变量: {bad}")


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
