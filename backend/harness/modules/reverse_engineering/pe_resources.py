"""PE 资源与载荷的通用解析（工程强逆向：backend 阶段与重建工具共用）。

定位
----
这里只放**与任何具体样本无关**的 PE 结构解析原语：资源目录遍历、对话框
模板、版本信息、图标、导入表、可打印串、尾部载荷探测。逆向流水线的降级
阶段（``stages.py``）与"证据包 → 重建 exe"工具（``rev_eng/tools/rebuild_gui_exe.py``）
都用同一套实现，避免两处逻辑漂移。

设计约束
--------
- 纯标准库，无第三方依赖（backend 与 CLI 工具都能直接导入）；
- 全部函数吃 ``bytes``、吐可 JSON 序列化的结构，不碰文件系统；
- 结构不支持时**明确报备**（如 DLGTEMPLATEEX），不猜测、不静默丢数据。
"""

from __future__ import annotations

import re
import struct
import zlib
from typing import Any

# 资源类型 id（Win32 定义）
RT_ICON = 3
RT_DIALOG = 5
RT_STRING = 6
RT_GROUP_ICON = 14
RT_VERSION = 16

# 对话框序数类 → 可读类名
DIALOG_CLASS_ORDS = {
    0x80: "button",
    0x81: "edit",
    0x82: "static",
    0x83: "listbox",
    0x84: "scrollbar",
    0x85: "combobox",
}

# 已知尾部归档签名：(名称, 签名字节, 说明)
ARCHIVE_SIGNATURES: tuple[tuple[str, bytes], ...] = (
    ("7z", b"7z\xbc\xaf\x27\x1c"),
    ("zip", b"PK\x03\x04"),
    ("cab", b"MSCF"),
    ("gzip", b"\x1f\x8b"),
    ("rar", b"Rar!\x1a\x07"),
)

TRAILER_MAGIC = b"AHRPLD1\x00"  # 重建 exe 尾部载荷描述块魔数
TRAILER_SIZE = 24  # offset(8) + size(8) + magic(8)


# ---------------------------------------------------------------------------
# PE 头 / 节表 / 资源目录
# ---------------------------------------------------------------------------


def _pe_layout(data: bytes) -> dict[str, Any] | None:
    """解析 PE 头，返回节表与关键 RVA（失败返回 None）。"""
    if len(data) < 0x200 or data[:2] != b"MZ":
        return None
    try:
        pe_off = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe_off : pe_off + 4] != b"PE\x00\x00":
            return None
        coff = pe_off + 4
        num_sec = struct.unpack_from("<H", data, coff + 2)[0]
        opt_size = struct.unpack_from("<H", data, coff + 16)[0]
        opt = coff + 20
        magic = struct.unpack_from("<H", data, opt)[0]
        dd_off = opt + (112 if magic == 0x20B else 96)
        res_rva, res_size = struct.unpack_from("<II", data, dd_off + 8 * 2)
        imp_rva, _imp_size = struct.unpack_from("<II", data, dd_off + 8 * 1)
        sec_tab = opt + opt_size
        # (虚拟地址, 虚拟大小, 原始大小, 原始偏移)：虚拟大小用于 RVA→偏移映射，
        # 原始大小用于计算 overlay 起点，两者不可混用（.data 常是 vsize >> rsize）。
        sections: list[tuple[int, int, int, int]] = []
        for i in range(num_sec):
            s = sec_tab + 40 * i
            if s + 40 > len(data):
                break
            vsize, vaddr, rsize, rptr = struct.unpack_from("<IIII", data, s + 8)
            sections.append((vaddr, vsize, rsize, rptr))
        return {
            "res_rva": res_rva,
            "res_size": res_size,
            "imp_rva": imp_rva,
            "sections": sections,
            "num_sec": num_sec,
        }
    except (struct.error, IndexError):
        return None


def _rva2off(layout: dict[str, Any], rva: int) -> int | None:
    for vaddr, vsize, rsize, rptr in layout["sections"]:
        if vaddr <= rva < vaddr + max(vsize, rsize):
            return int(rva - vaddr + rptr)
    return None


def _dir_entries(data: bytes, off: int) -> list[tuple[int, int]]:
    """资源目录的条目数组：命名条目在前、ID 条目在后（总数是两者之和）。"""
    n_named, n_id = struct.unpack_from("<HH", data, off + 12)
    out: list[tuple[int, int]] = []
    for i in range(n_named + n_id):
        e = off + 16 + 8 * i
        if e + 8 > len(data):
            break
        name_id, sub = struct.unpack_from("<II", data, e)
        out.append((name_id, sub))
    return out


def _resource_name(data: bytes, layout: dict[str, Any], name_id: int) -> str | None:
    """命名条目（高位 bit 置位）的名字串；ID 条目返回 None。"""
    if not (name_id & 0x80000000):
        return None
    off = _rva2off(layout, layout["res_rva"] + (name_id & 0x7FFFFFFF))
    if off is None or off + 2 > len(data):
        return None
    length = struct.unpack_from("<H", data, off)[0]
    end = off + 2 + length * 2
    if end > len(data):
        return None
    return data[off + 2 : end].decode("utf-16le", "replace")


def iter_resource_leaves(data: bytes, type_id: int) -> list[dict[str, Any]]:
    """遍历 ``.rsrc``，返回指定资源类型下的**全部**叶子。

    三级目录完整遍历（level1 类型 → level2 名字 → level3 语言），不取"第一个"；
    命名条目同时解析名字串。每项含 name_id/name/lang_id/offset/size。
    """
    out: list[dict[str, Any]] = []
    layout = _pe_layout(data)
    if layout is None or not layout["res_rva"]:
        return out
    base = _rva2off(layout, layout["res_rva"])
    if base is None:
        return out
    try:
        for t_id, t_sub in _dir_entries(data, base):
            if t_id & 0x80000000:
                continue  # 命名类型条目：与数字类型 id 无关，不能混入
            if (t_id & 0xFFFF) != type_id:
                continue
            if not (t_sub & 0x80000000):
                continue
            l2 = _rva2off(layout, layout["res_rva"] + (t_sub & 0x7FFFFFFF))
            if l2 is None:
                continue
            for name_id, n_sub in _dir_entries(data, l2):
                if not (n_sub & 0x80000000):
                    continue
                l3 = _rva2off(layout, layout["res_rva"] + (n_sub & 0x7FFFFFFF))
                if l3 is None:
                    continue
                for lang_id, d_sub in _dir_entries(data, l3):
                    if d_sub & 0x80000000:
                        continue
                    leaf = _rva2off(layout, layout["res_rva"] + d_sub)
                    if leaf is None:
                        continue
                    d_rva, d_size = struct.unpack_from("<II", data, leaf)
                    d_off = _rva2off(layout, d_rva)
                    if d_off is None:
                        continue
                    out.append(
                        {
                            "name_id": name_id & 0xFFFF,
                            "name": _resource_name(data, layout, name_id),
                            "lang_id": lang_id & 0xFFFF,
                            "offset": d_off,
                            "size": d_size,
                        }
                    )
    except (struct.error, IndexError):
        return out
    return out


def first_resource(data: bytes, type_id: int) -> tuple[int, int] | None:
    """取某资源类型的第一个数据块（offset, size）；没有返回 None。"""
    leaves = iter_resource_leaves(data, type_id)
    if not leaves:
        return None
    return leaves[0]["offset"], leaves[0]["size"]


# ---------------------------------------------------------------------------
# 对话框模板（RT_DIALOG）
# ---------------------------------------------------------------------------


def _read_sz_or_ord(blob: bytes, pos: int) -> tuple[str, Any, int]:
    """读 sz_Or_Ord 字段：返回 (kind, value, new_pos)，kind ∈ empty/ord/str。"""
    if pos + 2 > len(blob):
        return "empty", None, pos
    first = struct.unpack_from("<H", blob, pos)[0]
    if first == 0xFFFF:  # 序数形式
        if pos + 4 > len(blob):
            return "empty", None, pos
        return "ord", struct.unpack_from("<H", blob, pos + 2)[0], pos + 4
    if first == 0:
        return "empty", None, pos + 2
    end = pos
    while end + 1 < len(blob):
        if blob[end] == 0 and blob[end + 1] == 0:
            break
        end += 2
    return "str", blob[pos:end].decode("utf-16le", "replace"), end + 2


def parse_dialog_template(blob: bytes) -> dict[str, Any] | None:
    """解析 DLGTEMPLATE / DLGTEMPLATEEX 及其控件项。

    标准模板：18 字节定长头 + sz_Or_Ord 变长（menu/class/title），控件项各自
    DWORD 对齐。扩展模板（dlgVer=1 + signature=0xFFFF）头 26 字节且多
    helpID/exStyle，字体段带 weight/italic/charset——现代程序（含
    Process Explorer）几乎全用扩展模板，必须支持而非跳过。
    """
    if len(blob) < 18:
        return None
    head = struct.unpack_from("<I", blob, 0)[0]
    is_ex = (head & 0xFFFF) == 1 and (head >> 16) == 0xFFFF
    if is_ex:
        if len(blob) < 26:
            return None
        _help_id, _exstyle, style = struct.unpack_from("<III", blob, 4)
        cdit, x, y, cx, cy = struct.unpack_from("<HHHHH", blob, 16)
        pos = 26
    else:
        style, _exstyle = struct.unpack_from("<II", blob, 0)
        cdit, x, y, cx, cy = struct.unpack_from("<HHHHH", blob, 8)
        pos = 18
    _mk, _menu, pos = _read_sz_or_ord(blob, pos)
    _ck, _cls, pos = _read_sz_or_ord(blob, pos)
    _tk, title, pos = _read_sz_or_ord(blob, pos)
    font: dict[str, Any] | None = None
    if style & 0x40:  # DS_SETFONT
        if is_ex:
            # 扩展模板字体：pointSize(2) weight(2) italic(1) charset(1) + 字体名
            if pos + 6 <= len(blob):
                point = struct.unpack_from("<H", blob, pos)[0]
                pos += 6  # pointSize(2) + weight(2) + italic(1) + charset(1)
                _fk, face, pos = _read_sz_or_ord(blob, pos)
                font = {"point": point, "face": face if _fk == "str" else None}
        elif pos + 2 <= len(blob):
            point = struct.unpack_from("<H", blob, pos)[0]
            pos += 2
            _fk, face, pos = _read_sz_or_ord(blob, pos)
            font = {"point": point, "face": face if _fk == "str" else None}
    pos = (pos + 3) & ~3  # 每个控件项从 DWORD 边界开始
    controls: list[dict[str, Any]] = []
    for _ in range(cdit):
        if is_ex:
            if pos + 24 > len(blob):
                break
            _chelp, _cex, cstyle = struct.unpack_from("<III", blob, pos)
            ix, iy, icx, icy = struct.unpack_from("<HHHH", blob, pos + 12)
            cid = struct.unpack_from("<I", blob, pos + 20)[0]
            pos += 24
        else:
            if pos + 18 > len(blob):
                break
            cstyle, _cex = struct.unpack_from("<II", blob, pos)
            ix, iy, icx, icy, cid = struct.unpack_from("<HHHHH", blob, pos + 8)
            pos += 18
        ck, cclass, pos = _read_sz_or_ord(blob, pos)
        tk, ctext, pos = _read_sz_or_ord(blob, pos)
        if pos + 2 <= len(blob):  # cbCreationData
            extra = struct.unpack_from("<H", blob, pos)[0]
            pos += 2 + extra
        if ck == "ord" and isinstance(cclass, int):
            class_ord: int | None = cclass
            class_name = DIALOG_CLASS_ORDS.get(cclass)
        else:
            class_ord = None
            class_name = str(cclass) if ck == "str" else None
        if tk == "str":
            text = str(ctext)
        elif tk == "ord":
            text = f"#{ctext}"
        else:
            text = ""
        controls.append(
            {
                "class_ord": class_ord,
                "class_name": class_name,
                "text": text,
                "id": cid,
                "x": ix,
                "y": iy,
                "cx": icx,
                "cy": icy,
                "style": cstyle,
            }
        )
        pos = (pos + 3) & ~3
    return {
        "title": str(title) if _tk == "str" else "",
        "style": style,
        "box": [x, y, cx, cy],
        "font": font,
        "controls": controls,
        "template": "ex" if is_ex else "std",
    }


def read_dialogs(data: bytes) -> dict[str, Any]:
    """读取全部 RT_DIALOG，返回 {dialogs:[...], count:n}。

    对话框模板是"原版界面"最权威的定义：标题、控件类、文本、id、几何全部
    在此，重建时按字节复刻，无需从伪代码猜布局。
    """
    dialogs: list[dict[str, Any]] = []
    for idx, leaf in enumerate(iter_resource_leaves(data, RT_DIALOG)):
        blob = data[leaf["offset"] : leaf["offset"] + leaf["size"]]
        parsed = parse_dialog_template(blob)
        if parsed is None:
            continue
        parsed.update(
            {
                "index": idx,
                "name_id": leaf["name_id"],
                "name": leaf["name"],
                "lang_id": leaf["lang_id"],
                "resource_offset": leaf["offset"],
                "resource_bytes": leaf["size"],
                "raw": blob.hex(),
            }
        )
        dialogs.append(parsed)
    return {"dialogs": dialogs, "count": len(dialogs)}


# ---------------------------------------------------------------------------
# 版本信息 / 图标 / 导入表 / 字符串
# ---------------------------------------------------------------------------


def read_version_info(data: bytes) -> dict[str, str]:
    """解析 RT_VERSION 的 StringFileInfo 键值（产品自称的权威来源）。"""
    got = first_resource(data, RT_VERSION)
    if got is None:
        return {}
    off, size = got
    blob = data[off : off + size]
    out: dict[str, str] = {}
    try:
        pos = 6
        _sig, _sigval, pos = _read_sz_or_ord(blob, pos)
        pos = (pos + 3) & ~3
        value_len = struct.unpack_from("<H", blob, 2)[0]
        if value_len:  # VS_FIXEDFILEINFO
            pos = (pos + value_len + 3) & ~3

        def scan(buf: bytes, start: int, end: int) -> None:
            p = start
            while p + 6 <= end:
                length = struct.unpack_from("<H", buf, p)[0]
                if length < 6 or p + length > end:
                    return
                val_len = struct.unpack_from("<H", buf, p + 2)[0]
                kind, key, after = _read_sz_or_ord(buf, p + 6)
                p2 = (after + 3) & ~3
                low = key.lower() if isinstance(key, str) else ""
                if low.startswith("stringfileinfo") or (
                    isinstance(key, str) and len(key) == 8 and all(c in "0123456789abcdefABCDEF" for c in key)
                ):
                    scan(buf, p2, p + length)  # StringFileInfo / StringTable
                elif val_len > 0 and kind == "str" and key and key not in out:
                    _tk, text, _tp = _read_sz_or_ord(buf, p2)
                    if text:
                        out[str(key)] = str(text)
                p += length

        scan(blob, pos, len(blob))
    except (struct.error, IndexError):
        return {}
    return out


def extract_icon(data: bytes) -> bytes | None:
    """提取第一个 RT_GROUP_ICON 及其 RT_ICON，组装成标准 .ico 字节。"""
    try:
        g1 = first_resource(data, RT_GROUP_ICON)
        if g1 is None:
            return None
        g_off, g_size = g1
        g = data[g_off : g_off + g_size]
        count = struct.unpack_from("<H", g, 4)[0]
        icons = iter_resource_leaves(data, RT_ICON)
        by_id = {i["name_id"]: i for i in icons}
        blobs: list[tuple[bytes, int, int, int]] = []
        entries: list[tuple[int, int, int, int, int, int]] = []
        for i in range(count):
            e = 6 + 14 * i
            if e + 14 > len(g):
                break
            w, h, colors, _r, planes, bits, _size_in, icon_id = struct.unpack_from("<BBBBHHIH", g, e)
            leaf = by_id.get(icon_id)
            if leaf is None:
                continue
            blob = data[leaf["offset"] : leaf["offset"] + leaf["size"]]
            blobs.append((blob, w, h, colors))
            entries.append((w, h, colors, 0, 1, bits))
        if not blobs:
            return None
        ico = struct.pack("<HHH", 0, 1, len(blobs))
        offset = 6 + 16 * len(blobs)
        for idx, (w, h, colors, _r, planes, bits) in enumerate(entries):
            bsize = len(blobs[idx][0])
            ico += struct.pack("<BBBBHHII", w & 0xFF, h & 0xFF, colors & 0xFF, 0, planes, bits, bsize, offset)
            offset += bsize
        for blob, *_ in blobs:
            ico += blob
        return ico
    except (struct.error, IndexError):
        return None


def read_imports(data: bytes) -> list[str]:
    """解析 PE 导入表，返回 DLL 名列表。"""
    out: list[str] = []
    layout = _pe_layout(data)
    if layout is None or not layout["imp_rva"]:
        return out
    try:
        base = _rva2off(layout, layout["imp_rva"])
        if base is None:
            return out
        for i in range(256):  # IMAGE_IMPORT_DESCRIPTOR 20 字节一组，全零结束
            e = base + 20 * i
            if e + 20 > len(data):
                break
            name_rva = struct.unpack_from("<I", data, e + 12)[0]
            if not name_rva:
                break
            noff = _rva2off(layout, name_rva)
            if noff is None:
                continue
            end = data.find(b"\x00", noff)
            if end < 0:
                continue
            name = data[noff:end].decode("ascii", "replace")
            if name and name not in out:
                out.append(name)
    except (struct.error, IndexError):
        return out
    return out


def read_strings(data: bytes, min_len: int = 5, limit: int = 2000) -> list[str]:
    """提取可打印串：ASCII 与 UTF-16LE 两遍，各自独立配额后合并。

    只扫 ASCII 会漏掉 Windows 程序的大半 UI 文本（对话框标题、按钮、提示语
    通常以宽字符存放），而形态判定与重建恰恰依赖这些串。
    """
    out: list[str] = []
    seen: set[str] = set()

    def absorb(matches: list[bytes], decode: str) -> None:
        for raw in matches:
            s = raw.decode(decode, "replace").strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
            if len(out) >= limit:
                return

    absorb(re.findall(rb"(?:[\x20-\x7e]\x00){%d,}" % min_len, data), "utf-16le")
    absorb(re.findall(rb"[\x20-\x7e]{%d,}" % min_len, data), "ascii")
    return out


# ---------------------------------------------------------------------------
# 尾部载荷探测（自解压安装程序的 overlay）
# ---------------------------------------------------------------------------


def _validate_7z(data: bytes, off: int, end: int) -> bool:
    """7z：签名头 32 字节 = 签名(6)+版本(2)+StartHeaderCRC(4)+StartHeader(20)。

    校验 StartHeaderCRC 与"NextHeaderOffset+NextHeaderSize+32 == 载荷长度"双重自洽。
    ``end`` 为归档载荷的有效结尾（重建 exe 会在尾部附 24 字节描述块，须排除）。
    """
    if off + 32 > end:
        return False
    try:
        crc = struct.unpack_from("<I", data, off + 8)[0]
        start_header = data[off + 12 : off + 32]
        if zlib.crc32(start_header) & 0xFFFFFFFF != crc:
            return False
        next_off, next_size = struct.unpack_from("<QQ", data, off + 12)
        return bool(off + 32 + next_off + next_size == end)
    except struct.error:
        return False


def _overlay_start(data: bytes) -> int:
    """overlay 起点 = 最后一节 raw 数据的末尾（节表之后的文件尾区）。"""
    layout = _pe_layout(data)
    if layout is None or not layout["sections"]:
        return 0
    end = 0
    for _vaddr, _vsize, rsize, rptr in layout["sections"]:
        end = max(end, rptr + rsize)
    return min(end, len(data))


def detect_payload(data: bytes) -> dict[str, Any] | None:
    """探测 overlay 里的归档载荷（自解压安装程序的核心事实）。

    只搜 overlay 区（末节 raw 末尾之后）——样本代码/资源里可能也含同名签名
    （如 .rdata 的定位常量），全文件搜索会误取内部巧合命中；取**最先**出现
    且能延伸到 EOF 的候选。7z 校验 StartHeaderCRC 与 NextHeader 自洽，zip
    校验 EOCD，cab 校验长度字段；无签名或校验不过返回 None，由调用方走
    演示模式，不猜。
    """
    start = _overlay_start(data)
    if start >= len(data):
        return None
    # 重建 exe 在尾部附 24 字节载荷描述块（魔数 AHRPLD1），不属于归档载荷；
    # 校验归档自洽长度与搜索签名区都要排除它，否则 7z 的 EOF 自洽会差 24 字节而误判。
    trailer_len = TRAILER_SIZE if len(data) >= TRAILER_SIZE and data[-8:] == TRAILER_MAGIC else 0
    effective_end = len(data) - trailer_len
    region = data[start:effective_end]
    validated: dict[str, Any] | None = None
    unvalidated: dict[str, Any] | None = None
    for name, sig in ARCHIVE_SIGNATURES:
        pos = region.find(sig)  # 取最先出现（载荷紧跟 overlay 起点）
        if pos < 0:
            continue
        off = start + pos
        ok = True
        if name == "7z":
            ok = _validate_7z(data, off, effective_end)
        elif name == "zip":
            ok = data.rfind(b"PK\x05\x06", off, effective_end) >= 0
        elif name == "cab":
            ok = False
            if off + 12 <= effective_end:
                cb = struct.unpack_from("<I", data, off + 8)[0]
                ok = off + cb == effective_end
        cand = {
            "type": name,
            "offset": off,
            "size": effective_end - off,
            "signature": sig.hex(),
        }
        if ok:
            if validated is None or off < validated["offset"]:
                validated = cand
        elif unvalidated is None or off < unvalidated["offset"]:
            unvalidated = cand
    # 有能自洽校验的候选就用它；否则（如纯 gzip 流）才退到未校验命中
    return validated or unvalidated


def append_payload(exe_bytes: bytes, payload: bytes) -> bytes:
    """把载荷追加到 exe 尾部，并附 24 字节描述块（offset/size/魔数）。

    复刻"stub + overlay"的自解压结构：只追加、不改 PE 头，Windows 加载器
    忽略 overlay。描述块让运行时的 exe 能**精确**定位自身载荷，无需签名扫描
    （避免载荷内部再含同签名的歧义）。
    """
    offset = len(exe_bytes)
    trailer = struct.pack("<QQ", offset, len(payload)) + TRAILER_MAGIC
    return exe_bytes + payload + trailer


def read_own_payload(data: bytes) -> tuple[bytes, int] | None:
    """从（可能是重建产物的）exe 字节里读回追加的载荷；无描述块返回 None。"""
    if len(data) < TRAILER_SIZE:
        return None
    offset, size = struct.unpack_from("<QQ", data, len(data) - TRAILER_SIZE)
    if data[len(data) - 8 :] != TRAILER_MAGIC:
        return None
    if offset + size > len(data) - TRAILER_SIZE or offset < 0 or size < 0:
        return None
    return data[offset : offset + size], offset


# ---------------------------------------------------------------------------
# 安装器能力特征（能力词，非产品名）
# ---------------------------------------------------------------------------

INSTALLER_API_SIGNATURES = (
    "shbrowseforfolder",
    "shgetfolderpath",
    "shgetspecialfolderpath",
    "createdialogparam",
    "msctls_progress32",
    "adjusttokenprivileges",
)
INSTALLER_WORDS = (
    "destination folder",
    "select the folder",
    "install folder",
    "&install",
    "setup wizard",
    "uninstall",
)


def installer_signals(strings: list[str], imports: list[str]) -> list[str]:
    """从字符串与导入表挑出安装器能力特征（返回命中项，判定规则在调用方）。"""
    blob = (" ".join(strings[:4000]) + " " + " ".join(str(i) for i in imports)).lower()
    hits = [s for s in INSTALLER_API_SIGNATURES if s in blob]
    hits += [w for w in INSTALLER_WORDS if w in blob]
    return sorted(set(hits))


# ---------------------------------------------------------------------------
# 路径级包装（读文件 → 上面的字节级函数），供流水线阶段与 CLI 工具共用
# ---------------------------------------------------------------------------


def _read_capped(path: Any, cap: int) -> bytes:
    data: bytes = path.read_bytes()
    return data[:cap] if len(data) > cap else data


def local_strings(path: Any, min_len: int = 5, limit: int = 2000) -> list[str]:
    """从样本文件提取可打印串（ASCII + UTF-16LE）。"""
    return read_strings(_read_capped(path, 8 * 1024 * 1024), min_len, limit)


def local_imports(path: Any) -> list[str]:
    """从样本文件解析导入表 DLL 名。"""
    return read_imports(_read_capped(path, 16 * 1024 * 1024))


def local_dialogs(path: Any) -> dict[str, Any]:
    """从样本文件解析全部 RT_DIALOG 模板。"""
    return read_dialogs(_read_capped(path, 16 * 1024 * 1024))
