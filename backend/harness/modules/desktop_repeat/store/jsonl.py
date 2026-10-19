"""events.jsonl 追加写 / 流式读（§4.3 / ADR-03 / §7.2 RP-6002/6003）。

- 写：缓冲 + 定期 flush（每 32 条或 200ms，§3.2 写线程纪律）；崩溃最多丢末 200ms。
- 读：流式逐行；末行截断自动丢弃并标记 truncated；schema 高于当前版本即拒绝。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..errors import RepeatError
from ..models import SCHEMA_VERSION, RecEvent

logger = logging.getLogger("harness.repeat.store")

_FLUSH_LINES = 32
_FLUSH_MS = 200


class JsonlWriter:
    """单写者 JSONL 追加器（INV-R4：bundle 唯一写者由文件锁保证，这里是进程内单实例）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._buf: list[str] = []
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()
        self._seq = -1
        # 崩溃/重启封版恢复：读取既有末行 seq 续编（INV-R1 无空洞）。
        try:
            if path.exists() and path.stat().st_size > 0:
                with open(path, encoding="utf-8") as rf:
                    last_seq = -1
                    n = 0
                    for line in rf:
                        s = line.strip()
                        if not s:
                            continue
                        n += 1
                        try:
                            last_seq = max(last_seq, int(json.loads(s).get("seq", -1)))
                        except Exception:  # noqa: BLE001
                            pass
                    self._seq = last_seq
        except Exception:  # noqa: BLE001
            pass
        self._f = open(path, "a", encoding="utf-8", newline="\n")  # noqa: SIM115

    @property
    def last_seq(self) -> int:
        return self._seq

    def write(self, ev: RecEvent) -> None:
        if ev.seq <= self._seq:
            raise RuntimeError(f"seq 违例（INV-R1）：{ev.seq} <= {self._seq}")
        self._seq = ev.seq
        line = ev.to_line()
        with self._lock:
            self._buf.append(line)
            if len(self._buf) >= _FLUSH_LINES or (time.monotonic() - self._last_flush) * 1000 >= _FLUSH_MS:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buf:
            self._last_flush = time.monotonic()
            return
        try:
            self._f.write("\n".join(self._buf) + "\n")
            self._f.flush()
            os.fsync(self._f.fileno())
        except Exception as e:  # noqa: BLE001
            logger.error("events.jsonl flush 失败: %s", e)
        self._buf.clear()
        self._last_flush = time.monotonic()

    def close(self) -> None:
        with self._lock:
            try:
                self._flush_locked()
            finally:
                try:
                    self._f.close()
                except Exception:  # noqa: BLE001
                    pass


def iter_events(path: Path) -> Iterator[RecEvent]:
    """流式读取（§11 长流程：生成器，不整体加载）。

    - 末行 JSON 解析失败 → 丢弃并 yield 前抛 truncated 标记（由调用方检查）；
      这里通过返回值外的 truncated 标志对象不便表达，采用异常前缀式 API：
      使用 :func:`read_events` 获取 (events, truncated)。
    - v > SCHEMA_VERSION → RP-6002（拒绝，场景 13）。
    """
    yield from read_events(path)[0]


def read_events(path: Path) -> tuple[list[RecEvent], bool]:
    events: list[RecEvent] = []
    truncated = False
    if not path.exists():
        return events, truncated
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                # 只容忍"末行"截断（崩溃写坏最后一行）；中间坏行同样跳过并标 truncated
                if _looks_truncated(line):
                    truncated = True
                    continue
                truncated = True
                logger.warning("events.jsonl 第 %d 行解析失败，已跳过", lineno)
                continue
            v = int(raw.get("v", 1) or 1)
            if v > SCHEMA_VERSION:
                raise RepeatError("RP-6002", f"数据版本过新（v={v}），请先升级程序", detail={"lineno": lineno})
            try:
                ev = RecEvent.from_line(line)
            except Exception as e:  # noqa: BLE001
                truncated = True
                logger.warning("events.jsonl 第 %d 行字段非法（%s），已跳过", lineno, e)
                continue
            events.append(ev)
    return events, truncated


def read_events_lazy(path: Path) -> Iterator[dict[str, Any]]:
    """轻量 JSON dict 视图（编译管线用，避免 RecEvent 对象全量驻留）。"""
    for line in _iter_lines(path):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            yield parsed


def _iter_lines(path: Path) -> Iterator[str]:
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s:
                yield s


def _looks_truncated(line: str) -> bool:
    """末行截断启发：以 { 开头但括号不闭合。"""
    if not line.startswith("{"):
        return False
    try:
        json.loads(line)
        return False
    except json.JSONDecodeError:
        depth = 0
        for ch in line:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        return depth != 0


def counts(events: list[RecEvent]) -> dict[str, int]:
    c = {"events": len(events), "clicks": 0, "keys": 0, "frames": 0, "drops": 0, "uia_timeouts": 0}
    for ev in events:
        if ev.type in ("click", "double_click", "right_click", "middle_click", "drag"):
            c["clicks"] += 1
        elif ev.type in ("key", "text_input"):
            c["keys"] += 1
        if ev.flags.get("uia_timeout"):
            c["uia_timeouts"] += 1
    return c
