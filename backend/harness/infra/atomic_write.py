"""原子文件写入工具（SEC-24）。

配置/状态文件的「读 → 改 → 覆盖写」若直接 ``write_text`` / ``open(path,"w")``，
存在两类损坏风险：

1. **进程崩溃**：写入中途断电/异常，文件被截断成半截 JSON，下次读入即解析
   失败，且原内容已丢失（用户配置/停用集合/授权记录等静默重置）。
2. **并发读写竞争**：另一协程/进程在读文件时恰逢本进程截断写入，读到残缺
   内容并抛出解析异常（表现为随机的 JSON 报错）。

正确做法：先写**同目录临时文件**，再 ``os.replace`` 原子替换目标——同一文件
系统内 rename 是原子的，读者只会看到完整的旧文件或完整的新文件，绝不会看到
半截内容。并发写则以临时文件后缀 + PID 区分，避免两个写者撞同一临时文件。

Windows 注意：``os.replace`` 可覆盖已存在文件（不像 ``os.rename`` 在目标被
占用时抛错），故跨平台可直接使用。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

__all__ = ["atomic_write_text", "atomic_write_json"]

# 临时文件后缀：区分写者实例，避免并发写者覆盖彼此的半成品。
_TMP_SUFFIX = f".{os.getpid()}.tmp"


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    chmod: int | None = None,
) -> None:
    """把 ``text`` 原子写入 ``path``（临时文件 + ``os.replace``）。

    Args:
        path: 目标文件路径（父目录不存在会自动创建）。
        text: 要写入的完整内容。
        encoding: 文本编码，默认 utf-8。
        chmod: 可选，替换后对目标施加的文件权限位（如 ``0o600``）。不适用
            / 设置失败时静默跳过（Windows 上 POSIX 位有限支持）。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # 临时文件与目标**同目录**：保证同一文件系统，os.replace 才是原子操作。
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=target.name + ".",
        suffix=_TMP_SUFFIX,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())  # 落盘，避免断电后 rename 了但数据未持久
        if chmod is not None:
            try:
                os.chmod(tmp, chmod)
            except OSError:
                pass
        # Windows 兜底：目标若被置为只读，先解除只读再替换
        if os.name == "nt" and target.exists():
            try:
                os.chmod(target, 0o200 | (target.stat().st_mode & 0o777))  # 加写权限
            except OSError:
                pass
        # Windows：目标文件可能被其他进程**短暂**打开（心跳线程读 session.json、
        # 杀软扫描新文件），此时 os.replace 抛 WinError 5。原实现直接失败，
        # 导致录制封版中断、会话永远停在 draft/failed（真机坐实：
        # r-20261019-155147-086adc 收尾时 PermissionError）。
        last_err: OSError | None = None
        for attempt in range(6):
            try:
                os.replace(tmp, target)  # 原子替换（覆盖已存在的目标）
                last_err = None
                break
            except PermissionError as e:  # noqa: PERF203
                last_err = e
                time.sleep(0.05 * (attempt + 1))
        if last_err is not None:
            raise last_err
        if chmod is not None:
            try:
                os.chmod(target, chmod)
            except OSError:
                pass
    except BaseException:
        # 失败时清理半成品临时文件，避免目录里堆积 .tmp 垃圾
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(
    path: str | Path,
    data: object,
    *,
    indent: int | None = 2,
    ensure_ascii: bool = False,
    chmod: int | None = None,
    default: Callable[[object], object] | None = None,
) -> None:
    """把 ``data`` 序列化为 JSON 后原子写入 ``path``。

    ``default`` 透传给 :func:`json.dumps`，用于处理无法直接序列化的类型
    （如 ``set``、``datetime``）。
    """
    text = json.dumps(data, ensure_ascii=ensure_ascii, indent=indent, default=default)
    # json.dump 默认不带尾换行；补一个让文件以换行结尾（更友好，也便于 diff）
    if not text.endswith("\n"):
        text += "\n"
    atomic_write_text(path, text, chmod=chmod)
