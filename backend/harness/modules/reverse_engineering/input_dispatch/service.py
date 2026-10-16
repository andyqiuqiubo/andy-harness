"""输入分类器门面：分类判定 + 处理流程路由。

对外主入口：

- :func:`classify_input` —— 只做分类（kind/置信度/依据/证据）；
- :func:`route_input`    —— 分类 + 处理流程（流程/中间产物/预期输出）+ 轻量预分析；
- :class:`InputClassifier` —— 可注入自定义规则/处理器（单测与定制场景）。

设计约束（可扩展性）
====================

核心分类与路由逻辑**只依赖两个注册表**（rules / handlers）：新增输入类型时，
新增规则类 + 处理器类并注册即可，本模块与既有规则/处理器零改动。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from harness.modules.reverse_engineering.input_dispatch.handlers import (
    InputHandler,
    default_handlers,
)
from harness.modules.reverse_engineering.input_dispatch.models import (
    Classification,
    ClassificationSubject,
    InputKind,
    ProcessingPlan,
    RouteResult,
)
from harness.modules.reverse_engineering.input_dispatch.rules import (
    ClassificationRule,
    default_rules,
)

# 分类只依赖头部样本；默认取 1MB（足够覆盖 PE 头/资源目录/源码开头）
_DEFAULT_HEAD_BYTES = 1 << 20
_TEXT_SAMPLE_BYTES = 32 * 1024
_MAX_CONTROL_RATIO = 0.02  # 控制字符占比阈值（\t\n\r 除外）


def _decode_text(sample: bytes) -> tuple[str, bool]:
    """尽力把字节样本解码为文本；返回（文本, 是否严格解码成功）。

    依次尝试 UTF-8 / GBK / UTF-16（后者要求 BOM，避免任意二进制被"成功"误解码）。
    """
    if not sample:
        return "", False
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return sample.decode("utf-16"), True
        except (UnicodeDecodeError, UnicodeError):
            pass
    for enc in ("utf-8", "gbk"):
        try:
            return sample.decode(enc), True
        except (UnicodeDecodeError, UnicodeError):
            continue
    return sample.decode("utf-8", errors="ignore"), False


def _control_ratio(text: str) -> float:
    if not text:
        return 1.0
    bad = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r\f")
    return bad / len(text)


def _make_subject(data: bytes, full_size: int, filename: str, path: str) -> ClassificationSubject:
    sample = data[:_TEXT_SAMPLE_BYTES]
    text, strict = _decode_text(sample)
    text_like = bool(text) and strict and _control_ratio(text) <= _MAX_CONTROL_RATIO
    return ClassificationSubject(
        data=data,
        full_size=full_size,
        filename=filename,
        path=path,
        text_sample=text if text_like else (sample.decode("utf-8", errors="ignore") if sample else ""),
        text_like=text_like,
    )


def subject_from_path(path: str | Path, head_bytes: int = _DEFAULT_HEAD_BYTES) -> ClassificationSubject:
    """从磁盘文件构造分类载体（只读头部样本，避免全量加载大文件）。"""
    p = Path(path)
    with p.open("rb") as fh:
        head = fh.read(head_bytes)
    return _make_subject(head, p.stat().st_size, filename=p.name, path=str(p))


def subject_from_text(text: str, filename: str = "") -> ClassificationSubject:
    """从内存文本构造分类载体（可选 filename 作为扩展名提示）。"""
    data = text.encode("utf-8")
    return _make_subject(data, len(data), filename=filename, path="")


def _unregistered_plan(kind: str) -> ProcessingPlan:
    """未注册处理器的类型：给出显式降级计划（不抛错、可追踪）。"""
    return ProcessingPlan(
        kind=kind,
        flow="unregistered",
        goal=f"输入类型 {kind!r} 尚未注册处理器；请注册对应 InputHandler 后重试",
        steps=[],
        intermediate_artifacts=[],
        expected_outputs=[],
        next_action=f"调用 register_handler 注册 kind={kind!r} 的处理器",
    )


class InputClassifier:
    """输入分类器（规则判定 + 流程路由）。"""

    def __init__(
        self,
        rules: Sequence[ClassificationRule] | None = None,
        handlers: Mapping[str, InputHandler] | None = None,
    ) -> None:
        chosen = list(rules) if rules is not None else default_rules()
        self._rules: list[ClassificationRule] = sorted(chosen, key=lambda r: r.priority, reverse=True)
        self._handlers: dict[str, InputHandler] = dict(handlers if handlers is not None else default_handlers())

    def classify(self, subject: ClassificationSubject) -> Classification:
        """按优先级降序评估规则，首个命中即生效。"""
        for rule in self._rules:
            m = rule.match(subject)
            if m is not None:
                return Classification(
                    kind=m.kind,
                    confidence=m.confidence,
                    reasons=[m.reason],
                    evidence=m.evidence,
                    fallback=rule.is_fallback,
                    matched_rule=rule.name,
                )
        # 理论不可达（兜底规则总在注册表中）；显式返回未知，保证调用方不崩
        return Classification(
            kind=InputKind.BINARY,
            confidence=0.0,
            reasons=["没有任何规则命中（规则注册表可能被清空）"],
            evidence={},
            fallback=True,
            matched_rule="",
        )

    def route(self, subject: ClassificationSubject, *, analyze: bool = True) -> RouteResult:
        """分类 + 选择处理器 → 处理流程（可选轻量预分析）。"""
        classification = self.classify(subject)
        handler = self._handlers.get(classification.kind)
        if handler is None:
            return RouteResult(classification=classification, plan=_unregistered_plan(classification.kind), analysis={})
        plan = handler.build_plan(subject, classification)
        analysis = handler.analyze(subject, classification) if analyze else {}
        return RouteResult(classification=classification, plan=plan, analysis=analysis)


# ---------------------------------------------------------------------------
# 便捷入口（默认分类器：内置规则 + 内置处理器）
# ---------------------------------------------------------------------------


def classify_input(path: str | Path) -> Classification:
    """分类磁盘文件。"""
    return InputClassifier().classify(subject_from_path(path))


def route_input(path: str | Path, *, analyze: bool = True) -> RouteResult:
    """对磁盘文件做「分类 + 处理流程 + 轻量预分析」的一体化路由。"""
    return InputClassifier().route(subject_from_path(path), analyze=analyze)


def route_text(text: str, filename: str = "", *, analyze: bool = True) -> RouteResult:
    """对内存文本做「分类 + 处理流程 + 轻量预分析」的一体化路由。"""
    return InputClassifier().route(subject_from_text(text, filename), analyze=analyze)


def classify_any(content: str | Path, *, filename: str = "", analyze: bool = True) -> dict[str, Any]:
    """通用入口：字符串视为文本内容，路径视为文件；返回可序列化字典。"""
    if isinstance(content, Path):
        return route_input(content, analyze=analyze).to_dict()
    maybe_path = Path(content)
    if filename or not _looks_like_path(content) or not maybe_path.is_file():
        return route_text(content, filename, analyze=analyze).to_dict()
    return route_input(maybe_path, analyze=analyze).to_dict()


def _looks_like_path(text: str) -> bool:
    """保守判断字符串是否像文件路径（避免把自然语言误当路径）。"""
    if "\n" in text or len(text) > 260:
        return False
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|/|\.{1,2}[\\/])", text))
