"""manifest 封版与校验（§4.6）。

root_sha256 = sha256( concat( sorted("path:sha256\\n") ) )。
校验时机：编译前、回放前、技能导出前；任一不符 → RP-6001。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from ..errors import RepeatError
from ..models import SCHEMA_VERSION

# 清单排除目录（runtime 为瞬时文件，不入清单，§4.1）
EXCLUDE_DIRS = ("runtime",)
# 清单排除单文件：
# - manifest.json：清单不含自身（自哈希悖论；它自己的完整性由 session.json.integrity 锚定）
# - session.json：可变指针元数据（finalize 在 manifest 写完后仍要回写 ready 态与 integrity）；
#   证据不可变性由 events.jsonl + frames/ + anchors/ 的 root_sha256 锚定，
#   session.json 的内容哈希另存于 manifest 之外（integrity.events_sha256 / manifest_sha256）。
EXCLUDE_FILES = ("manifest.json", "session.json")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_root(files: list[dict[str, object]]) -> str:
    cat = "".join(sorted(f"{f['path']}:{f['sha256']}\n" for f in files))
    return hashlib.sha256(cat.encode("utf-8")).hexdigest()


def build_manifest(session_dir: Path, session_id: str) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for p in sorted(session_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(session_dir).as_posix()
        top = rel.split("/", 1)[0]
        if top in EXCLUDE_DIRS or rel in EXCLUDE_FILES or rel.endswith(".tmp"):
            continue
        files.append({"path": rel, "size": p.stat().st_size, "sha256": sha256_file(p)})
    return {
        "schema": SCHEMA_VERSION,
        "session_id": session_id,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "files": files,
        "root_sha256": compute_root(files),
    }


def write_manifest(session_dir: Path, manifest: dict[str, object]) -> None:
    # 原子写（tmp + os.replace，§4.6）
    import os
    tmp = session_dir / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, session_dir / "manifest.json")


def load_manifest(session_dir: Path) -> dict[str, object]:
    path = session_dir / "manifest.json"
    if not path.exists():
        raise RepeatError("RP-6001", "缺少 manifest.json，bundle 未封版或已损坏")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RepeatError("RP-6001", f"manifest.json 解析失败: {e}") from e
    m: dict[str, object] = parsed if isinstance(parsed, dict) else {}
    v = int(str(m.get("schema", 1) or 1))
    if v > SCHEMA_VERSION:
        raise RepeatError("RP-6002", f"manifest 版本过新（schema={v}）")
    return m


def verify_manifest(session_dir: Path, *, ignore_gc: bool = True) -> tuple[bool, list[str]]:
    """逐文件校验 + root 重算。返回 (ok, 差异文件清单)。

    ignore_gc=True：frames/ 允许被 GC 删除（§4.7：GC 只删帧并同步清单）——
    缺失的帧文件不算篡改；但存在的内容不符仍算失败。
    """
    m = load_manifest(session_dir)
    raw = m.get("files")
    items = raw if isinstance(raw, list) else []
    files = [f for f in items if isinstance(f, dict)]
    diffs: list[str] = []
    current: list[dict[str, object]] = []
    for item in files:
        rel = str(item.get("path", ""))
        want = str(item.get("sha256", ""))
        p = session_dir / rel
        if not p.exists():
            if ignore_gc and rel.startswith("frames/"):
                continue
            diffs.append(f"{rel}: 文件缺失")
            continue
        got = sha256_file(p)
        if got != want:
            diffs.append(f"{rel}: 内容变更 ({got[:8]}… ≠ {want[:8]}…)")
            continue
        current.append({"path": rel, "size": p.stat().st_size, "sha256": got})
    root = str(m.get("root_sha256", ""))
    recomputed = compute_root(current)
    if not ignore_gc and root and recomputed != root:
        # 帧被 GC 时 root 必然变化；非 GC 场景（封版即时校验）要求一致
        diffs.append(f"root_sha256 不一致 ({recomputed[:8]}… ≠ {root[:8]}…)")
    if diffs:
        raise RepeatError("RP-6001", "录制数据完整性校验失败",
                          detail={"diffs": diffs, "session": str(session_dir)})
    return True, []
