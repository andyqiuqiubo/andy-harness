"""知识库模块的常量与目录约定。

集中放置分段 / 清洗 / 存储的阈值，避免散落在多个模块里形成「魔法数字」。
目录约定沿用附件模块的风格（可用环境变量覆盖，测试时指向临时目录）。
"""

from __future__ import annotations

import os
from pathlib import Path

# ── 分段阈值（字符数；中文按字符、英文按词，均以字符粗控）─────────────────
# 超长分段会按此上限硬切，并在 metadata 打 truncated 标记（方案 5.2）。
MIN_SEGMENT_CHARS = 20
MAX_SEGMENT_CHARS = 1200
SEGMENT_OVERLAP_CHARS = 80

# S3 父子分段：父分段（携带上下文，参与向量/全文索引）与子分段（精确命中）的规模
PARENT_MAX_CHARS = 2000
CHILD_MAX_CHARS = 400

# S4 问答对：单条 Q / A 的最大长度，超出截断并打标记
QA_QUESTION_MAX_CHARS = 400
QA_ANSWER_MAX_CHARS = 1000

# ── 公共清洗规则（方案 5.2 的 7 条，顺序固定，逐条对每段生效）────────────
# key -> 人类可读描述（用于「清洗规则」在 UI 的展示与审计）
CLEANING_RULES: dict[str, str] = {
    "strip_whitespace": "去除首尾空白与多余空格",
    "remove_control_chars": "移除不可见控制字符（保留换行/制表）",
    "normalize_newlines": "统一换行符为 \\n，折叠连续空行",
    "fix_punctuation": "全角标点归一（重复标点收敛为单个）",
    "remove_zero_width": "移除零宽字符（BOM / ZWSP）",
    "unescape_html": "反转义常见 HTML 实体（&nbsp; &amp; &lt; 等）",
    "collapse_spaces_cjk": "折叠 CJK 之间的多余空格",
}

# ── 分段策略标识（供手动覆盖选择）─────────────────────────────────────
SEGMENT_STRATEGIES: dict[str, str] = {
    "auto": "自动选择",
    "S1": "通用规则分段",
    "S2": "结构感知分段",
    "S3": "父子分段",
    "S4": "问答对分段",
}

# ── Embedding / Rerank 默认值 ────────────────────────────────────────
DEFAULT_EMBEDDING_BATCH = 32
DEFAULT_RERANK_TOP_N = 10
# 未声明维度时用于占位的向量长度（仅在真正调用模型失败时用于降级提示，
# 不会凭空造向量参与检索）
EMBEDDING_DIMS_FALLBACK = 768

# ── 检索默认参数（方案 5.4）────────────────────────────────────────────
RETRIEVAL_MODES: tuple[str, ...] = ("keyword", "semantic", "hybrid")
DEFAULT_TOP_K = 6
RRF_K = 60  #  Reciprocal Rank Fusion 常数
DEFAULT_SCORE_THRESHOLD = 0.0

# ── 资产描述（图文混排图片 OCR/VLM）默认预算 ─────────────────────────
ASSET_DESCRIPTION_MAX_CHARS = 300

# ── 支持的 P1 文本/图文格式（P2 音视频等留待后续 FormatAdapter）───────
TEXT_EXTENSIONS: set[str] = {".txt", ".md", ".markdown", ".csv", ".json", ".log"}
DOCX_EXTENSIONS: set[str] = {".docx"}
PDF_EXTENSIONS: set[str] = {".pdf"}
IMAGE_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# 存储阶段（方案 5.1）
STAGE_P1 = "P1"  # 文本 + 图文混排（首期）
STAGE_P2 = "P2"  # 音视频 / 特殊格式（预留 FormatAdapter 接口）

# 分段策略别名（与 SEGMENT_STRATEGIES 的 key 保持一致）
STRATEGY_AUTO = "auto"
STRATEGY_S1 = "S1"
STRATEGY_S2 = "S2"
STRATEGY_S3 = "S3"
STRATEGY_S4 = "S4"
STRATEGIES: tuple[str, ...] = (STRATEGY_AUTO, STRATEGY_S1, STRATEGY_S2, STRATEGY_S3, STRATEGY_S4)

# 超长分段截断标记（方案 5.2）
TRUNCATION_MARK = "\n…[已截断]"

# 图文混排图片描述：OCR / VLM 均不可用时的降级占位（不得丢分段）
IMAGE_DESC_PLACEHOLDER = "[图片：未能生成描述]"

# 上传大小上限（字节）；比会话附件宽松，知识库以本地导入为主
MAX_UPLOAD_SIZE = int(os.environ.get("HARNESS_KB_MAX_UPLOAD", 10 * 1024 * 1024))  # 10 MB

# 提问分析拆解出的子查询上限（方案 5.5）
MAX_SUBQUERIES = 4

# 单文档原始文件大小上限（比会话附件更宽松，知识库以本地导入为主）
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024  # 20 MB

# ── 落盘根目录（可用环境变量覆盖；测试隔离时指向临时目录）──────────────
DEFAULT_KNOWLEDGE_DIR: str = str(Path(__file__).parent.parent.parent.parent / "data" / "knowledge")


def knowledge_dir() -> str:
    """知识库根目录（确保存在）。原始文件 / 资产均存放于此下。"""
    base = os.environ.get("HARNESS_KNOWLEDGE_DIR") or DEFAULT_KNOWLEDGE_DIR
    Path(base).mkdir(parents=True, exist_ok=True)
    return base


def knowledge_originals_dir(kb_id: str, doc_id: str) -> str:
    """某文档的原始文件目录（确保存在）。"""
    base = Path(knowledge_dir()) / kb_id / "originals" / doc_id
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def knowledge_assets_dir(kb_id: str, doc_id: str) -> str:
    """某文档抽取出的资产（图片等）目录（确保存在）。"""
    base = Path(knowledge_dir()) / kb_id / "assets" / doc_id
    base.mkdir(parents=True, exist_ok=True)
    return str(base)
