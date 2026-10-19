"""browser_daemon 插件 —— BrowserSkill bsk daemon 的后端确定性托管。

背景：对话模型所在的宿主沙箱会回收/拦截由其 shell 派生的常驻进程
（os error 5 / Job Object），导致 `bsk daemon start` 在会话里必失败。
本插件把 daemon 启动挪到 **后端进程**（start-all.bat / Tauri 壳拉起的长活进程）：
由它 Popen 一个 detached 的 `bsk daemon start --foreground`，日志落
`<项目根>/logs/browser-daemon.log`，并用 `bsk status --json` 复核就绪。

browser-skill（vendored BrowserSkill）执行前调用本插件注册的
`browser_daemon_status` 工具即可零用户操作地拿到就绪的 daemon。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin
from harness.modules.skill_manager.service import SkillService

logger_holder: list[Any] = []


class BrowserDaemonService:
    """bsk CLI 定位 + daemon 探测/启动/状态（后端进程执行，不受对话沙箱限制）。"""

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self._started_by_us = False  # 本次后端生命周期内是否由本服务拉起

    # ── CLI 定位 ──────────────────────────────────────

    @staticmethod
    def find_cli() -> str | None:
        """按「PATH → 官方默认安装目录」顺序定位 bsk 可执行文件。"""
        found = shutil.which("bsk")
        if found:
            return found
        exe = "bsk.exe" if sys.platform == "win32" else "bsk"
        candidate = Path.home() / ".local" / "bin" / exe
        if candidate.is_file():
            return str(candidate)
        env_dir = os.environ.get("BSK_INSTALL_DIR", "").strip()
        if env_dir:
            candidate = Path(env_dir) / exe
            if candidate.is_file():
                return str(candidate)
        return None

    # ── daemon 探测 ───────────────────────────────────

    async def probe(self) -> dict[str, Any]:
        """`bsk status --json`（BSK_AUTO_START=0，禁隐式自启）。

        返回 {"ok": bool, "status": dict|None, "raw": str}。
        """
        from harness.infra import subproc

        cli = self.find_cli()
        if not cli:
            return {"ok": False, "status": None, "raw": "", "error": "cli_not_found"}
        env = dict(os.environ)
        env["BSK_AUTO_START"] = "0"
        result = await subproc.run([cli, "status", "--json"], env=env, timeout=15)
        if result.timed_out:
            return {"ok": False, "status": None, "raw": result.stdout, "error": "probe_timeout"}
        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return {"ok": False, "status": None, "raw": result.stdout.strip()[:500], "error": "bad_json"}
        if result.returncode != 0 or "daemon_version" not in data:
            return {"ok": False, "status": None, "raw": result.stdout.strip()[:500], "error": "daemon_absent"}
        return {"ok": True, "status": data, "raw": ""}

    # ── daemon 启动（detached，随后端进程树之外的独立生命周期）──

    async def ensure_running(self, wait_secs: int = 8) -> dict[str, Any]:
        """探测→（未运行则）拉起→轮询复核。幂等：已运行不会重复启动。"""
        probed = await self.probe()
        if probed["ok"]:
            return {"state": "reused", "status": probed["status"]}

        cli = self.find_cli()
        if not cli:
            return {"state": "cli_missing", "status": None}
        if probed.get("error") == "probe_timeout":
            # 探测超时不能证明 daemon 不存在，盲目再拉会撞锁；如实回报
            return {"state": "probe_timeout", "status": None}

        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            logf = self._log_path.open("ab")  # noqa: SIM115（detached 子进程持有句柄）
            env = dict(os.environ)
            env["BSK_AUTO_START"] = "0"
            kwargs: dict[str, Any] = {
                "stdout": logf,
                "stderr": logf,
                "stdin": subprocess.DEVNULL,
                "env": env,
                "cwd": str(self._log_path.parent),
            }
            if sys.platform == "win32":
                # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：脱离后端 Job Object
                kwargs["creationflags"] = 0x00000008 | 0x00000200
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen([cli, "daemon", "start", "--foreground"], **kwargs)  # noqa: S603
            self._started_by_us = True
        except OSError as e:
            return {"state": "start_failed", "status": None, "error": str(e)}

        deadline = max(2, wait_secs)
        for _ in range(deadline):
            await asyncio.sleep(1)
            again = await self.probe()
            if again["ok"]:
                return {"state": "started", "status": again["status"], "pid": proc.pid}
        # 未就绪：带回启动日志尾部便于归因（锁冲突/权限等），不谎报成功
        tail = ""
        try:
            tail = self._log_path.read_bytes()[-1500:].decode("utf-8", errors="replace")
        except OSError:
            pass
        return {
            "state": "start_unconfirmed",
            "status": None,
            "pid": proc.pid,
            "log_tail": tail,
            "hint": "daemon 进程已派生但复核未就绪；若日志显示锁冲突，说明已有 daemon 在启动中，稍后重试探测即可",
        }

    # ── 归因辅助 ──────────────────────────────────────

    @property
    def started_by_us(self) -> bool:
        return self._started_by_us


# ── 工具：browser_daemon_status ─────────────────────


class BrowserDaemonStatusTool(ToolPlugin):
    """browser_daemon_status 工具 —— browser-skill 执行前的就绪保障入口。"""

    def __init__(self, services: Any) -> None:
        self._services = services

    @property
    def tool_name(self) -> str:
        return "browser_daemon_status"

    @property
    def risk_level(self) -> str:
        """只影响本机 daemon 进程生命周期，无用户数据副作用。"""
        return "write"

    @property
    def description(self) -> str:
        return (
            "检查并保障 BrowserSkill（browser-skill）本地守护进程 daemon 就绪："
            "由后端进程自动探测 bsk CLI、必要时自动拉起 daemon 并复核，"
            "返回 JSON（state: reused/started/started_by_other/cli_missing/...、"
            "daemon 版本、协议版本、已连接浏览器实例与扩展版本、version_skew）。"
            "执行任何 bsk 浏览器任务前调用它替代手动 `bsk status`；"
            "**绝不要**因为本工具失败而让用户去开终端启动 daemon——"
            "state=cli_missing 时才需要用户安装 bsk CLI，其余如实转述返回内容。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["ensure", "probe"],
                    "description": "ensure=探测并必要时拉起（默认）；probe=只探测不启动",
                },
            },
            "required": [],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        action = str(args.get("action", "ensure")).strip().lower() or "ensure"
        if not self._services.has(BrowserDaemonService):
            return json.dumps(
                {"state": "plugin_unavailable", "error": "browser_daemon 插件未激活"},
                ensure_ascii=False,
            )
        service: BrowserDaemonService = self._services.get(BrowserDaemonService)

        if action == "probe":
            probed = await service.probe()
        else:
            probed = await service.ensure_running()

        status = probed.get("status") or {}
        browsers = status.get("browsers") or []
        result: dict[str, Any] = {
            "state": probed.get("state", "ok" if probed.get("ok") else "daemon_absent"),
            "daemon_version": status.get("daemon_version", ""),
            "protocol_version": status.get("protocol_version", ""),
            "pid": status.get("pid") or probed.get("pid"),
            "browser_instances": [
                {
                    "instance_id": b.get("instance_id", ""),
                    "browser": b.get("browser_name", ""),
                    "extension_version": b.get("extension_version", ""),
                    "extension_protocol": b.get("extension_protocol_version", ""),
                    "version_skew": b.get("version_skew", False),
                    "unresponsive": b.get("unresponsive", False),
                }
                for b in browsers
            ],
            "sessions": len(status.get("sessions") or []),
        }
        if probed.get("error"):
            result["error"] = probed["error"]
        if probed.get("log_tail"):
            result["log_tail"] = probed["log_tail"]
        if probed.get("hint"):
            result["hint"] = probed["hint"]

        # 就绪判定与下一步指引（确定性代码给出，模型照做即可）
        ready_daemon = result["state"] in ("reused", "started")
        connected = [b for b in result["browser_instances"] if not b["unresponsive"]]
        if ready_daemon and connected:
            result["ready"] = True
            result["next"] = "daemon 与浏览器扩展均已就绪，按 browser-skill 流程继续 session start。"
        elif ready_daemon:
            result["ready"] = False
            result["next"] = (
                "daemon 已就绪但没有已连接的浏览器实例：通常是对应浏览器未打开、"
                "或未安装/未启用 BrowserSkill 扩展（技能正文含 Chrome/Edge 官方商店链接）；"
                "若浏览器在运行而扩展已装但未连，可请用户打开浏览器后重试，或由其确认扩展本地连接开关。"
            )
        elif result["state"] == "cli_missing":
            result["ready"] = False
            result["next"] = "本机未找到 bsk CLI：按技能正文的官方安装命令引导用户安装（这一步无法代劳）。"
        else:
            result["ready"] = False
            result["next"] = "daemon 未就绪，按返回的 error/hint/log_tail 如实归因，勿让用户开终端手动启动。"
        return json.dumps(result, ensure_ascii=False)


# ── 插件装配 ────────────────────────────────────────


def _skill_available(services: Any) -> bool:
    """browser-skill 已安装且启用时才默认激活本插件（其余情况仍不阻塞加载）。"""
    try:
        if not services.has(SkillService):
            return True  # 技能服务尚未就绪时不误杀插件
        meta = services.get(SkillService).get_skill("browser-skill")
        return meta is not None and meta.enabled
    except Exception:  # noqa: BLE001
        return True


class BrowserDaemonPlugin(BasePlugin):
    """browser_daemon 插件：注册服务 + browser_daemon_status 工具。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx

        from harness.engine.tool_registry import ToolRegistry

        backend_dir = Path(__file__).resolve().parents[2]  # <repo>/backend
        default_log = backend_dir.parent / "logs" / "browser-daemon.log"
        configured = str(ctx.config.get("daemon_log", "") or "").strip()
        log_path = Path(configured) if configured else default_log

        service = BrowserDaemonService(log_path)
        ctx.services.register(BrowserDaemonService, service, owner=self.plugin_id)

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        ctx.services.get(ToolRegistry).register(BrowserDaemonStatusTool(ctx.services), owner=self.plugin_id)
        ctx.logger.info("browser_daemon 已激活（daemon 日志: %s；按需由工具调用触发探测/启动）", log_path)
        if not _skill_available(ctx.services):
            ctx.logger.info("browser-skill 未启用：browser_daemon 保持激活但通常不会被调用")

    async def deactivate(self, ctx: PluginContext) -> None:
        from harness.engine.tool_registry import ToolRegistry

        try:
            ctx.services.get(ToolRegistry).unregister_all(self.plugin_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            ctx.services.unregister(BrowserDaemonService, owner=self.plugin_id)
        except Exception:  # noqa: BLE001
            pass
        ctx.logger.info("browser_daemon 已停用（不会主动杀掉已在运行的 daemon，其生命周期归宿主环境）")
