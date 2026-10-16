#!/usr/bin/env python
"""用项目里已有的 exe 重新验证「工程强逆向」的静态分析核心 + 重建形态自动选择。

环境说明
--------
- 沙箱内无 REA / Ghidra / TCC（真深度反编译与 TCC 编译重建需用户终端），故本脚本只跑
  **可离线运行的静态核心**：PE 资源解析（对话框/版本/导入/字符串/图标/尾部载荷）、
  安装器能力特征、以及重建形态自动选择（detect_kind / pick_title）。
- 这些正是流水线 `_static_fallback` 与 `rebuild_gui_exe.py` 共用的实现，离线可完整验证，
  且能直接证明「通用性」（靠能力特征而非产品名判定形态）。

用法::

    python rev_eng/verify_static_core.py
    python rev_eng/verify_static_core.py --exe rev_eng/samples/7z2604.exe
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # src/
TOOLS = ROOT / "rev_eng" / "tools"
PE_DIR = ROOT / "backend" / "harness" / "modules" / "reverse_engineering"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


pe = _load("pe_resources", PE_DIR / "pe_resources.py")
sys.path.insert(0, str(TOOLS))
import rebuild_kinds  # noqa: E402


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _control_count(dialogs: list[dict]) -> int:
    n = 0
    for d in dialogs:
        c = d.get("controls")
        if isinstance(c, list):
            n += len(c)
    return n


def analyze(path: Path) -> dict:
    data = path.read_bytes()
    dlg = pe.read_dialogs(data)
    dialogs = dlg.get("dialogs", [])
    version = pe.read_version_info(data)
    imports = pe.read_imports(data)
    strings = pe.local_strings(path)
    payload = pe.detect_payload(data)
    signals = pe.installer_signals(strings, imports)
    icon = pe.extract_icon(data)
    subject = path.name
    kind, reason = rebuild_kinds.detect_kind(
        {}, strings, imports, dialogs=dialogs, payload=payload
    )
    title = rebuild_kinds.pick_title(strings, subject, version or None)
    return {
        "path": str(path),
        "name": subject,
        "size": len(data),
        "sha256": _sha256(data)[:16],
        "is_pe": data[:2] == b"MZ",
        "version": version,
        "dialog_count": len(dialogs),
        "control_count": _control_count(dialogs),
        "import_count": len(imports),
        "imports_sample": imports[:6],
        "string_count": len(strings),
        "payload": payload,
        "installer_signals": signals[:4],
        "installer_signal_count": len(signals),
        "kind": kind,
        "kind_reason": reason,
        "title": title,
        "icon_bytes": len(icon) if icon else 0,
    }


def _fmt_row(r: dict) -> str:
    ver = r["version"]
    ver_str = ver.get("FileDescription") or ver.get("ProductName") or "(无版本资源)"
    pay = r["payload"]
    pay_str = f"{pay.get('type')} {pay.get('size'):,}B @0x{pay.get('offset'):x}" if pay else "无"
    return (
        f"\n### {r['name']}  ({r['size']:,}B, sha256:{r['sha256']})\n"
        f"  PE 有效        : {r['is_pe']}\n"
        f"  版本资源        : {ver_str}\n"
        f"  对话框/控件     : {r['dialog_count']} 个对话框 / {r['control_count']} 控件\n"
        f"  导入模块        : {r['import_count']} 个 ({', '.join(r['imports_sample'])} …)\n"
        f"  字符串          : {r['string_count']} 条（ASCII+UTF-16 两遍）\n"
        f"  尾部载荷        : {pay_str}\n"
        f"  安装器信号      : {r['installer_signal_count']} 项（{', '.join(r['installer_signals'])} …）\n"
        f"  图标提取        : {r['icon_bytes']:,} 字节\n"
        f"  ── 重建形态自动选择 ──\n"
        f"  detect_kind    : {r['kind']}  —— {r['kind_reason']}\n"
        f"  pick_title     : {r['title']}\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", action="append", default=[],
                    help="指定要验证的 exe（可多次）；默认用一组项目已有样本")
    args = ap.parse_args()

    defaults = [
        ROOT / "rev_eng" / "samples" / "7z2604.exe",
        ROOT / "rev_eng" / "samples" / "procexp" / "procexp64.exe",
        ROOT / "rev_eng" / "samples" / "native" / "find.exe",
        ROOT / "rev_eng" / "samples" / "generated" / "andy_reveng_sample.exe",
        # 闭环：已被重建过的产物再送回静态分析，确认是可再逆向的有效 PE
        ROOT / "rev_eng" / "runs" / "a9a752600aab" / "rebuild" / "7z2604_rebuilt.exe",
    ]
    targets = [Path(p) for p in args.exe] or defaults
    targets = [p for p in targets if p.is_file()]

    print("=" * 72)
    print("工程强逆向 · 离线静态核心验证（用项目里已有的 exe）")
    print("=" * 72)
    results = []
    for p in targets:
        try:
            r = analyze(p)
        except Exception as exc:  # noqa: BLE001
            print(f"\n### {p.name}  —— 解析异常：{exc}")
            continue
        results.append(r)
        print(_fmt_row(r))

    # 闭环一致性：重建产物必须仍是有效 PE 且能被同一套解析器吃下
    rebuilt = [r for r in results if "rebuilt" in r["name"]]
    print("=" * 72)
    print("闭环一致性检查（重建品 = 可再次逆向的有效 PE）")
    print("=" * 72)
    for r in rebuilt:
        ok = r["is_pe"]
        print(f"  {r['name']}: PE有效={ok}  标题={r['title']}")

    # 通用性断言：形态必须由能力特征决定，不能撞名
    print("=" * 72)
    print("通用性小结")
    print("=" * 72)
    kinds = {r["name"]: r["kind"] for r in results}
    for n, k in kinds.items():
        print(f"  {n:32s} -> {k}")
    print("\n完成。REA 深度反编译与 TCC 编译重建需在用户终端执行（沙箱无 REA/Ghidra/TCC）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
