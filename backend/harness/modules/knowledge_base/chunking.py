"""分段与清洗 —— 策略 S1/S2/S3/S4 + 7 条公共清洗规则。

每个策略把「清洗后的纯文本 + 资产列表」切成一组分段；分段带 metadata
（标题层级、是否截断、资产引用等），供检索结果来源标注与前端展示。

策略一览（方案 5.2）：
- ``S1`` 通用规则：按段落聚合到 MAX_SEGMENT_CHARS，带少量重叠；
- ``S2`` 结构感知：识别 Markdown / 标题层级，优先在标题边界切分，
  子段携带所属标题路径（metadata.headings）；
- ``S3`` 父子：先切父段（含上下文），再在父段内切子段；检索时子段精确命中，
  展示时回填父上下文；
- ``S4`` 问答对：按 Q/A 配对（「问：…答：…」）拆成 QA 段。

``choose_strategy`` 依据文本特征自动选择，用户可在文档维度手动覆盖。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field

from .limits import (
    ASSET_DESCRIPTION_MAX_CHARS,
    CHILD_MAX_CHARS,
    MAX_SEGMENT_CHARS,
    MIN_SEGMENT_CHARS,
    PARENT_MAX_CHARS,
    QA_ANSWER_MAX_CHARS,
    QA_QUESTION_MAX_CHARS,
    SEGMENT_OVERLAP_CHARS,
)


@dataclass
class Asset:
    """图文混排中的一张图片资产（图片字节落盘后的引用）。"""

    id: str
    filename: str
    storage_path: str
    description: str = ""  # OCR / VLM 描述；无则空
    page: int = 0


@dataclass
class Segment:
    """一个分段。content 为参与展示/向量的正文，fulltext 参与全文索引。"""

    content: str
    fulltext: str
    parent_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    content_hash: str = ""
    # 本地稳定 id：父子分段中，子段的 parent_id 指向父段的 id；
    # 落库时由 repository 复用为行主键，保证库内 parent_id 可 JOIN 回溯父段。
    id: str = ""

    def compute_hash(self) -> str:
        """对正文算稳定 hash（用于去重 / 增量重建判定）。"""
        if not self.content_hash:
            self.content_hash = hashlib.sha1(self.content.encode("utf-8")).hexdigest()
        return self.content_hash


# ── 7 条公共清洗规则（顺序固定）───────────────────────────────────────

_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HTML_ENTITIES: dict[str, str] = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
}
_REPEAT_PUNCT_RE = re.compile(r"([。．.!?！？；;…])\1{1,}")


def clean_text(text: str) -> str:
    """依次执行 7 条公共清洗规则，返回清洗后的正文。

    顺序有意义：先去零宽/控制字符，再归一换行与实体，最后处理 CJK 空格与
    重复标点，避免前一步引入的字符被后一步误处理。
    """
    if not text:
        return ""
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    for ent, ch in _HTML_ENTITIES.items():
        text = text.replace(ent, ch)
    text = _REPEAT_PUNCT_RE.sub(r"\1", text)
    # CJK 之间的多余空格（PDF 抽取常见噪声）折叠掉；
    # 注意只能是空格/制表符——换行是分段策略的结构依据，绝不能在此被吃掉。
    text = re.sub(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])", "", text)
    text = "\n".join(re.sub(r"[ \t]{2,}", " ", line).strip() for line in text.split("\n"))
    return text.strip()


def build_fulltext(content: str, assets: list[Asset]) -> str:
    """构造参与全文索引的文本：正文 + 各资产描述（图文混排不丢分段）。"""
    parts = [content.strip()]
    for asset in assets:
        desc = (asset.description or "").strip()
        if desc:
            parts.append(f"[图片 {asset.filename}] {desc[:ASSET_DESCRIPTION_MAX_CHARS]}")
    return "\n".join(p for p in parts if p)


# ── 切分原语 ────────────────────────────────────────────────────────


def _hard_split(text: str, limit: int) -> list[str]:
    """把超过 limit 的文本按字符硬切，段间保留重叠。

    调用方须对产出的每个片段打 ``truncated=True``（方案 5.2.4：超长且无自然
    分隔点时强制截断并标记）——标记的是「被迫硬切」这件事，不是最终片长。
    """
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    start = 0
    step = max(1, limit - SEGMENT_OVERLAP_CHARS)
    while start < len(text):
        out.append(text[start : start + limit])
        start += step
    return out


def _mark_truncated(segments: list[Segment]) -> None:
    """兜底：任何仍超过 MAX_SEGMENT_CHARS 的分段打 truncated 标记。"""
    for seg in segments:
        if len(seg.content) > MAX_SEGMENT_CHARS:
            seg.metadata["truncated"] = True


def _mk(content: str, assets: list[Asset], **meta: object) -> Segment:
    """构造一个分段：正文即展示内容，fulltext 追加资产描述。"""
    seg = Segment(content=content, fulltext=build_fulltext(content, assets), metadata=dict(meta), id=uuid.uuid4().hex)
    seg.compute_hash()
    return seg


# ── 策略 S1：通用规则分段 ─────────────────────────────────────────────


def chunk_s1(text: str, assets: list[Asset]) -> list[Segment]:
    """按空行聚成段落后，贪心合并到 MAX_SEGMENT_CHARS。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()] if text.strip() else []
    segments: list[Segment] = []
    buf = ""
    for para in paragraphs:
        if len(para) > MAX_SEGMENT_CHARS:
            if buf.strip():
                segments.append(_mk(buf, assets))
                buf = ""
            for piece in _hard_split(para, MAX_SEGMENT_CHARS):
                segments.append(_mk(piece, assets, truncated=True))
            continue
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) <= MAX_SEGMENT_CHARS:
            buf = candidate
        else:
            if buf.strip():
                segments.append(_mk(buf, assets))
            buf = para
    if buf.strip():
        segments.append(_mk(buf, assets))
    _mark_truncated(segments)
    return segments


# ── 策略 S2：结构感知分段 ─────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def chunk_s2(text: str, assets: list[Asset]) -> list[Segment]:
    """识别 Markdown 标题，优先在标题边界切分并记录标题路径。"""
    lines = text.split("\n")
    sections: list[tuple[list[str], str]] = []
    headings: list[str] = []
    current_title = ""
    current_lines: list[str] = []
    for line in lines:
        m = _HEADING_RE.match(line.strip())
        if m:
            if current_lines:
                sections.append((current_lines, current_title))
            level = len(m.group(1))
            title = m.group(2).strip()
            headings = headings[: level - 1] + [title]
            current_title = " / ".join(h for h in headings if h)
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_lines, current_title))

    if not any(title for _lines, title in sections):
        return chunk_s1(text, assets)

    segments: list[Segment] = []
    for sec_lines, title in sections:
        body = "\n".join(sec_lines).strip()
        if not body:
            continue
        over = len(body) > MAX_SEGMENT_CHARS
        pieces = [body] if not over else _hard_split(body, MAX_SEGMENT_CHARS)
        for piece in pieces:
            seg = _mk(piece, assets, truncated=over)
            if title:
                seg.metadata["headings"] = title
            segments.append(seg)
    _mark_truncated(segments)
    return segments


# ── 策略 S3：父子分段 ─────────────────────────────────────────────────


def chunk_s3(text: str, assets: list[Asset]) -> list[Segment]:
    """先按段落聚成父段，再在父段内切子段；子段 parent_id 指向父段。

    展示顺序：父段在前（携带上下文），子段随后。检索以子段为主。
    """
    parents = chunk_s1(text, assets)
    if all(len(p.content) <= CHILD_MAX_CHARS for p in parents):
        return parents

    segments: list[Segment] = []
    for parent in parents:
        parent_id = uuid.uuid4().hex
        parent.id = parent_id  # 子段 parent_id 指向此 id，落库后为行主键
        parent.metadata["role"] = "parent"
        segments.append(parent)
        for child in _hard_split(parent.content, CHILD_MAX_CHARS):
            cseg = _mk(child, assets, role="child", headings=parent.metadata.get("headings", ""))
            cseg.parent_id = parent_id
            segments.append(cseg)
    return segments


# ── 策略 S4：问答对分段 ───────────────────────────────────────────────

_QA_Q_RE = re.compile(r"^\s*(?:问|Q|Question)\s*[:：]", re.IGNORECASE | re.MULTILINE)
_QA_A_RE = re.compile(r"^\s*(?:答|A|Answer)\s*[:：]", re.IGNORECASE | re.MULTILINE)


def chunk_s4(text: str, assets: list[Asset]) -> list[Segment]:
    """按「问：/答：」结构拆成 QA 段；未命中结构时退化为 S2。"""
    lines = text.split("\n")
    qa_pairs: list[tuple[str, str]] = []
    q: list[str] = []
    a: list[str] = []
    mode = ""
    for line in lines:
        if _QA_Q_RE.match(line):
            if q and a:
                qa_pairs.append(("\n".join(q), "\n".join(a)))
            q = [_QA_Q_RE.sub("", line)]
            a = []
            mode = "q"
        elif _QA_A_RE.match(line):
            a = [_QA_A_RE.sub("", line)]
            mode = "a"
        elif mode == "q":
            q.append(line)
        elif mode == "a":
            a.append(line)
    if q and a:
        qa_pairs.append(("\n".join(q), "\n".join(a)))

    if not qa_pairs:
        return chunk_s2(text, assets)

    segments: list[Segment] = []
    for question, answer in qa_pairs:
        q_text = question.strip()[:QA_QUESTION_MAX_CHARS]
        a_text = answer.strip()[:QA_ANSWER_MAX_CHARS]
        content = f"问：{q_text}\n答：{a_text}"
        seg = _mk(content, assets, role="qa", question=q_text)
        segments.append(seg)
    return segments


# ── 自动策略选择 ──────────────────────────────────────────────────────


def build_asset_segment(asset: Asset) -> Segment:
    """为图片资产构造独立分段（方案 5.1.2：图片抽取为资产并参与检索）。

    图片描述（OCR/VLM）非空时并入全文；无描述时以文件名占位，
    保证图片至少可按文件名检索——**不因缺描述而丢弃**。
    """
    desc = (asset.description or "").strip()
    content = f"[图片 {asset.filename}]" + (f" {desc}" if desc else "")
    return _mk(content, [], role="asset", asset_ids=[asset.id], filename=asset.filename)


def choose_strategy(text: str) -> str:
    """依据文本特征自动选择分段策略。

    判定优先级：问答对（S4）→ 有标题层级（S2）→ 长文（S3 父子）→ 通用（S1）。
    """
    if not text.strip():
        return "S1"
    if _QA_Q_RE.search(text) and _QA_A_RE.search(text):
        return "S4"
    if any(_HEADING_RE.match(line.strip()) for line in text.split("\n")):
        return "S2"
    if len(text) > PARENT_MAX_CHARS * 2:
        return "S3"
    return "S1"


_STRATEGY_FUNCS = {
    "S1": chunk_s1,
    "S2": chunk_s2,
    "S3": chunk_s3,
    "S4": chunk_s4,
}


def segment_document(text: str, assets: list[Asset], strategy: str = "auto") -> tuple[list[Segment], str]:
    """按策略切分，返回 (分段列表, 实际使用的策略)。

    strategy 为 ``auto`` 时自动选择；其余为手动覆盖（S1/S2/S3/S4）。
    """
    chosen = choose_strategy(text) if strategy in ("", "auto") else strategy
    func = _STRATEGY_FUNCS.get(chosen, chunk_s1)
    segments = func(text, assets)
    # 过滤空段；父段、QA 段与图片资产段即便短也保留（承载上下文 / 资产引用）
    segments = [
        s
        for s in segments
        if len(s.content.strip()) >= MIN_SEGMENT_CHARS or s.metadata.get("role") in ("parent", "qa", "asset")
    ]
    return segments, chosen
