#!/usr/bin/env python3
"""桌面壳后端启动器（E11）。

Tauri 壳（以及双击脚本 / 调试）通过本脚本启动后端：
- 解析仓库内 venv 的 Python（可用 --python / HARNESS_PYTHON 覆盖）；
- 桌面场景默认只监听 127.0.0.1（不暴露到局域网）；
- 数据目录默认放到用户目录下（Windows: %APPDATA%/andy-harness，
  macOS: ~/Library/Application Support/andy-harness，
  Linux: ~/.local/share/andy-harness），通过环境变量交给后端；
- 子进程以独立进程组 / 进程树方式管理；收到退出信号时整棵树终止，
  不残留 uvicorn 进程。

用法：
  python launch-backend.py                 # 正常启动（默认 127.0.0.1:8000）
  python launch-backend.py --check         # 只解析并打印配置（JSON），不启动
  python launch-backend.py --port 8765 --data-dir /tmp/ah
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def repo_root() -> Path:
    """desktop/launcher/launch-backend.py → 仓库根（上两级）。"""
    return Path(__file__).resolve().parents[2]


def default_data_dir() -> Path:
    """按平台选择每用户数据目录。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "andy-harness"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "andy-harness"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "andy-harness"


def resolve_python(explicit: str | None = None) -> Path:
    """解析用于跑后端的 Python 解释器。"""
    if explicit:
        return Path(explicit).resolve()
    env_python = os.environ.get("HARNESS_PYTHON")
    if env_python:
        return Path(env_python).resolve()
    backend = repo_root() / "backend"
    candidate = (
        backend / ".venv" / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else backend / ".venv" / "bin" / "python"
    )
    return candidate.resolve()


def build_config(
    *,
    host: str,
    port: int,
    data_dir: Path,
    python_exe: Path,
    log_level: str,
) -> dict[str, Any]:
    """汇总启动配置（供 --check 与 Rust 侧读取）。"""
    backend_dir = repo_root() / "backend"
    return {
        "host": host,
        "port": port,
        "backend_dir": str(backend_dir),
        "python": str(python_exe),
        "data_dir": str(data_dir),
        "db_path": str(data_dir / "harness.db"),
        "attachments_dir": str(data_dir / "attachments"),
        "log_level": log_level,
        "url": f"http://{host}:{port}",
    }


def _validate(config: dict[str, Any]) -> list[str]:
    """检查启动前置条件，返回问题列表。"""
    problems: list[str] = []
    python_exe = Path(config["python"])
    if not python_exe.exists():
        problems.append(
            f"Python 解释器不存在: {python_exe}（先在 backend 目录 uv sync 建 venv）"
        )
    if not Path(config["backend_dir"]).is_dir():
        problems.append(f"backend 目录不存在: {config['backend_dir']}")
    return problems


def _child_env(config: dict[str, Any]) -> dict[str, str]:
    """构造子进程环境，把数据目录等通过后端识别的环境变量传入。"""
    env = dict(os.environ)
    env["HARNESS_DB_PATH"] = config["db_path"]
    env["HARNESS_ATTACHMENTS_DIR"] = config["attachments_dir"]
    # 子进程自己不继承可能干扰的 venv 激活态
    return env


def _terminate_tree(proc: subprocess.Popen[Any]) -> None:
    """跨平台终止整棵进程树。"""
    if sys.platform == "win32":
        # /T 连子孙一起杀；忽略未找到进程的错误。
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            return


def run_backend(config: dict[str, Any]) -> int:
    """启动后端并等待，返回退出码。"""
    Path(config["data_dir"]).mkdir(parents=True, exist_ok=True)
    Path(config["attachments_dir"]).mkdir(parents=True, exist_ok=True)

    cmd = [
        config["python"],
        "-m",
        "uvicorn",
        "harness.main:app",
        "--host",
        config["host"],
        "--port",
        str(config["port"]),
        "--log-level",
        config["log_level"],
    ]
    kwargs: dict[str, Any] = {
        "cwd": config["backend_dir"],
        "env": _child_env(config),
    }
    if sys.platform != "win32":
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)

    def _handle_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
        _terminate_tree(proc)

    # 转发 SIGINT / SIGTERM；Windows 上 SIGTERM 不可用，仅注册 SIGINT。
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    try:
        return proc.wait()
    except KeyboardInterrupt:
        _terminate_tree(proc)
        return 0
    finally:
        if proc.poll() is None:
            _terminate_tree(proc)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="andy-harness 桌面后端启动器")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", default=None, help="数据目录（默认按平台用户目录）")
    parser.add_argument("--python", default=None, help="后端 Python 解释器路径")
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只解析并打印配置（JSON），不启动",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    config = build_config(
        host=args.host,
        port=args.port,
        data_dir=data_dir.resolve(),
        python_exe=resolve_python(args.python),
        log_level=args.log_level,
    )

    if args.check:
        config["problems"] = _validate(config)
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return 1 if config["problems"] else 0

    problems = _validate(config)
    if problems:
        for problem in problems:
            print(f"错误: {problem}", file=sys.stderr)
        return 1
    return run_backend(config)


if __name__ == "__main__":
    raise SystemExit(main())
