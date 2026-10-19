"""DesktopRepeatService：对外唯一门面（§10.1）。

职责：
- 录制：租约 → bundle 创建 → bootstrap.json → Popen 录制器子进程（detached，
  与 browser_daemon 同范式）→ 心跳监视（RP-7004）→ 控制文件下发 pause/resume/marker/stop/discard；
- 编译：manifest 校验 → 9 阶段管线 → 工作流草稿/落库；
- 运行：静态参数干校验（RP-8001/2/3 在零副作用前拦截）→ RepeatRunner 后台任务
  → repeat_runs / repeat_step_hits 落库 → SSE 事件（bus.repeat_progress_store）；
- 技能：工作流 → SKILL.md（skill/generator）→ 技能目录 reload；
- 紧急停止热键（Ctrl+Alt+.，运行期 pynput 监听，缺依赖静默降级）。

本模块不 import fastapi；REST 层只做 HTTP→门面调用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .bus import Reporter, repeat_progress_store
from .config import RecordConfig, RepeatConfig
from .errors import RepeatError
from .executor.params import collect_required_param_names, resolve_params
from .executor.runner import RepeatRunner, graph_sha256, steps_from_graph
from .interpreter.pipeline import compile_session
from .models import CompileResult, CompileWarning
from .safety_gate import Decision, SafetyGate
from .store.bundle import BundleReader, new_session_id

logger = logging.getLogger("harness.repeat.service")

HEARTBEAT_STALE_S = 2.0


class DesktopRepeatService:
    def __init__(self, db: Any, sessions_root: Path, cfg: RepeatConfig | None = None,
                 *, workflow_service: Any | None = None,
                 skill_service: Any | None = None,
                 notify: Any | None = None) -> None:
        self._db = db
        from .repository import RepeatRepository
        self._repo = RepeatRepository(db)
        try:
            self._repo.setup()
        except Exception as e:  # noqa: BLE001
            logger.warning("repeat 表初始化失败（DB 不可写？）: %s", e)
        self._root = Path(sessions_root)
        self._root.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg or RepeatConfig.load()
        self._workflow_service = workflow_service
        self._skill_service = skill_service
        self._notify = notify

        self._active_session: str | None = None
        self._recorder_proc: subprocess.Popen[bytes] | None = None
        self._hb_task: asyncio.Task[None] | None = None
        self._lease: Any | None = None
        self._runners: dict[str, RepeatRunner] = {}
        self._run_tasks: dict[str, asyncio.Task[None]] = {}
        self._gc_task: asyncio.Task[None] | None = None
        self._emergency: dict[str, threading.Event] = {}
        self._hotkey_listener: Any | None = None
        self._hotkey_stop: threading.Event | None = None
        self._gates: dict[str, SafetyGate] = {}
        self._platform: dict[str, Any] | None = None
        self._stopping: set[str] = set()   # API 主动 stop/discard 的会话（抑制重复通知）
        # 进程重启后内存 runner 全部丢失：把上次遗留的 running/suspended 标为
        # interrupted，否则它们会永久停在"进行中"，用户既看不到结果也无法重跑。
        try:
            stale = self._repo.mark_stale_runs()
            if stale:
                logger.info("repeat: %d 条遗留运行已标记为 interrupted（RP-7005）", stale)
        except Exception as e:  # noqa: BLE001
            logger.warning("repeat 遗留运行清理失败: %s", e)

    # ── 依赖可用性 ───────────────────────────────────────────────
    def available(self) -> bool:
        try:
            from .platform import make_capture, make_inspector
            return bool(make_capture().available()) and bool(make_inspector().available())
        except Exception:  # noqa: BLE001
            return False

    def _platforms(self) -> dict[str, Any]:
        if self._platform is None:
            from .platform import (
                make_capture,
                make_env,
                make_grabber,
                make_injector,
                make_inspector,
                make_lease,
                make_windows,
            )
            injector = make_injector()
            from .executor.pacing import HumanTakeover
            self._platform = {
                "capture": make_capture(), "inspector": make_inspector(),
                "grabber": make_grabber(), "injector": injector,
                "windows": make_windows(), "env": make_env(), "lease": make_lease(),
                "takeover": HumanTakeover(injector),
            }
        return self._platform

    # ── 会话持久化（DB） ─────────────────────────────────────────
    def _db_upsert_session(self, sid: str, doc: dict[str, Any]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self._db.execute(
            "INSERT INTO repeat_sessions (id, label, status, session_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET label=excluded.label, status=excluded.status, "
            "session_json=excluded.session_json, updated_at=excluded.updated_at",
            (sid, str(doc.get("label", "")), str(doc.get("status", "draft")),
             json.dumps(doc, ensure_ascii=False), now, now),
        )

    def _db_list_sessions(self, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        try:
            rows = self._db.query(
                "SELECT id, label, status, created_at, updated_at, session_json "
                "FROM repeat_sessions ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        except Exception:  # noqa: BLE001
            return [], 0
        total_row = self._db.query_one("SELECT COUNT(*) AS c FROM repeat_sessions")
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["session"] = json.loads(d.pop("session_json") or "{}")
            except (json.JSONDecodeError, KeyError):
                d["session"] = {}
            # 列表页直接消费 counters（前端不该为了显示事件数再逐条拉详情）
            d["counters"] = (d["session"] or {}).get("counters") or {}
            out.append(d)
        return out, int(total_row["c"] if total_row else 0)

    def _db_delete_session(self, sid: str) -> None:
        try:
            self._db.execute("DELETE FROM repeat_sessions WHERE id = ?", (sid,))
        except Exception:  # noqa: BLE001
            pass

    # ── 录制：T01 start ──────────────────────────────────────────
    async def start_recording(self, rec: RecordConfig | None = None) -> dict[str, Any]:
        if not self.available():
            raise RepeatError("RP-7003", "录制依赖未安装（pynput/uiautomation），"
                                         "请 pip install 'harness[repeat]' 后在带图形会话中运行")
        if self._active_session:
            raise RepeatError("RP-5004", f"已有录制会话进行中: {self._active_session}")
        rec = rec or RecordConfig()
        lease_ok, holder = self._platforms()["lease"].acquire()
        if not lease_ok:
            raise RepeatError("RP-5004", "输入设备租约被占用（已有录制/回放在运行）",
                              detail={"holder_pid": holder})
        self._lease = self._platforms()["lease"]
        sid = new_session_id()
        sdir = self._root / sid
        (sdir / "runtime").mkdir(parents=True, exist_ok=True)
        env = self._platforms()["env"].snapshot()
        # env 物理真值 + 桌面名等现场采集（主线程一次）
        bootstrap = {
            "session_id": sid,
            "config": self._cfg_to_dict(),
            "record": {
                "label": rec.label, "frame_tier": rec.frame_tier,
                "countdown_s": rec.countdown_s, "mask_windows": rec.mask_windows,
                "mask_keys": rec.mask_keys, "auto_pause_mask": rec.auto_pause_mask,
            },
            "env": env.to_dict(),
        }
        (sdir / "runtime" / "bootstrap.json").write_text(
            json.dumps(bootstrap, ensure_ascii=False), encoding="utf-8")
        (sdir / "runtime" / "control.json").write_text('{"_rev":0}', encoding="utf-8")
        # session.json(draft)：写端先行（recorder 接管后同一路径续写）
        (sdir / "session.json").write_text(json.dumps({
            "schema": 1, "session_id": sid, "status": "draft",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "label": rec.label, "env": env.to_dict(),
            "config": bootstrap["config"], "counters": {}, "integrity": {}, "error": None,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        self._active_session = sid
        await asyncio.to_thread(self._spawn_recorder, sdir)
        self._hb_task = asyncio.create_task(self._heartbeat_watch(sid))
        doc = self._read_session(sid)
        self._db_upsert_session(sid, doc)
        return doc

    def _cfg_to_dict(self) -> dict[str, Any]:
        c = self.cfg
        return {
            "frame_tier": c.frame_tier, "countdown_s": c.countdown_s,
            "uia_enabled": c.uia_enabled, "uia_timeout_ms": c.uia_timeout_ms,
            "capture_clipboard": c.capture_clipboard, "mask_windows": c.mask_windows,
            "mask_keys": c.mask_keys, "auto_pause_mask": c.auto_pause_mask,
            "hotkeys": c.hotkeys, "queue_capacity": c.queue_capacity,
            "coord_gate": c.coord_gate, "allow_password_replay": c.allow_password_replay,
        }

    def _spawn_recorder(self, sdir: Path) -> None:
        backend_dir = Path(__file__).resolve().parents[3]  # <repo>/backend
        logf = (sdir / "runtime" / "recorder.log").open("ab")  # noqa: SIM115
        kwargs: dict[str, Any] = {
            "stdout": logf, "stderr": logf, "stdin": subprocess.DEVNULL,
            "cwd": str(backend_dir),
            "env": {**dict(__import__("os").environ),
                    "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        }
        if sys.platform == "win32":
            # CREATE_NO_WINDOW：不分配可见控制台窗口。
            # 旧值用了 DETACHED_PROCESS，Windows 会为控制台程序**新建一个黑窗**
            # （标题就是 python.exe 路径），开始录制时突然弹出、挡住界面（真机坐实）。
            kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self._recorder_proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "harness.modules.desktop_repeat.recorder.runner",
             "--bundle", str(sdir)], **kwargs)

    async def _heartbeat_watch(self, sid: str) -> None:
        reader = BundleReader(self._root / sid)
        last_ok = time.time()
        try:
            while self._active_session == sid:
                # 终态优先：录制器自行结束（热键/覆盖层/API）后立即收口。
                # 旧实现要等"进程退出 + 心跳超时 6s"，叠加收尾横幅驻留（默认 8s）
                # 会让 UI 挂着"录制中"十几秒，期间新录制被 RP-5004 拒绝（真机坐实）。
                doc: dict[str, Any] | None = None
                try:
                    doc = reader.session()
                except RepeatError:
                    doc = None
                st = str((doc or {}).get("status", "draft"))
                if st in ("ready", "failed", "discarded"):
                    self._db_upsert_session(sid, doc or {})
                    if sid not in self._stopping:
                        if st == "ready":
                            cnt = (doc or {}).get("counters", {})
                            self._notify_user("info", "录制已结束",
                                              f"{(doc or {}).get('label') or sid}："
                                              f"{cnt.get('events', 0)} 事件")
                        elif st == "discarded":
                            self._notify_user("info", "录制已丢弃", str((doc or {}).get("label") or sid))
                        else:
                            err = (doc or {}).get("error") or {}
                            self._notify_user("error", "录制失败", str(err.get("message", "")))
                    self._active_session = None
                    self._release_lease()
                    proc = self._recorder_proc
                    self._recorder_proc = None
                    if proc is not None:
                        try:
                            await asyncio.to_thread(proc.wait, 12)
                        except Exception:  # noqa: BLE001
                            pass
                    break
                hb = reader.heartbeat()
                ts = float(hb.get("ts", 0) or 0) / 1000.0
                if ts and time.time() - ts <= HEARTBEAT_STALE_S:
                    last_ok = time.time()
                elif time.time() - last_ok > HEARTBEAT_STALE_S * 3:
                    # RP-7004：心跳持续失联且进程已退出（崩溃/被杀）
                    proc = self._recorder_proc
                    if proc is not None and proc.poll() is not None:
                        code = proc.poll()
                        doc2 = reader.session()
                        if str(doc2.get("status", "draft")) == "draft":
                            doc2["status"] = "failed"
                            doc2["error"] = {"code": "RP-7004",
                                             "message": f"录制器异常退出 code={code}，已保留已录部分"}
                            (self._root / sid / "session.json").write_text(
                                json.dumps(doc2, ensure_ascii=False, indent=2), encoding="utf-8")
                            self._db_upsert_session(sid, doc2)
                            self._notify_user("error", "录制器异常退出",
                                              f"已保留已录部分（session {sid}）")
                        self._active_session = None
                        self._recorder_proc = None
                        self._release_lease()
                        break
                    last_ok = time.time()  # 尚无心跳文件（启动窗口）：再给时间
                await asyncio.sleep(0.8)
        except asyncio.CancelledError:
            return

    # ── 控制下发 ─────────────────────────────────────────────────
    async def pause_recording(self) -> dict[str, Any]:
        return await self._control({"cmd": "pause"})

    async def resume_recording(self) -> dict[str, Any]:
        return await self._control({"cmd": "resume"})

    async def marker(self, label: str = "", note: str = "") -> dict[str, Any]:
        return await self._control({"cmd": "marker", "label": label, "note": note})

    async def discard_recording(self, session_id: str | None = None) -> dict[str, Any]:
        """丢弃录制：与 stop 同一收尾路径（等终态 → 回收进程 → 清活跃态 → 释放租约）。"""
        return await self.stop_recording(session_id, discard=True)

    async def _control(self, cmd: dict[str, Any]) -> dict[str, Any]:
        sid = self._active_session or self._discover_live_session()
        if not sid:
            raise RepeatError("RP-5004", "当前无活跃录制会话")
        reader = BundleReader(self._root / sid)
        reader.send_control(cmd)
        # 等生效（≤2 轮询）
        rev_expected = int(reader.control().get("_rev", 0))
        for _ in range(20):
            await asyncio.sleep(0.15)
            if int(reader.control().get("_rev", 0) or 0) >= rev_expected:
                hb = reader.heartbeat()
                if str(hb.get("state", "")) in ("recording", "paused", "finalizing", "ready"):
                    break
        doc = self._read_session(sid)
        self._db_upsert_session(sid, doc)
        return doc

    def _discover_live_session(self) -> str | None:
        """发现"本进程没启动、但确实活着"的录制会话（体检 P1-3）。

        CLI 的 record --stop / pause / marker 都是独立进程：内存里的
        _active_session 必然为空，旧实现直接拒绝"当前无活跃录制会话"，
        与帮助文案承诺相反。控制/心跳都是 bundle 文件通道（ADR-02），
        跨进程天然可用——按"心跳新鲜 + 状态未封版"扫描即可接管。
        不认领 recorder 进程与租约（属启动方进程），只下发控制并等终态。
        """
        cutoff_ms = int(time.time() * 1000) - int(HEARTBEAT_STALE_S * 3 * 1000)
        best: tuple[int, str] | None = None
        try:
            dirs = list(self._root.glob("r-*"))
        except OSError:
            return None
        for sdir in dirs:
            try:
                reader = BundleReader(sdir)
                if not reader.exists or reader.status() != "draft":
                    continue
                hb = reader.heartbeat()
            except RepeatError:
                continue
            if str(hb.get("state", "")) not in ("arming", "countdown", "recording", "paused"):
                continue
            ts = int(hb.get("ts", 0) or 0)
            if ts < cutoff_ms:
                continue
            if best is None or ts > best[0]:
                best = (ts, sdir.name)
        return best[1] if best else None

    # ── 录制：T08 stop ───────────────────────────────────────────
    async def stop_recording(self, session_id: str | None = None, *, discard: bool = False) -> dict[str, Any]:
        # 体检 P1-3：CLI 的 record --stop 是独立进程，本进程没有 _active_session
        # 却应能接管别的进程正在录的会话（控制走 bundle 文件，天然跨进程）。
        # 与 _control 一致：显式 sid 优先，否则回退到"心跳新鲜的 draft"发现。
        sid = session_id or self._active_session or self._discover_live_session()
        if not sid:
            raise RepeatError("RP-5004", "当前无活跃录制会话")
        reader = BundleReader(self._root / sid)
        if not reader.exists:
            raise RepeatError("RP-6001", f"录制会话不存在: {sid}")
        current = reader.status()
        # 幂等：已封版的会话不再写控制文件（CLI 的 record --stop 是独立进程，
        # 不能因为本进程没有 active_session 就拒绝，否则命令行无法结束录制）。
        if current in ("ready", "failed", "discarded"):
            if discard and current != "discarded":
                raise RepeatError("RP-5004", f"会话已封版（{current}），无法丢弃")
            doc = reader.session()
            self._db_upsert_session(sid, doc)
            return doc
        # 本进程正在录另一个会话 → 拒绝（避免控制指令写进无关 bundle）
        if self._active_session and self._active_session != sid:
            raise RepeatError("RP-5004",
                              f"会话 {sid} 不在录制中（当前活跃: {self._active_session}）")
        is_local = self._active_session == sid
        self._stopping.add(sid)
        reader.send_control({"cmd": "discard" if discard else "stop",
                             "reason": "discard" if discard else "api"})
        # 等终态：discard 只可能 discarded；stop 允许 ready/failed/discarded。
        # 轮数可配（体检 P2-10）：默认 140×0.15s≈21s，覆盖 recorder 收尾
        # （flush+manifest+横幅）；测试环境可缩短。
        wants = ("discarded",) if discard else ("ready", "failed", "discarded")
        rounds = max(1, int(getattr(self.cfg, "stop_wait_rounds", 140) or 140))
        status = "draft"
        try:
            for _ in range(rounds):
                await asyncio.sleep(0.15)
                try:
                    status = reader.status()
                except RepeatError:
                    continue
                if status in wants:
                    break
        finally:
            self._stopping.discard(sid)
        if is_local:
            if self._hb_task:
                self._hb_task.cancel()
                self._hb_task = None
            if self._recorder_proc is not None:
                try:
                    await asyncio.to_thread(self._recorder_proc.wait, 12)
                except Exception:  # noqa: BLE001
                    pass
                self._recorder_proc = None
            self._active_session = None
            self._release_lease()
        doc = reader.session()
        self._db_upsert_session(sid, doc)
        st = doc.get("status")
        if st == "ready":
            cnt = doc.get("counters", {})
            self._notify_user("info", "录制已保存",
                              f"{doc.get('label') or sid}：{cnt.get('events', 0)} 事件｜"
                              f"{round((cnt.get('bytes', 0) or 0) / 1e6, 1)} MB")
        elif st == "discarded":
            self._notify_user("info", "录制已丢弃", str(doc.get("label") or sid))
        elif st == "failed":
            err = doc.get("error") or {}
            self._notify_user("error", "录制失败", str(err.get("message", "")))
        return doc

    def _release_lease(self) -> None:
        if self._lease is not None:
            try:
                self._lease.release()
            except Exception:  # noqa: BLE001
                pass
            self._lease = None

    def _read_session(self, sid: str) -> dict[str, Any]:
        try:
            return BundleReader(self._root / sid).session()
        except RepeatError:
            p = self._root / sid / "session.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            raise

    # ── 会话 CRUD ────────────────────────────────────────────────
    def list_sessions(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        items, total = self._db_list_sessions(limit, offset)
        return {"items": items, "total": total}

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._read_session(session_id)

    def delete_session(self, session_id: str) -> bool:
        if session_id == self._active_session:
            raise RepeatError("RP-5004", "该会话正在录制中，请先停止")
        sdir = self._root / session_id
        existed = sdir.exists()
        cur = self._db.execute("DELETE FROM repeat_sessions WHERE id = ?", (session_id,))
        if existed:
            try:
                import shutil
                shutil.rmtree(sdir, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass
        return bool(cur.rowcount > 0 or existed)

    # ── 编译 ─────────────────────────────────────────────────────
    async def compile_session(self, session_id: str, *, use_llm: bool = False) -> CompileResult:
        sid = session_id
        reader = BundleReader(self._root / sid)
        reader.verify()  # RP-6001
        doc = reader.session()
        if doc.get("status") != "ready":
            # 报错必须带封版失败的**具体原因**，否则用户只看到 status=failed，
            # 不知道该重录还是能救（真机：录制器异常退出 code=1 时完全无从判断）。
            err = str((doc.get("error") or {}).get("message", "") or "")
            hint = "；请重新录制" if doc.get("status") == "failed" else ""
            raise RepeatError("RP-6001",
                              f"会话 {sid} 未封版（status={doc.get('status')}）"
                              + (f"：{err}" if err else "") + hint)
        events, truncated = reader.events()
        # 环境基线回填到图 meta（预检比对用）
        result = compile_session(events, session_id=sid, cfg=self.cfg)
        if truncated:
            result.warnings.append(_warn("CP-TRUNC", "", "事件流末尾不完整，最后片段可能缺失", "warn"))
        if use_llm and result.steps:
            result = await self._semantic_name(result, doc)
        # 把 env 基线写入 graph meta
        result.draft_graph.setdefault("meta", {})["env_base"] = doc.get("env") or {}
        result.draft_graph["meta"]["bundle_session"] = sid
        self._db_upsert_session(sid, {**doc, "compiled": True,
                                      "stats": result.stats.to_dict()})
        self._notify_user("info", "解析完成",
                          f"{result.stats.steps} 步｜警告 {len(result.warnings)}｜"
                          f"需确认 {result.stats.needs_review}")
        if self.cfg.vision_enabled and result.steps:
            self._spawn_vision(sid, result)
        return result

    # ── 视觉辅助（百炼 VL，旁路：失败只记日志，不影响主链路） ─────
    def _spawn_vision(self, sid: str, result: CompileResult) -> None:
        async def _job() -> None:
            try:
                summary = await self.vision_analyze(sid, result)
                self._notify_user(
                    "info", "视觉核对完成",
                    f"{summary.get('analyzed', 0)} 步｜疑似未生效 "
                    f"{len(summary.get('suspects') or [])} 步")
            except Exception as e:  # noqa: BLE001
                logger.info("vision: 会话 %s 视觉核对失败: %s", sid, e)

        try:
            asyncio.create_task(_job())
        except RuntimeError:
            logger.info("vision: 无事件循环，跳过视觉核对（会话 %s）", sid)

    async def vision_analyze(self, session_id: str,
                             result: CompileResult | None = None) -> dict[str, Any]:
        """对已封版会话做逐步视觉核对，结果落 bundle/vision.json + 会话摘要。"""
        from .vision.analyzer import analyze_session
        from .vision.client import BailianVisionClient

        sid = session_id
        if result is None:
            result = await self.compile_session(sid)
        client = BailianVisionClient(
            api_key=self.cfg.vision_api_key, model=self.cfg.vision_model,
            base_url=self.cfg.vision_base_url, timeout_s=self.cfg.vision_timeout_s,
            key_file=self.cfg.vision_api_key_file)
        summary = await analyze_session(
            client, self._root / sid,
            [s.to_dict() for s in result.steps],
            max_steps=self.cfg.vision_max_steps)
        try:
            (self._root / sid / "vision.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.info("vision: vision.json 写入失败: %s", e)
        try:
            doc = self._read_session(sid)
            vision = {"analyzed": summary.get("analyzed", 0),
                      "suspects": summary.get("suspects") or [],
                      "model": summary.get("model", "")}
            # bundle 的 session.json 是审查读取的事实来源（get_session 走这里），
            # DB 是列表索引——两处都写，与 stop/compile 的既有写法一致。
            (self._root / sid / "session.json").write_text(
                json.dumps({**doc, "vision": vision}, ensure_ascii=False),
                encoding="utf-8")
            self._db_upsert_session(sid, {**doc, "vision": vision})
        except Exception as e:  # noqa: BLE001
            logger.info("vision: 会话摘要回写失败: %s", e)
        return summary

    def vision_status(self, session_id: str) -> dict[str, Any]:
        p = self._root / session_id / "vision.json"
        if not p.is_file():
            return {"session_id": session_id, "available": False}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return {"session_id": session_id, "available": True, **data}
        except Exception as e:  # noqa: BLE001
            return {"session_id": session_id, "available": False, "error": str(e)}

    async def _semantic_name(self, result: CompileResult, doc: dict[str, Any]) -> CompileResult:
        """LLM 语义命名（可选；只改 label/描述，不动结构——§6.2 约束）。"""
        if not self.cfg.llm_semantic:
            return result
        try:
            from .interpreter.semantic import apply_semantic
            return await apply_semantic(result, self._model_chat)
        except Exception as e:  # noqa: BLE001
            logger.info("语义命名跳过: %s", e)
            return result

    def _model_chat(self) -> Any | None:
        try:
            from harness.main import _services  # 延迟到真正需要时（不在 desktop_repeat 顶层 import fastapi）
            from harness.modules.model_manager.provider_registry import ProviderRegistry
            if _services is None or not _services.has(ProviderRegistry):
                return None
            return _services.get(ProviderRegistry)
        except Exception:  # noqa: BLE001
            return None

    # ── 发布为工作流 ─────────────────────────────────────────────
    async def save_as_workflow(self, session_id: str, name: str, description: str = "") -> dict[str, Any]:
        result = await self.compile_session(session_id)
        if any(w.severity == "error" for w in result.warnings):
            errs = [w.message for w in result.warnings if w.severity == "error"]
            raise RepeatError("RP-6004", "流程定义非法：" + "；".join(errs))
        wf = self._require_workflow_service().create_workflow(name or "录制流程", description, result.draft_graph)
        wf = self._require_workflow_service().update_workflow(
            wf["id"], name or "录制流程", description, result.draft_graph)
        return {**wf, "compile": result.to_dict()}

    def _require_workflow_service(self) -> Any:
        if self._workflow_service is None:
            raise RepeatError("RP-7003", "工作流服务不可用")
        return self._workflow_service

    # ── 运行 ─────────────────────────────────────────────────────
    def _params_defs(self, graph: dict[str, Any]) -> list[dict[str, Any]]:
        for n in graph.get("nodes", []) or []:
            if n.get("type") == "start":
                data = n.get("data") or {}
                return list(data.get("params") or [])
        return []

    async def dry_run(self, workflow_id: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        wf = self._get_workflow(workflow_id)
        graph = wf["graph"]
        try:
            resolved, flags = resolve_params(self._params_defs(graph), params or {}, unattended=True)
        except RepeatError as e:
            return {"workflow_id": workflow_id, "ok": False, "error": e.to_dict(),
                    "params_needed": collect_required_param_names(self._params_defs(graph))}
        # 静态校验前置：非法步骤必须在"零副作用体检"阶段就暴露，
        # 而不是体检全绿、点回放才 422（真实用户路径：先干跑再运行）。
        try:
            steps = steps_from_graph(graph)
        except RepeatError as e:
            return {"workflow_id": workflow_id, "ok": False, "error": e.to_dict()}
        for s in steps:
            err = s.validate()
            if err:
                return {"workflow_id": workflow_id, "ok": False,
                        "error": {"code": "RP-6004", "message": err, "step_sid": s.sid},
                        "steps": []}
        runner = self._build_runner(wf, resolved, flags, graph, dry=True)
        report = await runner.dry()
        return report.to_dict()

    async def start_run(self, workflow_id: str, params: dict[str, Any] | None = None, *,
                        unattended: bool = False, allow_blind_coord: bool = False,
                        expected_graph_sha256: str | None = None) -> dict[str, Any]:
        wf = self._get_workflow(workflow_id)
        graph = wf["graph"]
        # 版本绑定（§8.5）
        if expected_graph_sha256:
            got = graph_sha256(graph)
            if got != expected_graph_sha256:
                raise RepeatError("RP-6004", "流程已更新（graph 哈希不一致），请重新导出技能",
                                  detail={"expected": expected_graph_sha256, "got": got})
        # 录制输入租约互斥（§5.6：录制中禁止回放）
        if self._active_session:
            raise RepeatError("RP-5004", f"正在录制会话 {self._active_session}，禁止同时回放")
        # 同一时刻只允许一个回放在跑：WinInputLease 对同进程是幂等的（同一实例
        # 重复 acquire 直接返回成功），若不做这层检查，多个挂起的 run 会同时注入
        # 输入、互相抢焦点（真机坐实：4 条 run 并存，全部卡在 suspended）。
        active = [rid for rid, r in self._runners.items()
                  if str(getattr(r.rec, "status", "")) in ("precheck", "running", "suspended",
                                                           "dry_run", "queued")]
        if active:
            raise RepeatError("RP-5004",
                              f"已有回放进行中（{active[0]}），请先中止或等待其结束",
                              detail={"run_id": active[0]})
        lease = self._platforms()["lease"]
        ok, holder = lease.acquire()
        if not ok:
            raise RepeatError("RP-5004", "输入设备租约被占用（已有录制/回放在进行）",
                              detail={"holder_pid": holder})
        self._lease = lease
        try:
            resolved, flags = resolve_params(self._params_defs(graph), params or {},
                                             unattended=unattended)
        except RepeatError as e:
            self._release_lease()
            if e.code in ("RP-8001", "RP-8002"):
                return {"rejected": True, "error": e.to_dict(),
                        "params": collect_required_param_names(self._params_defs(graph))}
            if e.code == "RP-8003":
                self._notify_user("warn", "无法无人值守运行", e.message)
                return {"rejected": True, "error": e.to_dict()}
            self._release_lease()
            raise
        # 步骤静态校验
        try:
            steps = steps_from_graph(graph)
        except RepeatError:
            self._release_lease()
            raise
        for s in steps:
            err = s.validate()
            if err:
                self._release_lease()
                raise RepeatError("RP-6004", err)
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        doc = self._start_run_record(run_id, workflow_id, graph, resolved, flags, unattended)
        self._repo.insert_run(run_id, workflow_id, doc, steps_total=len(steps))

        runner = self._build_runner(wf, resolved, flags, graph,
                                    run_id=run_id, unattended=unattended,
                                    allow_blind_coord=allow_blind_coord)
        self._runners[run_id] = runner
        task = asyncio.create_task(self._run_and_persist(run_id, runner))
        self._run_tasks[run_id] = task
        # 紧急停止热键
        self._start_emergency_hotkey(run_id, runner)
        return {"run_id": run_id, "status": "running",
                "workflow_id": workflow_id, "total_steps": len(steps),
                "vars": doc["vars"], "unattended": unattended}

    def _build_runner(self, wf: dict[str, Any], resolved: dict[str, Any],
                      flags: dict[str, bool], graph: dict[str, Any], *,
                      run_id: str = "", unattended: bool = False,
                      dry: bool = False, allow_blind_coord: bool = False) -> RepeatRunner:
        sid_bundle = str((graph.get("meta") or {}).get("bundle_session", "")) or None
        bundle_dir = self._root / sid_bundle if sid_bundle else None
        anchor_loader = None
        frame_loader = None
        if sid_bundle:
            reader = BundleReader(self._root / sid_bundle)

            def anchor_loader(rel: str) -> bytes | None:
                try:
                    return reader.anchor_bytes(rel)
                except Exception:  # noqa: BLE001
                    return None

            def frame_loader(rel: str) -> bytes | None:
                try:
                    return reader.frame_bytes(rel)
                except Exception:  # noqa: BLE001
                    return None
        gate = SafetyGate(mode="auto" if unattended else "interactive",
                          approval_provider=self._make_approval_provider(run_id),
                          allow_password=self.cfg.allow_password_replay,
                          approval_policy=getattr(self.cfg, "approval_policy", "degraded"),
                          _audit_cb=self._make_audit_cb(run_id))
        self._gates[run_id or "dry"] = gate
        cfg = self.cfg
        run_root = self._root / (run_id or "dry")
        try:
            run_root.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        runner = RepeatRunner(
            workflow_id=wf["id"], graph=graph, resolved=resolved, flags=flags,
            cfg=cfg, inspector=self._platforms()["inspector"],
            injector=self._platforms()["injector"], windows=self._platforms()["windows"],
            env_provider=self._platforms()["env"], gate=gate,
            run_id=run_id or f"dry-{uuid.uuid4().hex[:8]}",
            reporter=Reporter(run_id or "dry", repeat_progress_store),
            emergency_stop=self._emergency.setdefault(run_id or "dry", threading.Event()),
            bundle_dir=bundle_dir, anchor_loader=anchor_loader, frame_loader=frame_loader,
            run_root=run_root, unattended=unattended, allow_blind_coord=allow_blind_coord,
            takeover=self._platforms().get("takeover"),
        )
        if dry:
            return runner
        return runner

    def _make_approval_provider(self, run_id: str) -> Callable[[dict[str, Any]], Decision]:
        def provider(payload: dict[str, Any]) -> Decision:
            # REST 人工通道：pending 记录 + 通知；异步等待由 runner 的 _await_resume 承担。
            self._repo.insert_approval(run_id, payload)
            self._notify_user("warn", "回放等待审批",
                              f"步骤 {payload.get('sid')}（{payload.get('action')}，{payload.get('risk')}）")
            # 默认拒绝自动：返回 pending_approval 由 runner 挂起
            return Decision(False, "等待人工审批", True, "")
        return provider

    def _make_audit_cb(self, run_id: str) -> Callable[[dict[str, Any]], None]:
        def cb(rec: dict[str, Any]) -> None:
            self._repo.insert_audit(rec)
        return cb

    async def _run_and_persist(self, run_id: str, runner: RepeatRunner) -> None:
        # 运行态增量落库：挂起/长时间执行期间 DB 也必须反映真实状态，
        # 否则页面刷新后只能看到 insert 时的 running/done_steps=0。
        sync = asyncio.create_task(self._sync_run_state(run_id, runner))
        try:
            rec = await runner.run()
            await asyncio.to_thread(self._persist_run, run_id, rec)
        except Exception as e:  # noqa: BLE001
            logger.exception("repeat run 异常")
            await asyncio.to_thread(self._repo.finish_run, run_id,
                                    {"status": "failed", "error_code": "RP-7003",
                                     "error_message": str(e)})
            if not repeat_progress_store.has_terminal(run_id):
                repeat_progress_store.emit(run_id, {"type": "run_error", "run_id": run_id,
                                                    "code": "RP-7003", "message": str(e),
                                                    "timestamp": datetime.now().astimezone()
                                                    .isoformat(timespec="seconds")})
        finally:
            sync.cancel()
            self._cleanup_run(run_id)

    async def _sync_run_state(self, run_id: str, runner: RepeatRunner) -> None:
        last = ""
        while True:
            await asyncio.sleep(0.8)
            try:
                d = runner.rec.to_dict()
                # 状态或已完成步数没变就不写库，避免无意义 UPDATE
                sig = f"{d.get('status')}|{(d.get('stats') or {}).get('done')}"
                if sig == last:
                    continue
                last = sig
                await asyncio.to_thread(self._repo.touch_run, run_id, d)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass

    def _persist_run(self, run_id: str, rec: Any) -> None:
        """全量落库：每步 StepRun → repeat_step_hits，收口 → repeat_runs。"""
        try:
            for sr in getattr(rec, "steps", []) or []:
                self._repo.record_step(run_id, sr)
            self._repo.finish_run(run_id, rec)
        except Exception as e:  # noqa: BLE001
            logger.warning("repeat run 落库失败（不影响运行结果）: %s", e)

    def _cleanup_run(self, run_id: str) -> None:
        self._runners.pop(run_id, None)
        self._run_tasks.pop(run_id, None)
        ev = self._emergency.pop(run_id, None)
        if ev is not None:
            ev.set()
        self._gates.pop(run_id, None)
        self._stop_emergency_hotkey()
        # 运行结束释放输入租约
        self._release_lease()

    async def resume_run(self, run_id: str, decision: str) -> dict[str, Any]:
        # 先校验裁决词：无论运行是否还在内存中，非法值都必须拒绝（旧实现
        # 在 runner 已回收时直接返回 DB 记录，非法裁决被静默接受）。
        if decision not in ("retry", "skip", "abort"):
            raise RepeatError("RP-6004", f"非法裁决: {decision}（仅 retry/skip/abort）")
        runner = self._runners.get(run_id)
        if runner is None:
            rec = self._run_record_from_db(run_id)
            if rec is None:
                raise RepeatError("RP-6004", f"运行不存在: {run_id}")
            return rec
        if decision == "abort":
            runner.abort()
        else:
            runner.resume(decision)
        await asyncio.sleep(0)
        return self._run_record_from_db(run_id) or {"run_id": run_id, "status": runner.rec.status}

    async def abort_run(self, run_id: str, reason: str = "user") -> dict[str, Any]:
        runner = self._runners.get(run_id)
        if runner is None:
            rec = self._run_record_from_db(run_id)
            if rec is None:
                raise RepeatError("RP-6004", f"运行不存在: {run_id}")
            return {"run_id": run_id, "status": rec.get("status", "finished")}
        runner.abort(reason)
        return {"run_id": run_id, "status": runner.rec.status}

    def get_run(self, run_id: str) -> dict[str, Any]:
        runner = self._runners.get(run_id)
        if runner is not None:
            return runner.rec.to_dict()
        dbrec = self._run_record_from_db(run_id)
        if dbrec is not None:
            return dbrec
        return {"run_id": run_id, "status": "not_found"}

    def list_runs(self, workflow_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self._repo.list_runs(workflow_id, limit)

    async def subscribe(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        async for ev in repeat_progress_store.subscribe(run_id):
            yield ev

    def status(self) -> dict[str, Any]:
        rec_state = None
        rec_events = 0
        rec_started = ""
        rec_error = ""
        if self._active_session:
            try:
                reader = BundleReader(self._root / self._active_session)
                hb = reader.heartbeat()
                rec_state = str(hb.get("state", "") or "")
                rec_events = int(hb.get("events", 0) or 0)
                doc = self._read_session(self._active_session)
                rec_started = str(doc.get("created_at", "") or "")
                rec_error = str((doc.get("error") or {}).get("message", "") or "")
            except Exception:  # noqa: BLE001
                rec_state = ""
        return {
            "available": self.available(),
            "active_session": self._active_session,
            "recording_state": rec_state,   # arming/countdown/recording/paused/finalizing
            # 录制中实时反馈：前端据此显示"已录 N 事件 / 时长"
            "recording_events": rec_events,
            "recording_started_at": rec_started,
            "recording_error": rec_error,
            "running_runs": list(self._runners),
            "config": self.cfg.to_public_dict(),
        }

    # ── 技能导出（§8.5） ─────────────────────────────────────────
    async def export_skill(self, workflow_id: str, slug: str | None = None,
                           description_hint: str = "") -> dict[str, Any]:
        from .skill.generator import generate_skill
        wf = self._get_workflow(workflow_id)
        version = int(wf.get("version", 1) or 1)
        skills_root = self._default_user_skills_root()
        sdir = await asyncio.to_thread(
            generate_skill, wf, skills_root=skills_root, slug=slug,
            description_hint=description_hint)
        if self._skill_service is not None:
            try:
                self._skill_service.reload()  # 若有 reload 接口
            except Exception:  # noqa: BLE001
                pass
        return {"path": str(sdir), "skill_dir": sdir.name,
                "workflow_id": workflow_id, "workflow_version": version,
                "graph_sha256": graph_sha256(wf["graph"])}

    def _default_user_skills_root(self) -> Path:
        import os
        base = os.path.expanduser("~/.andy-harness/skills")
        p = Path(base)
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ── 紧急停止热键（§场景 12/P17） ─────────────────────────────
    def _start_emergency_hotkey(self, run_id: str, runner: RepeatRunner) -> None:
        """全局紧急停止热键（默认 Ctrl+Alt+.）：独立于前台窗口；热键监听挂起时
        以 300ms GetKeyState 轮询兜底（RegisterHotKey 不可用的受限环境）。
        无 pynput 依赖时静默降级（run 仍可用 REST/工具中止）。"""
        if self._hotkey_stop is not None:
            return
        stop_evt = threading.Event()
        self._hotkey_stop = stop_evt
        try:
            from pynput import keyboard
            spec = [p.strip().lower() for p in self.cfg.emergency_hotkey.split("+") if p.strip()]
            mods = {m for m in ("ctrl", "alt", "shift") if m in spec}
            key = next((k for k in spec if k not in mods), ".")

            def on_press(k: Any) -> None:
                try:
                    name = (getattr(k, "name", None) or getattr(k, "char", None) or "")
                    if str(name).lower() != key:
                        return
                    if all(_mod_held(m) for m in mods):
                        runner.abort("hotkey")
                except Exception:  # noqa: BLE001
                    pass

            listener = keyboard.Listener(on_press=on_press, suppress=False)
            listener.start()
            threading.Thread(target=self._hotkey_reaper, args=(listener, stop_evt),
                             daemon=True, name="repeat-hotkey-reaper").start()
        except Exception as e:  # noqa: BLE001
            logger.info("紧急停止热键不可用（静默降级，可用 REST/工具中止）: %s", e)
            self._hotkey_stop = None
            self._hotkey_listener = None

    @staticmethod
    def _hotkey_reaper(listener: Any, stop: threading.Event) -> None:
        stop.wait(30 * 60)  # 运行超时上限之外自动回收
        try:
            listener.stop()
        except Exception:  # noqa: BLE001
            pass

    def _stop_emergency_hotkey(self) -> None:
        ev = self._hotkey_stop
        if ev is not None:
            ev.set()
            self._hotkey_stop = None
        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:  # noqa: BLE001
                pass
            self._hotkey_listener = None

    # ── 杂项 ─────────────────────────────────────────────────────
    def _get_workflow(self, workflow_id: str) -> dict[str, Any]:
        svc = self._require_workflow_service()
        wf = svc.get_workflow(workflow_id)
        if wf is None:
            raise RepeatError("RP-6004", f"工作流不存在: {workflow_id}")
        return wf if isinstance(wf, dict) else dict(wf)

    def _start_run_record(self, run_id: str, workflow_id: str, graph: dict[str, Any],
                          resolved: dict[str, Any], flags: dict[str, bool],
                          unattended: bool) -> dict[str, Any]:
        from .executor.params import redact_vars
        return {"run_id": run_id, "workflow_id": workflow_id,
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "vars": redact_vars(resolved, flags),
                "unattended": unattended,
                "graph_sha256": graph_sha256(graph),
                "status": "running"}

    def _run_record_from_db(self, run_id: str) -> dict[str, Any] | None:
        return self._repo.get_run(run_id)

    def _notify_user(self, level: str, title: str, body: str) -> None:
        if self._notify is not None:
            try:
                self._notify.toast(title, body)
            except Exception:  # noqa: BLE001
                pass
        repeat_progress_store.emit("notices", {"type": "notice", "level": level,
                                               "title": title, "body": body,
                                               "timestamp": datetime.now().astimezone().isoformat(timespec="seconds")})

    # ── 嵌入执行（GraphEngine 的 gui 节点走这里，§2.5） ───────────
    async def run_embedded_step(self, step_dict: dict[str, Any],
                                resolved: dict[str, Any], flags: dict[str, bool],
                                *, dry: bool = False) -> dict[str, Any]:
        """在混合工作流中执行单个 gui 步骤（复用定位链/动作/护栏）。

        与整段回放的差异：不建 repeat_runs 记录、不落检查点（属于宿主工作流的子步）；
        真实执行仍需输入租约（GUI 动作天然独占）。返回 {ok, status, hit_index, code, message}。
        """
        from .models import StepDef

        step = StepDef.from_dict(step_dict)
        err = step.validate()
        if err:
            raise RepeatError("RP-6004", err)
        # 单步伪图（供 runner 初始化作用域/环境）
        pseudo = {"nodes": [
            {"id": "n0", "type": "start", "data": {"params": []}},
            {"id": "n1", "type": "gui", "data": step.to_dict()},
            {"id": "n2", "type": "end", "data": {}},
        ], "edges": [
            {"id": "e0", "source": "n0", "target": "n1"},
            {"id": "e1", "source": "n1", "target": "n2"},
        ], "meta": ({"env_base": {}} if dry else {"env_base": {}})}
        run_id = f"emb-{uuid.uuid4().hex[:8]}"
        if dry:
            runner = self._build_runner({"id": "embedded", "graph": pseudo}, resolved, flags, pseudo,
                                        run_id=run_id, dry=True)
            rep = await runner.dry()
            ds = rep.steps[0] if rep.steps else None
            return {"ok": bool(ds and ds.hit_index >= 0), "status": "dry",
                    "hit_index": ds.hit_index if ds else -1, "message": ds.message if ds else ""}
        # 真实：租约 + 单步执行
        if self._active_session:
            raise RepeatError("RP-5004", f"正在录制 {self._active_session}，禁止执行 GUI 步骤")
        lease = self._platforms()["lease"]
        ok, holder = lease.acquire()
        if not ok:
            raise RepeatError("RP-5004", "输入设备租约被占用", detail={"holder_pid": holder})
        self._lease = lease
        try:
            runner = self._build_runner({"id": "embedded", "graph": pseudo}, resolved, flags, pseudo,
                                        run_id=run_id, unattended=False)
            await runner._precheck()  # noqa: SLF001 - 复用轻量预检
            outcome = await runner._run_step(0, step)  # noqa: SLF001
            sr = runner.rec.steps[-1] if runner.rec.steps else None
            return {"ok": sr is not None and sr.status in ("done", "degraded", "skipped"),
                    "status": sr.status if sr else outcome,
                    "hit_index": sr.hit_index if sr else -1,
                    "code": sr.code if sr else "", "message": sr.message if sr else ""}
        except RepeatError as e:
            return {"ok": False, "status": "failed", "code": e.code, "message": e.message}
        finally:
            self._release_lease()
            self._cleanup_run(run_id)

    # ── 维护 ─────────────────────────────────────────────────────
    GC_INTERVAL_S = 6 * 3600.0

    def _ensure_gc_task(self) -> None:
        """帧 GC 周期任务接线（体检 P1-5）。

        gc()/gc_session_frames 实现完整但全仓无调用点——standard/forensic 档
        录制帧 30/7 天保留策略形同虚设，bundle 只增不减（forensic 单次可数百
        MB）。挂在服务自身而非 scheduler：任何启用 andy-repeat 的部署都自动
        生效，不依赖用户另建定时任务。低频（6h）且异常静默，绝不影响录制/回放。
        """
        if self._gc_task is not None and not self._gc_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 无事件循环（CLI 同步路径）：跳过，下次调用再挂
        self._gc_task = loop.create_task(self._gc_loop())

    async def _gc_loop(self) -> None:
        try:
            # 启动先静默一段时间：避开服务冷启动峰值，首轮在 5 分钟后
            await asyncio.sleep(300)
            while True:
                try:
                    n = await asyncio.to_thread(self._gc_once)
                    if n:
                        logger.info("repeat 帧 GC：清理 %d 个过期帧文件", n)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.debug("repeat 帧 GC 异常（下轮再试）", exc_info=True)
                await asyncio.sleep(self.GC_INTERVAL_S)
        except asyncio.CancelledError:
            return

    def _gc_once(self) -> int:
        """同步执行一轮 GC（供 to_thread）。跳过本进程正在录的会话。"""
        n = 0
        for sdir in self._root.glob("r-*"):
            if sdir.name == self._active_session:
                continue
            try:
                from .store.bundle import gc_session_frames
                n += gc_session_frames(sdir, self.cfg)
            except Exception:  # noqa: BLE001
                continue
        return n

    async def gc(self) -> int:
        """帧 GC（§4.7）。返回删除帧数。"""
        return await asyncio.to_thread(self._gc_once)

    def ensure_maintenance(self) -> None:
        """对外幂等入口：确保帧 GC 周期任务已挂（插件 activate 调用）。"""
        self._ensure_gc_task()

    def shutdown(self) -> None:
        for t in list(self._run_tasks.values()):
            t.cancel()
        if self._hb_task is not None:
            self._hb_task.cancel()
        if self._gc_task is not None:
            self._gc_task.cancel()
            self._gc_task = None
        self._stop_emergency_hotkey()
        self._release_lease()
        if self._recorder_proc is not None:
            try:
                self._recorder_proc.terminate()
            except Exception:  # noqa: BLE001
                pass


def _warn(code: str, sid: str, msg: str, sev: str) -> CompileWarning:
    return CompileWarning(code, sid, msg, sev)


def _mod_held(name: str) -> bool:
    """查询修饰键物理状态（GetKeyState 高位=按下）。"""
    try:
        import ctypes
        vk = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10}[name]
        return bool(ctypes.windll.user32.GetKeyState(vk) & 0x8000)
    except Exception:  # noqa: BLE001
        return False
