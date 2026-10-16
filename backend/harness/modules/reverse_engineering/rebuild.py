"""功能等价重建 —— 由逆向证据包生成新的可执行文件（TCC 编译的原生 PE）。

**唯一实现，双入口复用**：流水线阶段（``rebuild_exe``）与 REST
（``POST /api/reverse-engineering/runs/{id}/rebuild``）都调用本模块，保证
"自动产出"与"手工重建"行为一致。

重建工具本身是 ``src/rev_eng/tools/rebuild_gui_exe.py``（CLI）：读取
``runs/<run_id>/run.json`` 的静态事实 + 原样本（对话框/图标/版本/载荷），
按能力特征自动选择 UI 形态并用项目内隔离的 TCC 编译成原生 PE。

因此**调用前必须保证 run.json 已包含该 run 的完整事实**（流水线在重建阶段
前会先落盘一次快照；对已完成的 run 直接调用即可）。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 重建（含 TCC 编译）通常数秒级；给足余量但必须有上限，避免挂死调用方。
DEFAULT_TIMEOUT_S = 300


@dataclass
class RebuildResult:
    """一次重建的结果（可直接序列化给 REST / 前端 / 证据）。"""

    ok: bool
    run_id: str
    exe_path: str = ""
    exe_name: str = ""
    size: int = 0
    sha256: str = ""
    kind: str = ""
    title: str = ""
    log: str = ""
    error: str = ""
    artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "run_id": self.run_id,
            "exe_path": self.exe_path,
            "exe_name": self.exe_name,
            "size": self.size,
            "sha256": self.sha256,
            "kind": self.kind,
            "title": self.title,
            "log": self.log,  # 完整工具输出：失败时是唯一诊断依据，成功时记录形态选择依据
            "error": self.error,
            "artifacts": list(self.artifacts),
        }


def find_tool() -> Path | None:
    """定位重建脚本（向上搜索 rev_eng/tools/rebuild_gui_exe.py）。"""
    for base in Path(__file__).resolve().parents:
        cand = base / "rev_eng" / "tools" / "rebuild_gui_exe.py"
        if cand.is_file():
            return cand
    return None


def find_tcc() -> Path | None:
    """定位项目内隔离的 TCC 编译器（.tools/tcc/tcc/tcc.exe）。"""
    for base in Path(__file__).resolve().parents:
        cand = base / ".tools" / "tcc" / "tcc" / "tcc.exe"
        if cand.is_file():
            return cand
    return None


def _parse_kv(log: str) -> dict[str, str]:
    """解析工具的输出行（``[rebuild] key : value``）为键值对。"""
    out: dict[str, str] = {}
    for line in log.splitlines():
        if not line.startswith("[rebuild]"):
            continue
        body = line[len("[rebuild]") :].strip()
        key, sep, value = body.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _exe_from_log(kv: dict[str, str]) -> Path | None:
    """从 ``[rebuild] exe : <path>  (N,NNN bytes)`` 行取输出路径。"""
    raw = kv.get("exe", "")
    if not raw:
        return None
    # 形如 "H:\...\x_rebuilt.exe  (18,944 bytes)[  +payload 1,325,339B]"
    match = re.match(r"^(.*?\.exe)\s+\(([\d,]+)\s*bytes\)", raw)
    path_str = match.group(1).strip() if match else raw.split("  (")[0].strip()
    p = Path(path_str)
    return p if p.is_file() else None


def run_rebuild(
    run_id: str,
    *,
    kind: str | None = None,
    runs_dir: str | Path | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> RebuildResult:
    """同步执行一次重建（阻塞；异步调用方请用 ``asyncio.to_thread`` 包装）。

    :param run_id: 已完成的 run（其 ``run.json`` 必须含静态事实）
    :param kind: 强制指定重建形态；``None`` 表示按样本能力特征自动选择
    :param runs_dir: 自定义 runs 根目录（流水线可注入自己的 runs_dir；缺省用工具默认）
    """
    tool = find_tool()
    if tool is None:
        return RebuildResult(ok=False, run_id=run_id, error="重建工具不存在：rev_eng/tools/rebuild_gui_exe.py")
    if find_tcc() is None:
        return RebuildResult(ok=False, run_id=run_id, error="TCC 编译器未就位：.tools/tcc/tcc/tcc.exe")

    cmd = [sys.executable, str(tool), "--run", run_id]
    if runs_dir is not None:
        cmd += ["--runs-dir", str(runs_dir)]
    if kind:
        cmd += ["--kind", kind]
    try:
        proc = subprocess.run(  # noqa: S603 —— 固定脚本 + 受控参数，无 shell
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(tool.parent),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return RebuildResult(ok=False, run_id=run_id, error=f"重建超时（>{timeout_s}s）")
    except OSError as exc:
        return RebuildResult(ok=False, run_id=run_id, error=f"无法启动重建工具：{exc}")

    log = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return RebuildResult(ok=False, run_id=run_id, log=log, error=f"重建失败（exit={proc.returncode}）")

    kv = _parse_kv(log)
    exe = _exe_from_log(kv)
    if exe is None:
        return RebuildResult(ok=False, run_id=run_id, log=log, error="重建完成但未定位到输出 exe")

    data = exe.read_bytes()
    return RebuildResult(
        ok=True,
        run_id=run_id,
        exe_path=str(exe),
        exe_name=exe.name,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        kind=kv.get("kind", "").split()[0] if kv.get("kind") else "",
        title=kv.get("title", ""),
        log=log,
        artifacts=[str(exe), str(exe.parent / "app.ico"), str(exe.parent / "rebuilt.c")],
    )


def record_rebuild(run_dir: Path, result: RebuildResult) -> bool:
    """把重建结果写回 ``run.json``（供运行详情 / 前端展示；幂等覆盖）。

    只增量更新 ``stages.rebuild_exe`` 与顶层 ``rebuild`` 字段，不改动既有阶段结果。
    写入失败返回 False（调用方不应因此失败——重建产物本身已落盘）。
    """
    run_json = run_dir / "run.json"
    if not run_json.is_file():
        return False
    try:
        data = json.loads(run_json.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        stages = data.setdefault("stages", {})
        if not isinstance(stages, dict):
            return False
        stages["rebuild_exe"] = {
            "stage": "rebuild_exe",
            "status": "ok" if result.ok else "failed",
            "engine": "tcc-rebuild" if result.ok else "none",
            "target_engine": "tcc-rebuild",
            "summary": (
                f"已生成功能等价的新 exe：{result.exe_name}（{result.size:,} 字节，形态 {result.kind}）"
                if result.ok
                else f"重建失败：{result.error}"
            ),
            "facts": result.to_dict(),
            "artifacts": [],
            "logs": [],
            "error": result.error,
            "duration_ms": 0,
        }
        data["rebuild"] = result.to_dict()
        run_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except (OSError, json.JSONDecodeError):
        return False
