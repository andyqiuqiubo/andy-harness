"""bundle：录制会话自包含目录的读写编排（§4.1/§4.4/§4.7）。

- BundleWriter：录制器子进程唯一写者（文件锁 + pid 校验，INV-R4）；
  session.json / manifest.json 原子写，events.jsonl 只追加。
- BundleReader：读取端（服务/编译/回放共用），封版校验（RP-6001/6002/6003）。
- gc_session_frames：定期帧回收（只删 frames/，同步清单重算 root）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import RecordConfig, RepeatConfig
from ..errors import RepeatError
from ..models import SCHEMA_VERSION, Env, RecEvent
from . import manifest as mf
from .jsonl import JsonlWriter, read_events

SESSION_STATUSES = ("draft", "ready", "failed", "discarded")


def new_session_id() -> str:
    return f"r-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ── 单写者锁（INV-R4） ────────────────────────────────────────────
class BundleLock:
    """bundle 目录写锁：runtime/lock 内容 = pid。陈旧锁（pid 不存活）自动接管。"""

    def __init__(self, session_dir: Path) -> None:
        self._dir = session_dir
        self._path = session_dir / "runtime" / "lock"
        self._held = False

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            try:
                owner = int(self._path.read_text(encoding="utf-8").strip() or "0")
            except (ValueError, OSError):
                owner = 0
            # INV-R4 单写者：只要锁文件在且持有进程存活即拒绝（同进程的第二个
            # BundleLock 实例也算第二写者）。陈旧锁（持有进程已退出，如崩溃/重启
            # 后 pid 复用极小概率）清理接管。
            if owner and _pid_alive(owner):
                return False
            try:
                self._path.unlink()
            except OSError:
                pass
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            self._held = True
            return True
        except FileExistsError:
            return False

    def release(self) -> None:
        if self._held:
            try:
                self._path.unlink()
            except OSError:
                pass
            self._held = False

    def __enter__(self) -> BundleLock:
        if not self.acquire():
            raise RepeatError("RP-5004", "该录制会话正被另一进程写入")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import ctypes
        import ctypes.wintypes as wt

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # noqa: N806
        STILL_ACTIVE = 259  # noqa: N806
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wt.HANDLE
        kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = wt.DWORD(0)
            if kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                return int(code.value) == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return True  # 判不了就保守认为活（宁可拒绝双写）


# ── 写端 ────────────────────────────────────────────────────────
class BundleWriter:
    """录制期写者。生命周期 = 一次录制会话（子进程内实例化一次）。"""

    def __init__(self, sessions_root: Path, session_id: str,
                 cfg: RepeatConfig, rec: RecordConfig, env: Env) -> None:
        self.dir = sessions_root / session_id
        self.session_id = session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "frames").mkdir(exist_ok=True)
        (self.dir / "anchors").mkdir(exist_ok=True)
        (self.dir / "runtime").mkdir(exist_ok=True)
        self._env = env
        tier = rec.frame_tier or cfg.frame_tier
        self._merged_cfg: dict[str, Any] = {
            "frame_tier": tier,
            "countdown_s": rec.countdown_s if rec.countdown_s >= 0 else cfg.countdown_s,
            "uia_enabled": cfg.uia_enabled,
            "uia_timeout_ms": cfg.uia_timeout_ms,
            "capture_clipboard": cfg.capture_clipboard,
            "mask_windows": list(rec.mask_windows or cfg.mask_windows),
            "mask_keys": list(rec.mask_keys or cfg.mask_keys),
            "auto_pause_mask": list(rec.auto_pause_mask or cfg.auto_pause_mask),
            "hotkeys": dict(cfg.hotkeys),
        }
        self._created = _now_iso()
        self._frames = 0
        self._bytes_saved = 0
        self._writer = JsonlWriter(self.dir / "events.jsonl")
        self._seq = self._writer.last_seq + 1  # 续接既有文件末 seq+1（全新文件=-1+1=0；崩溃重启封版 INV-R1 无空洞）
        self._emit_lock = threading.Lock()
        self._frame_id = 0
        self._t0_mono = time.monotonic()
        self._t0_epoch_ms = int(time.time() * 1000)

    # ── session.json ────────────────────────────────────────────
    def _session_doc(self, status: str, *, ended_at: str = "", counters: dict[str, int] | None = None,
                     integrity: dict[str, Any] | None = None, error: dict[str, Any] | None = None,
                     extra: dict[str, Any] | None = None) -> dict[str, Any]:
        import socket
        doc: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "session_id": self.session_id,
            "status": status,
            "created_at": self._created,
            "ended_at": ended_at or None,
            "label": self._label,
            "operator": {"host": socket.gethostname(), "user": os.environ.get("USERNAME", ""),
                         "elevated": None},
            "env": self._env.to_dict(),
            "config": self._merged_cfg,
            "counters": counters or {},
            "integrity": integrity or {},
            "error": error,
        }
        doc.update(extra or {})
        return doc

    _label = ""

    @property
    def last_seq(self) -> int:
        """已分配的最大 seq（-1 = 尚未写入任何事件）。"""
        return self._writer.last_seq

    def set_label(self, label: str) -> None:
        self._label = str(label or "")[:120]

    def write_draft(self) -> None:
        self._write_session("draft")

    def _write_session(self, status: str, **kw: Any) -> None:
        from harness.infra.atomic_write import atomic_write_json
        kw.setdefault("counters", self.counters())
        atomic_write_json(self.dir / "session.json", self._session_doc(status, **kw), indent=2)

    # ── 事件 ─────────────────────────────────────────────────────
    def next_seq(self) -> int:
        """兼容保留：仅供已持有 _emit_lock 的内部路径使用。

        外部请走 emit()——它在同一把锁内分配 seq 并落盘，杜绝
        多线程「先分配后慢写入」导致的乱序（INV-R1 违例）。
        """
        s = self._seq
        self._seq += 1
        return s

    def next_frame_id(self) -> int:
        """帧文件名专用自增 id（仅需唯一，与事件 seq 解耦）。"""
        with self._emit_lock:
            self._frame_id += 1
            return self._frame_id

    def t_ms(self) -> int:
        return int((time.monotonic() - self._t0_mono) * 1000)

    def ts_ms(self) -> int:
        return self._t0_epoch_ms + self.t_ms()

    def emit(self, ev: RecEvent) -> None:
        # 分配 seq + 落盘在同一把锁内完成：多线程（worker 与 control 轮询）
        # 各自「先 alloc 后 attach 慢操作再 write」时，alloc 序与 write 序
        # 可能颠倒 → INV-R1 违例曾致 worker 线程崩溃、后续演示事件全丢。
        with self._emit_lock:
            if ev.seq < 0:
                ev.seq = self.next_seq()
            self._writer.write(ev)

    def flush(self) -> None:
        self._writer.flush()

    # ── 帧 ──────────────────────────────────────────────────────
    def save_frame(self, name: str, data: bytes) -> str | None:
        """写帧文件；超出单 bundle 上限返回 None 并告警（§4.7）。"""
        if self._bytes_saved + len(data) > self._max_bytes():
            return None
        path = self.dir / "frames" / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self._frames += 1
        self._bytes_saved += len(data)
        return f"frames/{name}"

    def _max_bytes(self) -> int:
        try:
            mb = int(self._merged_cfg.get("_bundle_max_mb") or 0)
        except (TypeError, ValueError):
            mb = 0
        return (mb or 500) * 1024 * 1024

    def counters(self) -> dict[str, int]:
        c = {"events": self._seq, "frames": self._frames, "bytes": self._bytes_saved}
        return c

    # ── 控制/心跳（ADR-02） ─────────────────────────────────────
    def write_heartbeat(self, state: str, *, events: int, drops: int, qsize: int,
                        paused_since_ms: int | None = None) -> None:
        hb = {
            "pid": os.getpid(),
            "state": state,                      # arming/countdown/recording/paused/finalizing
            "events": events, "drops": drops, "qsize": qsize,
            "paused_since_ms": paused_since_ms,
            "ts": int(time.time() * 1000),
        }
        self._atomic_json("runtime/heartbeat.json", hb)

    def read_control(self) -> dict[str, Any]:
        p = self.dir / "runtime" / "control.json"
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    def _atomic_json(self, rel: str, doc: dict[str, Any]) -> None:
        path = self.dir / rel
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    # ── 封版 / 终态 ─────────────────────────────────────────────
    def finalize(self, *, reason: str = "stop", error: str = "",
                 counters_extra: dict[str, int] | None = None) -> dict[str, Any]:
        """T08→T13/T14：flush → 完整性校验 → manifest 封版 → session ready/failed。"""
        self._writer.close()
        counters = self.counters()
        if counters_extra:
            counters.update(counters_extra)
        events, truncated = read_events(self.dir / "events.jsonl")
        counters.setdefault("events", len(events))
        if error:
            self._write_session("failed", ended_at=_now_iso(), counters=counters,
                                integrity={"truncated": truncated},
                                error={"reason": reason, "message": error})
            return {"status": "failed", "counters": counters, "truncated": truncated}
        if not events:
            self._write_session("failed", ended_at=_now_iso(), counters=counters,
                                integrity={"truncated": truncated},
                                error={"reason": "empty", "message": "未采集到任何事件"})
            return {"status": "failed", "counters": counters, "truncated": truncated}
        m = mf.build_manifest(self.dir, self.session_id)
        mf.write_manifest(self.dir, m)
        integrity = {
            "events_sha256": mf.sha256_file(self.dir / "events.jsonl"),
            "manifest_sha256": mf.sha256_file(self.dir / "manifest.json"),
            "root_sha256": str(m["root_sha256"]),
            "truncated": truncated,
        }
        self._write_session("ready", ended_at=_now_iso(), counters=counters, integrity=integrity)
        return {"status": "ready", "counters": counters, "truncated": truncated,
                "manifest": m}

    def mark_discarded(self) -> None:
        self._writer.close()
        # 软删：改名标记（30 天回收，§3.10 / §4.7）
        self._write_session("discarded", ended_at=_now_iso(), counters=self.counters())
        marker = self.dir / "runtime" / "discarded_at"
        marker.write_text(str(int(time.time())), encoding="utf-8")

    def close(self) -> None:
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001
            pass


# ── 读端 ────────────────────────────────────────────────────────
class BundleReader:
    def __init__(self, session_dir: Path) -> None:
        self.dir = Path(session_dir)
        self.session_id = self.dir.name

    @property
    def exists(self) -> bool:
        return (self.dir / "session.json").exists()

    def session(self) -> dict[str, Any]:
        p = self.dir / "session.json"
        if not p.exists():
            raise RepeatError("RP-6001", f"录制会话不存在: {self.dir}")
        try:
            parsed = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise RepeatError("RP-6001", f"session.json 损坏: {e}") from e
        if not isinstance(parsed, dict):
            raise RepeatError("RP-6001", "session.json 结构非法（期望对象）")
        d: dict[str, Any] = parsed
        v = int(d.get("schema", 1) or 1)
        if v > SCHEMA_VERSION:
            raise RepeatError("RP-6002", f"数据版本过新（schema={v}），请升级程序")
        return d

    def status(self) -> str:
        try:
            return str(self.session().get("status", "draft"))
        except RepeatError:
            return "draft"

    def heartbeat(self) -> dict[str, Any]:
        p = self.dir / "runtime" / "heartbeat.json"
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    def control(self) -> dict[str, Any]:
        p = self.dir / "runtime" / "control.json"
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    def send_control(self, cmd: dict[str, Any]) -> None:
        """服务进程 → recorder 的控制指令（合并写 control.json）。"""
        runtime = self.dir / "runtime"
        runtime.mkdir(exist_ok=True)
        cur = self.control()
        cur.update(cmd)
        cur["_rev"] = int(cur.get("_rev", 0)) + 1
        cur["_sent"] = int(time.time() * 1000)
        path = runtime / "control.json"
        tmp = path.with_name("control.json.tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def events(self) -> tuple[list[RecEvent], bool]:
        return read_events(self.dir / "events.jsonl")

    def env(self) -> Env:
        return Env.from_dict(self.session().get("env"))

    def verify(self) -> None:
        """封版校验（编译/回放前）。未封版 → RP-6001。"""
        mf.verify_manifest(self.dir)

    def frame_bytes(self, rel: str) -> bytes | None:
        p = self.dir / rel
        if p.exists():
            return p.read_bytes()
        return None

    def anchor_bytes(self, rel: str) -> bytes | None:
        p = self.dir / rel
        if p.exists():
            return p.read_bytes()
        return None

    def save_anchor(self, rel: str, data: bytes) -> str:
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return rel


# ── 帧 GC（§4.7，scheduler 每日调用） ────────────────────────────
def gc_session_frames(session_dir: Path, cfg: RepeatConfig) -> int:
    """只删 frames/ 过期帧，事件/锚点/清单保留；清单同步更新并重算 root。"""
    reader = BundleReader(session_dir)
    if not reader.exists:
        return 0
    doc = reader.session()
    if doc.get("status") != "ready":
        return 0
    tier = str((doc.get("config") or {}).get("frame_tier", "standard"))
    days = cfg.retention_days_forensic if tier == "forensic" else cfg.retention_days_standard
    cutoff = time.time() - days * 86400
    mpath = session_dir / "manifest.json"
    if not mpath.exists():
        return 0
    m = json.loads(mpath.read_text(encoding="utf-8"))
    kept: list[dict[str, Any]] = []
    removed = 0
    for item in m.get("files", []) or []:
        rel = str(item.get("path", ""))
        p = session_dir / rel
        if rel.startswith("frames/") and p.exists() and p.stat().st_mtime < cutoff:
            try:
                p.unlink()
                removed += 1
            except OSError:
                kept.append(item)
            continue
        kept.append(item)
    if removed:
        m["files"] = kept
        m["root_sha256"] = mf.compute_root(kept)
        m["gc_at"] = _now_iso()
        mf.write_manifest(session_dir, m)
    return removed
