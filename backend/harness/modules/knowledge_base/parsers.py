"""P1 文本 / 图文抽取 —— txt / md / csv / docx / pdf / 纯图片。

方案 5.1：P1 覆盖文本与图文混排。每种格式产出
「清洗后正文 + 资产列表（图片 + OCR/VLM 描述）」。

依赖约束：本项目运行环境**没有** pypdf / python-docx，故：

- ``.docx`` 本质是 zip，用标准库 ``zipfile`` 读 ``word/document.xml``，
  按 ``<w:p>`` 段落、``<w:t>`` 文本、``<w:drawing>`` 图片还原；
- ``.pdf`` 不做完整渲染，仅用 ``zlib`` 解 ``FlateDecode`` 流并按文本操作符
  (Tj / TJ) 粗提取正文，再交给清洗与分段；图片 XObject 单独抽取为资产。

图片描述（图文混排，方案 5.1 要求「不丢分段」）：
优先调用已配置的 VLM（复用 Provider 的 OpenAI 兼容视觉接口）；不可用时降级到
OCR；两者都不可用时保留空描述——**但图片仍作为资产登记**，正文分段不因缺
描述而丢失。本模块只负责「把图片变成 asset + 尽力补描述」，具体描述由调用方
注入（避免本模块直接依赖模型配置）。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any

from .chunking import Asset
from .limits import DOCX_EXTENSIONS, IMAGE_EXTENSIONS, PDF_EXTENSIONS, TEXT_EXTENSIONS


class ParseError(Exception):
    """文档解析失败。"""


# ── P2 FormatAdapter 挂载点（方案 5.1：音视频/特殊格式的适配器接口）──────
class FormatAdapter:
    """P2 存储阶段的格式适配器契约（架构预留，本期不内置实现）。

    实现方提供 ``extensions``（后缀集合）与 ``parse``（返回同 P1 的
    ``(正文, 资产列表)``），再经 :func:`register_adapter` 注册即可接入
    摄取管道，无需改动 service / REST 层。
    """

    extensions: set[str] = set()

    def parse(self, data: bytes, filename: str, assets_dir: str, doc_id: str) -> tuple[str, list[Asset]]:
        raise NotImplementedError


_ADAPTERS: dict[str, FormatAdapter] = {}


def register_adapter(adapter: FormatAdapter) -> None:
    """注册 P2 格式适配器（同后缀后来者覆盖先前者）。"""
    for ext in adapter.extensions:
        _ADAPTERS[ext.lower()] = adapter


def has_adapter(filename: str) -> bool:
    """该文件后缀是否已注册 P2 适配器（决定能否进入摄取管道）。"""
    return Path(filename).suffix.lower() in _ADAPTERS


def detect_stage(filename: str) -> str:
    """依据后缀判定存储阶段：P1（文本/图文），其余归 P2（音视频/特殊格式）。

    P2 中已注册适配器的格式可被 :func:`parse_document` 实际解析；
    未注册的在 service 层拒绝并提示暂不支持。
    """
    ext = Path(filename).suffix.lower()
    if ext in TEXT_EXTENSIONS or ext in DOCX_EXTENSIONS or ext in PDF_EXTENSIONS or ext in IMAGE_EXTENSIONS:
        return "P1"
    return "P2"


def _decode_text(data: bytes) -> str:
    """按 UTF-8 → GBK 依次尝试解码纯文本文件。"""
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ── 纯文本 / Markdown / CSV / JSON / log ──────────────────────────────


def parse_text(data: bytes, filename: str) -> tuple[str, list[Asset]]:
    """纯文本族：直接解码，无资产。"""
    return _decode_text(data), []


# ── 图片（单图即一个资产；正文为空，靠描述参与检索）────────────────────


def parse_image(
    data: bytes, filename: str, storage_path: str, asset_id: str, description: str = ""
) -> tuple[str, list[Asset]]:
    """单图：正文为空，产出单一资产（描述由调用方注入）。"""
    asset = Asset(id=asset_id, filename=filename, storage_path=storage_path, description=description)
    return "", [asset]


# ── DOCX（标准库 zipfile + XML 正则还原段落 / 表格 / 图片）─────────────

_DOCX_P_RE = re.compile(r"<w:p\b[^>]*>.*?</w:p>", re.DOTALL)
_DOCX_T_RE = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.DOTALL)
_DOCX_TAB_RE = re.compile(r"<w:tab\b[^>]*/?>")
_DOCX_BR_RE = re.compile(r"<w:br\b[^>]*/?>")
_DOCX_DRAWING_RE = re.compile(r"<w:drawing\b[^>]*>.*?</w:drawing>", re.DOTALL)


def _unescape_xml(s: str) -> str:
    """反转义 XML 文本节点中的五个预定义实体。"""
    return (
        s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")
    )


def parse_docx(data: bytes, assets_dir: str, doc_id: str) -> tuple[str, list[Asset]]:
    """解析 DOCX：按段落还原正文，表格内单元格以制表符连接，抽取图片为资产。

    图片字节从 ``word/media/`` 取出落盘到 assets_dir，得到 Asset（描述暂空，
    由上层按需补 OCR/VLM）。本函数不因图片抽取失败而中断正文还原。
    """
    text_parts: list[str] = []
    assets: list[Asset] = []
    try:
        zf = zipfile.ZipFile(_BytesReader(data))
    except Exception as e:  # noqa: BLE001
        raise ParseError(f"DOCX 无法打开（非有效 zip）: {e}") from e
    with zf:
        names = zf.namelist()
        # 1) 图片关系表：rId -> media 路径
        rel_targets: dict[str, str] = {}
        for rel_name in [n for n in names if n.startswith("word/_rels/") and n.endswith(".rels")]:
            try:
                rel_xml = zf.read(rel_name).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            for m in re.finditer(
                r'<Relationship\b[^>]*Id="([^"]+)"[^>]*Type="[^"]*/image"[^>]*Target="([^"]+)"', rel_xml
            ):
                rel_targets[m.group(1)] = m.group(2)
        if "word/document.xml" not in names:
            raise ParseError("DOCX 缺少 word/document.xml")
        doc_xml = zf.read("word/document.xml").decode("utf-8", errors="replace")

        media_idx = 0
        for p_match in _DOCX_P_RE.finditer(doc_xml):
            p_xml = p_match.group(0)
            # 段落内文本节点
            runs: list[str] = []
            for t in _DOCX_T_RE.finditer(p_xml):
                runs.append(_unescape_xml(t.group(1)))
            line = "".join(runs)
            line = _DOCX_TAB_RE.sub("\t", line)
            line = _DOCX_BR_RE.sub("\n", line)
            # 表格单元格边界（w:tc）已由空 w:p 分段，这里对空段跳过
            # 图片：从段内 drawing 数出 embed rId
            for draw in _DOCX_DRAWING_RE.finditer(p_xml):
                rid_m = re.search(r'r:embed="([^"]+)"', draw.group(0))
                target = rel_targets.get(rid_m.group(1)) if rid_m else None
                if not target:
                    continue
                media_path = "word/" + target.lstrip("/").replace("../", "")
                media_path = media_path.replace("word//", "word/")
                if media_path not in names:
                    # 尝试去重前缀后的直接匹配
                    alt = next((n for n in names if n.endswith(target.split("/")[-1])), None)
                    if not alt:
                        continue
                    media_path = alt
                try:
                    raw = zf.read(media_path)
                except Exception:  # noqa: BLE001
                    continue
                media_idx += 1
                filename = Path(media_path).name
                storage_path = _write_asset(assets_dir, doc_id, media_idx, filename, raw)
                assets.append(
                    Asset(
                        id=f"{doc_id}_img{media_idx}",
                        filename=filename,
                        storage_path=storage_path,
                        description="",
                    )
                )
            if line.strip():
                text_parts.append(line)
    return "\n".join(text_parts), assets


class _BytesReader:
    """把 bytes 包成 file-like 供 zipfile 使用。"""

    def __init__(self, data: bytes) -> None:
        import io

        self._io = io.BytesIO(data)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN001
        return getattr(self._io, name)


def _write_asset(assets_dir: str, doc_id: str, idx: int, filename: str, raw: bytes) -> str:
    """把资产字节落盘到 assets_dir，返回存储路径。"""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in Path(filename).name) or "asset"
    out = Path(assets_dir) / f"{idx:04d}__{safe}"
    out.write_bytes(raw)
    return str(out)


# ── PDF（zlib 解 FlateDecode + 文本操作符粗提取）───────────────────────

_PDF_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_PDF_TJ_RE = re.compile(rb"\((?:[^()\\]|\\.)*\)\s*Tj")
_PDF_TJ_ARRAY_RE = re.compile(rb"\[(?:[^\[\]]|\((?:[^()\\]|\\.)*\))*\]\s*TJ")
_PDF_PDFSTR_RE = re.compile(rb"\((?:[^()\\]|\\.)*\)")
_PDF_XOBJECT_IMAGE_RE = re.compile(rb"/Subtype\s*/Image")


def _unescape_pdf_literal(raw: bytes) -> bytes:
    """反转义 PDF 字面字符串的转义序列（括号与反斜杠）。"""
    out = bytearray()
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == 0x5C and i + 1 < len(raw):  # backslash
            nxt = raw[i + 1]
            if nxt in b"nrtbf":
                out.append({"n": 10, "r": 13, "t": 9, "b": 8, "f": 12}[chr(nxt)])
                i += 2
                continue
            if 0x30 <= nxt <= 0x37:  # octal up to 3 digits
                oct_digits = b""
                j = i + 1
                while j < len(raw) and len(oct_digits) < 3 and 0x30 <= raw[j] <= 0x37:
                    oct_digits += bytes([raw[j]])
                    j += 1
                out.append(int(oct_digits, 8))
                i = j
                continue
            out.append(nxt)
            i += 2
            continue
        out.append(b)
        i += 1
    return bytes(out)


def parse_pdf(data: bytes, assets_dir: str, doc_id: str) -> tuple[str, list[Asset]]:
    """解析 PDF：解 FlateDecode 流，按 Tj / TJ 文本操作符粗提取正文。

    非完整渲染器，目标是「拿到可检索的正文」；抽取失败或加密 PDF 会抛 ParseError。
    图片 XObject 以 DCTDecode/FlateDecode 原样落盘为资产（描述留空）。
    """
    import zlib

    text_parts: list[str] = []
    assets: list[Asset] = []
    media_idx = 0
    saw_text = False
    for m in _PDF_STREAM_RE.finditer(data):
        raw = m.group(1)
        try:
            decoded = zlib.decompress(raw)
        except Exception:  # noqa: BLE001
            continue  # 非 zlib 流（图片/字体二进制等）跳过
        # 文本操作符
        has_text_op = b" Tj" in decoded or b" TJ" in decoded
        if has_text_op:
            chunk: list[str] = []
            for tj in _PDF_TJ_RE.finditer(decoded):
                for lit in _PDF_PDFSTR_RE.finditer(tj.group(0)):
                    chunk.append(_unescape_pdf_literal(lit.group(0)[1:-1]).decode("latin-1", errors="replace"))
            for tja in _PDF_TJ_ARRAY_RE.finditer(decoded):
                for lit in _PDF_PDFSTR_RE.finditer(tja.group(0)):
                    chunk.append(_unescape_pdf_literal(lit.group(0)[1:-1]).decode("latin-1", errors="replace"))
            if chunk:
                saw_text = True
                text_parts.append("".join(chunk))
        # 图片 XObject（粗判：含 /Subtype /Image）
        if _PDF_XOBJECT_IMAGE_RE.search(decoded):
            # 图片通常是二进制流，zlib 解出的概率低；此处尽力而为
            media_idx += 1
            filename = f"pdf_img{media_idx}.bin"
            storage_path = _write_asset(assets_dir, doc_id, media_idx, filename, raw)
            assets.append(
                Asset(id=f"{doc_id}_img{media_idx}", filename=filename, storage_path=storage_path, description="")
            )
    if not saw_text and not assets:
        raise ParseError("PDF 未抽取到文本或图片（可能为扫描件或不受支持的编码）")
    return "\n".join(text_parts), assets


# ── 统一入口 ──────────────────────────────────────────────────────────


def parse_document(
    data: bytes,
    filename: str,
    assets_dir: str,
    doc_id: str,
) -> tuple[str, list[Asset]]:
    """依据后缀分派到对应解析器，返回 (清洗前正文, 资产列表)。

    清洗与分段在 service 层统一进行（保证清洗规则对所有格式一致）。
    P2 格式若已注册 :class:`FormatAdapter` 则交由适配器解析。
    """
    ext = Path(filename).suffix.lower()
    if ext in DOCX_EXTENSIONS:
        return parse_docx(data, assets_dir, doc_id)
    if ext in PDF_EXTENSIONS:
        return parse_pdf(data, assets_dir, doc_id)
    if ext in IMAGE_EXTENSIONS:
        asset_id = f"{doc_id}_img1"
        storage_path = _write_asset(assets_dir, doc_id, 1, Path(filename).name, data)
        return parse_image(data, filename, storage_path, asset_id)
    adapter = _ADAPTERS.get(ext)
    if adapter is not None:
        return adapter.parse(data, filename, assets_dir, doc_id)
    # 默认按纯文本处理（txt/md/csv/json/log/未知文本后缀）
    return parse_text(data, filename)
