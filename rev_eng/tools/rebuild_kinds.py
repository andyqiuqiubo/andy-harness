"""重建形态（kind）注册表与自动选择。

`rebuild_gui_exe.py` 只负责「读证据包 → 选形态 → 渲染模板 → 编译」，
具体长什么样由这里的模板决定。新增一种形态 = 放一个 `*.c.tpl` 进
`rebuild_templates/` 并在此登记，不必改主脚本。

三种内置形态
------------
process_manager
    系统监控类样本。命中「进程枚举能力特征」（Toolhelp32 / psapi /
    NtQuerySystemInformation 等）时选用；运行时真枚举进程。
strings_browser
    通用形态。把样本字符串表做成可搜索列表，对任何样本都成立。
facts_browser
    兜底形态。只展示证据包里的结构化事实，零行为假设；样本事实太少
    （字符串不足）时自动降级到这里。

选择依据是**能力特征**而不是产品名，因此对未见过的样本同样有效。
"""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).resolve().parent / "rebuild_templates"
REPO_ROOT = Path(__file__).resolve().parents[2]

# 形态 -> 模板文件
KIND_TEMPLATES: dict[str, str] = {
    "process_manager": "process_manager.c.tpl",
    "installer_wizard": "installer_wizard.c.tpl",
    "strings_browser": "strings_browser.c.tpl",
    "facts_browser": "facts_browser.c.tpl",
}

# 外部解压器（通用契约：7z/zip/cab 等归档都可用它释放）。隔离在项目目录，
# 运行时按"exe 同目录 → 项目 .tools → PATH"顺序查找；缺失则由重建产物降级。
EXTRACTOR_CANDIDATES = (
    REPO_ROOT / ".tools" / "7zip" / "7z.exe",
    REPO_ROOT / ".tools" / "7zip" / "7za.exe",
)

# 进程枚举能力特征：出现在导入模块或字符串里都算命中
_PROCESS_SIGNATURES = (
    "toolhelp32",
    "process32first",
    "process32next",
    "createtoolhelp32snapshot",
    "enumprocesses",
    "enumprocessmodules",
    "getprocessmemoryinfo",
    "ntquerysysteminformation",
    "openprocess",
    "queryfullprocessimagename",
    "psapi",
    "ntdll",
)
# 进程语义词汇（弱信号，只在字符串里统计）
_PROCESS_WORDS = ("process", "thread", "handle", "working set", "commit charge", "pid")

# PE/编译器样板串：任何样本都有，不能当产品名
_BOILERPLATE = (
    "this program cannot be run in dos mode",
    "rich",
    "text segment",
    "data segment",
    ".rdata",
    ".pdata",
    "msvcp",
    "vcruntime",
    "api-ms-win",
    "windowscodecs",
)

# 运行时/错误信息：任何程序都有，不能当产品名
_RUNTIME_NOISE = (
    "error", "exception", "fail", "invalid", "not found", "cannot",
    "unknown", "warning", "access denied", "assert", "denied",
)


def _cstr(value: str) -> str:
    """把任意字符串转成安全的 C 字符串字面量（纯 ASCII）。"""
    safe = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in str(value))
    safe = safe.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{safe}"'


def _ascii_list(values: list[str], limit: int, maxlen: int = 120) -> str:
    picked: list[str] = []
    for v in values:
        s = str(v)[:maxlen]
        if s and all(32 <= ord(c) < 127 for c in s):
            picked.append(s)
        if len(picked) >= limit:
            break
    return ", ".join(_cstr(v) for v in picked)


def _is_ascii(s: str) -> bool:
    return bool(s) and all(32 <= ord(c) < 127 for c in s)


def _as_dict(value: object) -> dict[str, object]:
    """把 JSON 解析结果安全收敛成 dict（run.json 结构不可全信）。"""
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _as_int(value: object, default: int = 0) -> int:
    try:
        result = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
    return result if isinstance(result, int) else default


def _is_subseq(needle: str, hay: str) -> bool:
    """needle 是否为 hay 的字符子序列（忽略大小写）。"""
    it = iter(hay.lower())
    return all(c in it for c in needle.lower())


def _stem(subject: str) -> str:
    """样本名主干（去扩展名与非字母数字），用于和候选标题做缩写匹配。

    保留数字：安装程序常把版本号并进文件名（如 setup26.exe），其主干
    setup26 往往是 "Setup 26" 一类标题的字符子序列。"""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", str(subject))
    return re.sub(r"[^0-9a-z]", "", stem.lower())


def _score_title(s: str, freq: int, subject: str = "") -> int:
    """给候选标题打分：越像产品名分越高（与具体产品无关）。"""
    if not _is_ascii(s) or not (3 <= len(s) <= 64):
        return -100
    low = s.lower()
    if any(b in low for b in _BOILERPLATE):
        return -100
    if any(n in low for n in _RUNTIME_NOISE):
        return -100
    if s[0] in "!$#@.-":
        return -100
    if s.rstrip().endswith(":"):  # 消息前缀（"Unsupported command:"）不是产品名
        return -100
    if re.search(r"[/\\]|%[sduxfg%]|https?://|\{\d\}|[<>{}]", s):
        return -100
    if "=" in s:  # KEY=VALUE 是常量不是产品名
        return -100
    if "::" in s or s.startswith("_") or "__" in s:  # 符号名 / 命名空间
        return -100
    if re.search(r"\.(pdb|lib|obj|cpp|c|h|res|manifest|xml)$", low):
        return -100
    # 全大写 + 连字符/下划线 = 宏或标识符，不是产品名
    if not re.search(r"[a-z]", s) and re.search(r"[-_]", s):
        return -100
    # 无空格且内部多处大写 = API / 符号名（CompareStringW 之类）
    if " " not in s and len(re.findall(r"[A-Z]", s[1:])) >= 2:
        return -100
    letters = sum(c.isalpha() or c == " " for c in s)
    if letters / len(s) < 0.75:
        return -100
    if s.isupper() or s.isdigit():
        return -100
    if re.fullmatch(r"[0-9A-Fa-f]{8,}", s):
        return -100

    score = 0
    if s[0].isupper():
        score += 3
    words = s.split()
    if 1 <= len(words) <= 4:
        score += 3
    if " " in s:
        score += 2
    if 5 <= len(s) <= 40:
        score += 2
    if freq > 1:
        score += 2
    if re.search(r"\d+\.\d+", s):  # 带版本号通常就是产品自称
        score += 2
    if s.endswith((".exe", ".dll", ".sys")):
        score -= 2
    stem = _stem(subject)
    if len(stem) >= 3 and _is_subseq(stem, re.sub(r"[^A-Za-z]", "", s)):
        score += 6  # 与样本名互为缩写，最强信号
    return score


def pick_title(strings: list[str], subject: str,
               version: dict[str, str] | None = None) -> str:
    """推断产品名：优先 PE 版本资源（权威），否则按「像不像产品名」打分。

    version 为样本 RT_VERSION 资源的 StringFileInfo 键值（FileDescription 等），
    任何带版本信息的 PE 都有；缺失或不可用时才走字符串打分，且要求分数达到阈值，
    达不到就诚实回退样本名而不是硬猜。
    """
    for key in ("FileDescription", "ProductName", "OriginalFilename", "ProductVersion"):
        v = str((version or {}).get(key) or "").strip()
        if v and 3 <= len(v) <= 80 and _is_ascii(v):
            return f"{v}  [REBUILT]"

    freq: dict[str, int] = {}
    for s in strings:
        freq[s] = freq.get(s, 0) + 1
    best, best_score = "", 0
    for s in strings:
        sc = _score_title(s, freq[s], subject)
        if sc > best_score:
            best, best_score = s, sc
    # 阈值 10：至少要「首字母大写 + 正常词数 + 长度合适」三项同时成立，
    # 否则说明样本里没有像样的产品自称，诚实回退到样本名而不是瞎猜。
    if best_score >= 10:
        return f"{best}  [REBUILT]"
    return f"{subject} (rebuilt)"


def _artifact_values(path: Path) -> list[str]:
    """REA v6.4 返回 evidence 结构，真实值在 normalized_result[*].value。"""
    if not path.is_file():
        return []
    try:
        import json  # noqa: PLC0415

        data = json.loads(path.read_text(encoding="utf-8"))
        out: list[str] = []

        def walk(o: object) -> None:
            if isinstance(o, dict):
                v = o.get("value")
                if isinstance(v, str) and v:
                    out.append(v)
                for x in o.values():
                    walk(x)
            elif isinstance(o, list):
                for x in o:
                    walk(x)

        walk(data)
        return out
    except Exception:  # noqa: BLE001
        return []


def _artifact_first(run_dir: Path, pattern: str) -> list[str]:
    """按通配取第一个命中的证据产物（原生与降级路径产物名不同）。"""
    for cand in sorted((run_dir / "artifacts").glob(pattern)):
        out = _artifact_values(cand)
        if out:
            return out
    return []


def load_strings(run_dir: Path, facts: dict[str, object]) -> list[str]:
    """优先取 artifacts 里的全量字符串（facts 里的常被截断到 200 条）。

    产物名随路径而异：原生分析写 `rea-strings.json`，
    引擎不可用时的降级路径写 `rea-strings-fallback.json`。"""
    out = _artifact_first(run_dir, "rea-strings*.json")
    return out or [str(s) for s in _as_list(facts.get("strings"))]


def load_functions(run_dir: Path, facts: dict[str, object]) -> list[str]:
    """同理：优先取 artifacts 里的全量函数清单。"""
    out = _artifact_first(run_dir, "rea-procedures*.json")
    return out or [str(f) for f in _as_list(facts.get("functions"))]


def pick_banner(strings: list[str], subject: str, kind: str) -> str:
    """横幅 = 推断出的产品名 + 样本内的官方域名，两者都取自样本自身。"""
    base = pick_title(strings, subject).removesuffix("[REBUILT]").strip()
    domain = ""
    for s in strings:
        m = re.search(r"www\.[A-Za-z0-9.\-]{4,60}", s)
        if m:
            domain = m.group(0)
            break
    tail = f"[REBUILT by andy-harness - {kind}]"
    if base and domain:
        return f"{base} - {domain}   {tail}"
    if domain:
        return f"{domain}   {tail}"
    if base:
        return f"{base}   {tail}"
    return tail


def detect_kind(facts: dict[str, object], strings: list[str],
                imports: list[str], *, dialogs: list[dict[str, Any]] | None = None,
                payload: dict[str, Any] | None = None) -> tuple[str, str]:
    """按能力特征选择形态，返回 (kind, 选择理由)。

    优先级：process_manager（进程枚举，运行时强信号）→ installer_wizard
    （对话框模板 + 安装器能力特征）→ strings_browser → facts_browser。
    安装器要求**结构证据（RT_DIALOG）+ 能力证据**双条件，避免把带对话框的
    普通工具误判成安装器。
    """
    imp_blob = " ".join(str(i) for i in imports).lower()
    str_blob = " ".join(strings[:4000]).lower()

    hits = [sig for sig in _PROCESS_SIGNATURES if sig in imp_blob or sig in str_blob]
    words = [w for w in _PROCESS_WORDS if w in str_blob]
    if len(hits) >= 2 and len(words) >= 2:
        return "process_manager", (
            f"命中进程枚举特征 {len(hits)} 项（{', '.join(hits[:3])}）+ 语义词 "
            f"{len(words)} 项"
        )

    dialogs = dialogs or []
    if dialogs:
        api_hits = [s for s in _INSTALLER_API_SIGNATURES
                    if s in imp_blob or s in str_blob]
        sem_hits = [w for w in _INSTALLER_WORDS if w in str_blob]
        reasons: list[str] = []
        if len(api_hits) >= 2:
            reasons.append(f"安装器 API 特征 {len(api_hits)} 项（{', '.join(api_hits[:3])}）")
        elif len(api_hits) >= 1 and sem_hits:
            reasons.append(f"API 特征 {api_hits[0]} + 安装语义词 {sem_hits[0]!r}")
        if payload:
            reasons.append(f"尾部含 {payload.get('type')} 载荷")
        if reasons:
            return "installer_wizard", (
                f"样本含 {len(dialogs)} 个对话框模板；" + "；".join(reasons)
            )

    usable = [s for s in strings if _is_ascii(s) and 2 <= len(s) <= 200]
    if len(usable) >= 40:
        return "strings_browser", f"无进程/安装器特征；可用字符串 {len(usable)} 条，够做字符串浏览"
    return "facts_browser", f"字符串仅 {len(usable)} 条，降级为纯事实展示"


def load_dialogs(run_dir: Path) -> list[dict[str, Any]]:
    """从证据包读对话框模板（原生与降级路径的 artifact 名不同，用通配兼容）。"""
    for cand in sorted((run_dir / "artifacts").glob("rea-dialogs*.json")):
        try:
            import json  # noqa: PLC0415

            data = json.loads(cand.read_text(encoding="utf-8"))
            found = _as_list(_as_dict(data).get("dialogs"))
            if found:
                return [d for d in found if isinstance(d, dict)]
        except Exception:  # noqa: BLE001
            continue
    return []


# 安装器能力特征（与 backend pe_resources 保持一致：能力词，非产品名）
_INSTALLER_API_SIGNATURES = (
    "shbrowseforfolder", "shgetfolderpath", "shgetspecialfolderpath",
    "createdialogparam", "msctls_progress32", "adjusttokenprivileges",
)
_INSTALLER_WORDS = (
    "destination folder", "select the folder", "install folder",
    "&install", "setup wizard", "uninstall",
)


def _load_pseudo(run_dir: Path, limit: int = 1400) -> str:
    """从证据包取一段真实 Ghidra 伪代码正文（取不到给占位）。"""
    cand = run_dir / "artifacts" / "rea-pseudo-code.json"
    if not cand.is_file():
        return "(no pseudo code in this run)"
    try:
        import json  # noqa: PLC0415

        data = json.loads(cand.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key, val in data.items():
                text = val if isinstance(val, str) else str(val)
                text = re.sub(r"\\n", "\n", text)
                text = re.sub(r"\s+\n", "\n", text).strip()
                if len(text) > 40:
                    return f"/* {key} */\n{text[:limit]}"
    except Exception:  # noqa: BLE001
        pass
    return "(no pseudo code in this run)"


def build_context(
    run: dict[str, object],
    run_dir: Path,
    *,
    kind: str,
    title: str,
    banner: str,
    icon_path: Path,
    has_icon: bool,
    max_strings: int = 400,
    dialogs: list[dict[str, Any]] | None = None,
    payload: dict[str, Any] | None = None,
    version: dict[str, Any] | None = None,
) -> dict[str, object]:
    """组装模板可用的全部变量（模板按需取用，多余的忽略）。"""
    rea = _as_dict(_as_dict(run.get("stages")).get("rea_static"))
    facts = _as_dict(rea.get("facts"))
    strings = load_strings(run_dir, facts)
    functions = load_functions(run_dir, facts)
    imports = [str(i) for i in _as_list(facts.get("imports"))]
    decompiled = [str(d) for d in _as_list(facts.get("decompiled"))]
    version = _as_dict(version)

    subject = str(run.get("subject_name") or "sample.exe")
    sha256 = (str(run.get("subject_sha256") or "-"))[:64]
    fn_count = _as_int(facts.get("function_count")) or len(functions) or 0
    str_count = _as_int(facts.get("string_count")) or len(strings) or 0

    picked: list[str] = []
    for s in strings:
        if _is_ascii(s) and 2 <= len(s) <= 120:
            picked.append(s)
        if len(picked) >= max_strings:
            break

    rows = [
        ("Sample", subject),
        ("SHA-256", sha256),
        ("Engine", str(rea.get("engine") or "rea")),
        ("Stage status", str(rea.get("status") or "-")),
        ("Run id", str(run.get("run_id") or run_dir.name)),
        ("Template", kind),
        ("Functions", f"{fn_count:,}"),
        ("Strings", f"{str_count:,}"),
        ("Decompiled", ", ".join(decompiled[:8]) or "(none)"),
        ("Imported modules", " ".join(imports[:40]) or "(none)"),
    ]
    pseudo = _load_pseudo(run_dir)
    rows.append(("Pseudo code", pseudo[:600].replace("\n", " | ")))

    installer = _installer_context(dialogs or [], strings, version, payload)
    installer.update({
        "run_id": str(run.get("run_id") or run_dir.name),
        "subject_name": subject,
        "title": _cstr(title),
        "banner": _cstr(banner),
        "sample": _cstr(subject),
        "sha256": _cstr(sha256),
        "engine": _cstr(str(rea.get("engine") or "rea")),
        "fn_count": fn_count,
        "str_count": str_count,
        "dec_count": max(1, len(decompiled)),
        "decompiled": _ascii_list(decompiled, 12) or '"(none)"',
        "imports": _ascii_list(imports, 16) or '"(none)"',
        "strings": _ascii_list(picked, max_strings) or '"(none)"',
        "n_strings": len(picked),
        "facts_rows": ", ".join(f'{{{_cstr(k)}, {_cstr(v)}}}' for k, v in rows),
        "n_facts": len(rows),
        "pseudo": _cstr(pseudo),
        "icon_path": _cstr(str(icon_path)),
        "has_icon": "1" if has_icon else "0",
    })
    return installer


# 控件角色语义（通用规则：按控件类 + 文本语义推断，推断不到就给 0 禁用该功能）
_FOLDER_HINTS = ("destination", "install folder", "target folder", "folder")
_INSTALL_HINTS = ("install", "next", "ok", "setup", "extract")
_CANCEL_HINTS = ("cancel", "close", "quit", "exit")
_BS_DEFPUSHBUTTON = 0x0001


def _installer_context(dialogs: list[dict[str, Any]], strings: list[str],
                       version: dict[str, Any],
                       payload: dict[str, Any] | None) -> dict[str, object]:
    """从对话框模板与样本串推导 installer_wizard 模板变量。

    取第一个"含可推断角色"的对话框；角色按控件类 + 文本语义推断，不猜产品。
    推断不到的角色 id 记 0，模板据此禁用对应功能而非编造。
    """
    chosen: dict[str, Any] | None = None
    for dlg in dialogs:
        if dlg.get("unsupported"):
            continue
        controls = dlg.get("controls") or []
        if not isinstance(controls, list) or not controls:
            continue
        chosen = dlg
        break

    ctx: dict[str, object] = {
        "dlg_template_hex": "", "dlg_template_len": 0,
        "dlg_title": _cstr("Setup"), "dlg_font_point": 8, "dlg_font_face": _cstr("MS Shell Dlg"),
        "id_label": 0, "id_dest_edit": 0, "id_browse": 0, "id_progress": 0,
        "id_install": 0, "id_cancel": 0,
        "label_dest": _cstr("Destination folder:"),
        "label_browse_caption": _cstr("Select destination folder:"),
        "label_install": _cstr("&Install"),
        "label_cancel": _cstr("Cancel"),
        "msg_cancel_confirm": _cstr("Cancel the installation?"),
        "msg_done": _cstr("Installation completed."),
        "default_dest": _cstr(""),
        "has_payload": "1" if payload else "0",
        "payload_type": _cstr(str((payload or {}).get("type") or "")),
        "payload_size": int((payload or {}).get("size") or 0),
        "extractor_path": _cstr(_find_extractor()),
    }
    if chosen is None:
        # 样本没有可解析对话框：用通用兜底模板（安装器常识布局，不含样本信息）
        fallback = _fallback_dialog_template()
        ctx["dlg_template_hex"] = _hex_array(fallback)
        ctx["dlg_template_len"] = len(bytes.fromhex(fallback))
        return ctx

    ctx["dlg_template_hex"] = _hex_array(str(chosen.get("raw") or ""))
    ctx["dlg_template_len"] = int(chosen.get("resource_bytes") or 0)
    if chosen.get("title"):
        ctx["dlg_title"] = _cstr(str(chosen["title"]))
    font = chosen.get("font") or {}
    if isinstance(font, dict):
        if font.get("point"):
            ctx["dlg_font_point"] = int(font["point"])
        if font.get("face"):
            ctx["dlg_font_face"] = _cstr(str(font["face"]))

    controls = [c for c in chosen.get("controls") or [] if isinstance(c, dict)]
    label_text = ""
    dest_edit_id = 0
    browse_id = 0
    progress_id = 0
    install_id = 0
    cancel_id = 0
    install_label = ""
    cancel_label = ""
    for c in controls:
        cname = str(c.get("class_name") or "")
        text = str(c.get("text") or "")
        low = text.lower()
        cid = int(c.get("id") or 0)
        if cname == "msctls_progress32":
            progress_id = cid or progress_id
        elif cname == "edit" and not dest_edit_id:
            dest_edit_id = cid
        elif cname == "static" and text and not label_text and not dest_edit_id:
            if any(h in low for h in _FOLDER_HINTS):
                label_text = text
        elif cname == "button":
            if text.strip() in {"...", "..", "…"} or (0 < len(text.strip()) <= 2 and not browse_id):
                browse_id = cid or browse_id
            elif int(c.get("style") or 0) & _BS_DEFPUSHBUTTON and not install_id:
                install_id, install_label = cid, text
            elif any(h in low for h in _CANCEL_HINTS) and not cancel_id:
                cancel_id, cancel_label = cid, text
            elif any(h in low for h in _INSTALL_HINTS) and not install_id:
                install_id, install_label = cid, text
    if label_text:
        ctx["label_dest"] = _cstr(label_text)
    if install_label:
        ctx["label_install"] = _cstr(install_label)
    if cancel_label:
        ctx["label_cancel"] = _cstr(cancel_label)
    ctx.update({
        "id_dest_edit": dest_edit_id, "id_browse": browse_id,
        "id_progress": progress_id, "id_install": install_id,
        "id_cancel": cancel_id,
        "id_label": 0,
    })

    # 提示语优先用样本自身字符串（语义匹配），取不到用中性默认
    for s in strings:
        low = s.lower()
        if "cancel" in low and ("install" in low or "setup" in low) and "?" in s:
            ctx["msg_cancel_confirm"] = _cstr(s[:120])
            break
    for s in strings:
        low = s.lower()
        if ("is installed" in low or "installation complete" in low
                or "successfully installed" in low or "installation finished" in low):
            ctx["msg_done"] = _cstr(s[:120])
            break
    for s in strings:
        if "select the folder" in s.lower() or "browse" in s.lower():
            ctx["label_browse_caption"] = _cstr(s[:120])
            break

    # 默认目标目录：版本资源给了产品名才推导，否则留空
    product = str(version.get("ProductName") or version.get("InternalName") or "").strip()
    if product:
        ctx["default_dest"] = _cstr(f"C:\\Program Files\\{product}")
    return ctx


def _find_extractor() -> str:
    """定位外部解压器（项目 .tools 优先）；找不到返回空串（模板走降级）。"""
    for cand in EXTRACTOR_CANDIDATES:
        if cand.is_file():
            return str(cand)
    return ""


def _hex_array(hex_text: str) -> str:
    """把 hex 字符串转成 C 字节数组字面量（每行 16 个，控制单行长度）。"""
    if not hex_text:
        return ""
    raw = bytes.fromhex(hex_text)
    lines = []
    for i in range(0, len(raw), 16):
        chunk = ", ".join(f"0x{b:02x}" for b in raw[i:i + 16])
        lines.append("    " + chunk + ("," if i + 16 < len(raw) else ""))
    return "\n".join(lines)


def _w(s: str) -> bytes:
    """ASCII 串 → UTF-16LE + NUL。"""
    return s.encode("utf-16le") + b"\x00\x00"


def _sz_or_ord(value: str) -> bytes:
    return _w(value)


def _ord_class(ord_id: int) -> bytes:
    return struct.pack("<HH", 0xFFFF, ord_id)


def _fallback_dialog_template() -> str:
    """样本没有可解析对话框时，生成一个**通用**最小安装对话框模板。

    通用兜底而非特例：控件类/几何/文本都是安装器的常识布局，不含任何样本信息。
    """
    ws_child = 0x50000000
    ws_visible = 0x10000000
    style = (0x80800000 | 0x00C00000 | 0x00080000 | 0x80 | 0x40)  # POPUP|CAPTION|SYSMENU|MODALFRAME|SETFONT
    out = struct.pack("<IIHHHHH", style, 0, 6, 0, 0, 200, 112)
    out += struct.pack("<H", 0)          # menu: 空
    out += struct.pack("<H", 0)          # class: 空
    out += _sz_or_ord("Setup")           # title
    out += struct.pack("<H", 8)          # point size
    out += _sz_or_ord("MS Shell Dlg")    # typeface

    def item(cid: int, cls: bytes, text: str, x: int, y: int, cx: int, cy: int,
             istyle: int) -> bytes:
        body = struct.pack("<IIHHHHH", istyle, 0, x, y, cx, cy, cid)
        body += cls
        body += _sz_or_ord(text)
        body += struct.pack("<H", 0)     # cbCreationData
        pad = (-len(body)) % 4
        return body + b"\x00" * pad

    out += item(110, _ord_class(0x82), "Destination folder:", 8, 8, 184, 8, ws_child | ws_visible)
    out += item(111, _ord_class(0x81), "", 8, 21, 152, 14,
                ws_child | ws_visible | 0x00800000 | 0x00080000)  # BORDER|AUTOHSCROLL
    out += item(112, _ord_class(0x80), "...", 172, 20, 20, 16, ws_child | ws_visible)
    out += item(114, _sz_or_ord("msctls_progress32"), "", 8, 64, 184, 10, ws_child | ws_visible)
    out += item(1, _ord_class(0x80), "&Install", 56, 88, 64, 16,
                ws_child | ws_visible | 0x0001)  # DEFPUSHBUTTON
    out += item(2, _ord_class(0x80), "Cancel", 128, 88, 64, 16, ws_child | ws_visible)
    return out.hex()


def render(kind: str, context: dict[str, object]) -> tuple[str, list[str]]:
    """渲染模板，返回 (源码, 未替换的占位符列表)。"""
    import string  # noqa: PLC0415

    tpl_file = TEMPLATE_DIR / KIND_TEMPLATES[kind]
    raw = tpl_file.read_text(encoding="utf-8")
    template = string.Template(raw)
    # 只在模板原文里找占位符：渲染后的源码含样本字符串，其中的 $xxx 是数据不是占位符
    wanted = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", raw))
    missing = sorted(wanted - set(context))
    return template.safe_substitute(context), missing
