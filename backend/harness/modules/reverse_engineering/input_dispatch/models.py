"""输入分类与处理流程路由 —— 数据模型与公共词汇。

设计要点
========

- ``kind`` 使用**普通字符串**而非强枚举：内置类型由 :class:`InputKind` 提供标准
  词汇；第三方新增类型可直接使用任意新字符串（如 ``"archive"``），**无需修改本模块**。
- 全部模型提供 ``to_dict()``，可直接序列化给 REST / 前端 / 证据包。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class InputKind(StrEnum):
    """内置输入类型词汇表（扩展类型可直接使用新的字符串值）。"""

    EXECUTABLE = "executable"  # 可执行文件 → 逆向分析 → 生成功能等价的新可执行文件
    JAVA_CLASS = "java_class"  # Java class 字节码 → 反编译为 Java 源码/结构表示
    SOURCE_CODE = "source_code"  # 源代码 → 代码理解 / 重构
    TEXT = "text"  # 自然语言描述 → 需求实现
    BINARY = "binary"  # 兜底：无法识别的二进制 → 通用分诊


@dataclass
class ClassificationSubject:
    """归一化的待分类输入（文件与内存文本使用同一载体）。

    ``data`` 只携带**头部样本**（探测器仅看头部，避免全量读大文件）；文本类
    判定依据 ``text_sample`` / ``text_like``，由 service 的工厂函数统一填充。
    """

    data: bytes  # 头部样本
    full_size: int  # 原始总字节数
    filename: str = ""  # 文件名（含扩展名；纯文本输入可留空）
    path: str = ""  # 源文件路径（纯文本输入为空）
    text_sample: str = ""  # 可解码文本样本（不可解码时尽力而为）
    text_like: bool = False  # 是否"像文本"（严格解码成功且低控制字符比）

    @property
    def display_name(self) -> str:
        return self.filename or self.path or "<inline>"

    @property
    def suffix(self) -> str:
        """小写扩展名（含点）；无扩展名返回空串。"""
        name = self.filename or self.path
        idx = name.rfind(".")
        return name[idx:].lower() if idx > 0 else ""


@dataclass
class RuleMatch:
    """单条分类规则的命中结果。"""

    kind: str
    confidence: float  # 0~1，越高越可信
    reason: str  # 人类可读的判定依据
    evidence: dict[str, Any] = field(default_factory=dict)  # 结构化证据（魔数/扩展名/语言…）


@dataclass
class Classification:
    """分类结论。"""

    kind: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    fallback: bool = False  # 是否由兜底规则判定（无法识别）
    matched_rule: str = ""  # 命中的规则名（可追踪性）

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "confidence": round(self.confidence, 3),
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
            "fallback": self.fallback,
            "matched_rule": self.matched_rule,
        }


@dataclass
class FlowStep:
    """处理流程中的单步。"""

    name: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description}


@dataclass
class ProcessingPlan:
    """某类型输入对应的处理流程定义（流程 / 中间产物 / 预期输出）。"""

    kind: str
    flow: str  # 流程标识
    goal: str  # 最终目标
    steps: list[FlowStep] = field(default_factory=list)
    intermediate_artifacts: list[str] = field(default_factory=list)  # 中间产物
    expected_outputs: list[str] = field(default_factory=list)  # 预期输出
    next_action: str = ""  # 下一步动作提示（如何真正执行该流程）

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "flow": self.flow,
            "goal": self.goal,
            "steps": [s.to_dict() for s in self.steps],
            "intermediate_artifacts": list(self.intermediate_artifacts),
            "expected_outputs": list(self.expected_outputs),
            "next_action": self.next_action,
        }


@dataclass
class RouteResult:
    """分类 + 处理流程的完整路由结果。"""

    classification: Classification
    plan: ProcessingPlan
    analysis: dict[str, Any] = field(default_factory=dict)  # 轻量预分析（中间产物预览）

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification.to_dict(),
            "plan": self.plan.to_dict(),
            "analysis": dict(self.analysis),
        }
