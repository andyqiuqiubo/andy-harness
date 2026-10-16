#!/usr/bin/env python
"""由逆向证据包重建一个**可运行的 GUI exe**（Win32 原生，TCC 编译）。

通用设计
--------
1. 输入只有工程强逆向流水线产出的 `run.json`（Evidence v2 事实台账），
   不做凭空臆造：
   - **窗口标题优先取样本 PE 的 RT_VERSION 资源**（FileDescription /
     ProductName），这是产品自称的权威来源；样本无版本信息时才回退到
     通用打分制（从样本自身字符串里挑"最像产品名"的串，达不到阈值就
     诚实使用样本名）；
   - 图标从样本 PE 资源段原样提取（.ico 回灌）；
   - 列表内容一律来自静态事实。
2. **形态由样本能力特征自动选择**（见 `rebuild_kinds.py`），不是写死的：

   ================  ============================================  ================
   形态              选用条件                                       产物
   ================  ============================================  ================
   process_manager   命中进程枚举特征（Toolhelp32/psapi/NtQuery…）  真枚举进程的管理器
   strings_browser   无进程特征但字符串充足（>=40 条）              可搜索字符串浏览器
   facts_browser     事实太少                                       纯事实总览（兜底）
   ================  ============================================  ================

   新增形态只需往 `rebuild_templates/` 放一个 `*.c.tpl` 并在
   `rebuild_kinds.KIND_TEMPLATES` 登记，本脚本无需改动。
3. 重建品是**原生 Win32 PE**（非脚本、非包装），双击即出界面；编译器
   TCC 隔离在项目目录 `.tools/tcc/`，不依赖系统 MSVC / MinGW / .NET。
   TCC 自带头文件不含 commctrl.h，全部结构体手工声明、API 经
   GetProcAddress 动态解析。
4. 生成的 exe 可再次作为流水线样本送进 REA 反编译，形成
   "逆向 → 重建 → 再逆向" 的闭环验证。

用法::

    python rebuild_gui_exe.py --run 89956c7e2a4c        # 自动选形态
    python rebuild_gui_exe.py --latest --kind strings_browser
    python rebuild_gui_exe.py --run <id> --list-kinds
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rebuild_kinds import (  # noqa: E402
    KIND_TEMPLATES,
    TEMPLATE_DIR,
    build_context,
    detect_kind,
    load_strings,
    pick_banner,
    pick_title,
    render,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNS_DIR = REPO_ROOT / "src" / "rev_eng" / "runs"
SAMPLES_DIR = REPO_ROOT / "src" / "rev_eng" / "samples"
TCC = REPO_ROOT / ".tools" / "tcc" / "tcc" / "tcc.exe"


# ---------------------------------------------------------------------------
# PE 解析：复用后端共享模块（rev_eng 工具与流水线阶段同一套实现，避免两处漂移）
# ---------------------------------------------------------------------------

_BACKEND = REPO_ROOT / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from harness.modules.reverse_engineering.pe_resources import (  # noqa: E402
    append_payload,
    detect_payload,
    extract_icon,
    read_dialogs,
    read_version_info,
)


def extract_dialog_templates(exe_path: Path) -> list[dict[str, Any]]:
    """解析样本全部 RT_DIALOG 模板（含原始字节，供界面零损失复刻）。"""
    data = exe_path.read_bytes()[:16 * 1024 * 1024]
    dialogs: list[dict[str, Any]] = read_dialogs(data)["dialogs"]
    return dialogs


def extract_main_icon(exe_path: Path, out_ico: Path) -> bool:
    """从样本 PE 提取主图标组写成标准 .ico（无资源段返回 False）。"""
    data = exe_path.read_bytes()[:16 * 1024 * 1024]
    ico = extract_icon(data)
    if not ico:
        return False
    out_ico.write_bytes(ico)
    return True


def _wcstr(value: str) -> str:
    r"""把任意文本转成 ASCII 安全的宽串字面量。

    非 ASCII 字符写成 \uXXXX 转义（C 宽串里即原字符），避免整源"非 ASCII
    一律替换为 ?"把中文等提示文本打成问号。
    """
    out: list[str] = []
    for ch in str(value):
        code = ord(ch)
        if 32 <= code < 127 and ch not in '"\\':
            out.append(ch)
        else:
            out.append(f"\\u{code:04x}")
    return 'L"' + "".join(out) + '"'


def find_sample_path(run: dict[str, object]) -> Path | None:
    """通用样本定位：优先 run 记录的绝对路径，再回落到项目 samples 目录。"""
    for key in ("subject_path", "sample_path", "target", "source", "path"):
        v = run.get(key)
        if isinstance(v, str) and Path(v).is_file():
            return Path(v)
    name = run.get("subject_name")
    if isinstance(name, str) and SAMPLES_DIR.is_dir():
        for hit in SAMPLES_DIR.rglob(name):
            if hit.is_file():
                return hit
    return None


def latest_run_id(runs_dir: Path | None = None) -> str:
    base = runs_dir or RUNS_DIR
    cands = [p for p in base.iterdir() if p.is_dir() and (p / "run.json").is_file()]
    if not cands:
        raise SystemExit(f"没有已完成的 run：{base}")
    return max(cands, key=lambda p: (p / "run.json").stat().st_mtime).name


def main() -> int:
    ap = argparse.ArgumentParser(description="由逆向证据包重建可运行的 GUI exe")
    ap.add_argument("--run", help="run id（默认取最新完成的 run）")
    ap.add_argument("--runs-dir", default=str(RUNS_DIR),
                    help="runs 根目录（默认 <repo>/src/rev_eng/runs；供自定义目录/测试使用）")
    ap.add_argument("--latest", action="store_true", help="使用最新 run")
    ap.add_argument("--kind", choices=sorted(KIND_TEMPLATES),
                    help="强制指定重建形态（默认按样本特征自动选）")
    ap.add_argument("--list-kinds", action="store_true", help="列出可用形态后退出")
    ap.add_argument("--out", help="输出 exe 路径（默认 runs/<run>/rebuild/<sample>_rebuilt.exe）")
    ap.add_argument("--tcc", default=str(TCC), help="tcc.exe 路径")
    args = ap.parse_args()

    if args.list_kinds:
        for k, f in KIND_TEMPLATES.items():
            print(f"{k:16} {TEMPLATE_DIR / f}")
        return 0

    runs_dir = Path(args.runs_dir)
    run_id = args.run or latest_run_id(runs_dir)
    run_dir = runs_dir / run_id
    run_json = run_dir / "run.json"
    if not run_json.is_file():
        raise SystemExit(f"run.json 不存在：{run_json}")
    run = json.loads(run_json.read_text(encoding="utf-8"))

    rea = run.get("stages", {}).get("rea_static", {})
    facts = rea.get("facts", {}) or {}
    strings = load_strings(run_dir, facts)
    imports = [str(i) for i in (facts.get("imports") or [])]

    subject = run.get("subject_name") or "sample.exe"

    rebuild_dir = run_dir / "rebuild"
    rebuild_dir.mkdir(parents=True, exist_ok=True)
    src_path = rebuild_dir / "rebuilt.c"
    out_path = Path(args.out) if args.out else rebuild_dir / f"{Path(subject).stem}_rebuilt.exe"

    # 样本本地事实：对话框模板（原版界面定义）、版本资源（产品自称）、
    # 尾部载荷（自解压安装程序的释放物）。样本不在本地时各自为空，形态判定
    # 退化为仅凭证据包，行为分层降级。
    icon_path = rebuild_dir / "app.ico"
    sample_path = find_sample_path(run)
    sample_data = sample_path.read_bytes()[:64 * 1024 * 1024] if sample_path else b""
    dialogs = extract_dialog_templates(sample_path) if sample_path else []
    payload = detect_payload(sample_data) if sample_data else None
    version = read_version_info(sample_data) if sample_data else {}

    kind, why = (args.kind, "命令行指定") if args.kind else detect_kind(
        facts, strings, imports, dialogs=dialogs, payload=payload)
    title = pick_title(strings, subject, version)
    banner = pick_banner(strings, subject, kind)

    has_icon = False
    if sample_path and extract_main_icon(sample_path, icon_path):
        has_icon = True
        print(f"[rebuild] icon     : {sample_path.name} -> {icon_path.name} "
              f"({icon_path.stat().st_size:,} bytes)")
    else:
        print("[rebuild] icon     : 未提取（样本无资源段或不在本地），使用默认图标")
    if payload:
        print(f"[rebuild] payload  : {payload['type']} @ {payload['offset']:,} "
              f"({payload['size']:,} bytes)")

    context = build_context(run, run_dir, kind=kind, title=title, banner=banner,
                            icon_path=icon_path, has_icon=has_icon,
                            dialogs=dialogs, payload=payload, version=version)
    source, missing = render(kind, context)
    if missing:
        print(f"[rebuild] warn: 模板存在未替换占位符 {missing}", file=sys.stderr)
    if any(ord(ch) > 127 for ch in source):
        source = "".join(ch if ord(ch) < 128 else "?" for ch in source)
        print("[rebuild] warn: 源码含非 ASCII 字符，已替换为 '?'", file=sys.stderr)
    src_path.write_text(source, encoding="ascii")

    tcc = Path(args.tcc)
    if not tcc.is_file():
        raise SystemExit(f"TCC 未就位：{tcc}（应位于项目目录 .tools/tcc/tcc/tcc.exe）")

    cmd = [str(tcc), "-o", str(out_path), str(src_path),
           "-luser32", "-lgdi32", "-lkernel32"]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tcc.parent))  # noqa: S603
    if proc.returncode != 0 or not out_path.is_file():
        print("[rebuild] 编译失败：", file=sys.stderr)
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        return 1

    # 复刻"stub + overlay"自解压结构：把载荷追加到 exe 尾部（只追加不改 PE 头），
    # 并附 24 字节描述块供运行时精确定位。
    appended = False
    if payload:
        p_off = int(payload["offset"])
        p_size = int(payload["size"])
        payload_bytes = sample_data[p_off:p_off + p_size]
        out_path.write_bytes(append_payload(out_path.read_bytes(), payload_bytes))
        appended = True
        (rebuild_dir / "payload-info.json").write_text(json.dumps(payload, indent=2),
                                                       encoding="utf-8")

    print(f"[rebuild] run      : {run_id}")
    print(f"[rebuild] sample   : {subject}")
    print(f"[rebuild] kind     : {kind}   ({why})")
    print(f"[rebuild] title    : {title}")
    print(f"[rebuild] source   : {src_path}  ({src_path.stat().st_size:,} bytes)")
    extra = f"  +payload {int(payload['size']):,}B" if (appended and payload) else ""
    print(f"[rebuild] exe      : {out_path}  ({out_path.stat().st_size:,} bytes){extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
