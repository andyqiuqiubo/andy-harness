"""分类规则集（策略模式 + 注册表）。

判定语义
========

规则按 ``priority`` **降序**评估，**首个命中即生效**（优先级即权威性），规则之间
互相独立、无隐式耦合；新增输入类型 = 新增一个规则类并加 ``@register_rule`` 装饰器，
**核心分类器与既有规则零改动**。

内置优先级设计（数值越大越权威、越先判定）：

===========  =========================  ==========================================
优先级        规则                        判定依据
===========  =========================  ==========================================
101          JavaClassRule              Java class 魔数（0xCAFEBABE）+ 版本/扩展名
100          BinaryMagicRule            魔数自证（PE / ELF / Mach-O），格式与架构
80           SourceCodeRule             源码扩展名映射 + 内容特征（shebang/语法）
60           ExecutableExtensionRule    可执行扩展名软信号（要求内容非文本，
                                         防止"纯文本却命名为 .exe"被误判）
20           TextRule                   可解码文本（低控制字符比）→ 自然语言
5            BinaryFallbackRule         兜底：无法识别 → 通用二进制分诊
===========  =========================  ==========================================
"""

from __future__ import annotations

import re
import struct
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from harness.modules.reverse_engineering.input_dispatch.models import (
    ClassificationSubject,
    InputKind,
    RuleMatch,
)

# ---------------------------------------------------------------------------
# 魔数与架构表（PE / ELF / Mach-O）
# ---------------------------------------------------------------------------

_PE_MACHINE: dict[int, str] = {
    0x014C: "x86",
    0x8664: "x64",
    0x01C0: "arm",
    0x01C4: "armv7",
    0xAA64: "arm64",
    0x0200: "ia64",
    0x5032: "riscv32",
    0x5064: "riscv64",
}
_ELF_MACHINE: dict[int, str] = {
    0x03: "x86",
    0x3E: "x64",
    0x28: "arm",
    0xB7: "arm64",
    0x08: "mips",
    0x14: "ppc",
    0x15: "ppc64",
    0xF3: "riscv",
    0x102: "loongarch",
}
_MACHO_CPU: dict[int, str] = {
    7: "x86",
    0x01000007: "x64",
    12: "arm",
    0x0100000C: "arm64",
    18: "ppc",
}
_PE_SUBSYSTEM: dict[int, str] = {
    1: "native",
    2: "windows_gui",
    3: "windows_console",
    9: "windows_ce_gui",
}


def _pe_summary(data: bytes) -> dict[str, Any] | None:
    """解析 PE 关键事实：架构 / 位数 / 入口点 / 子系统。非 PE 返回 None。"""
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    try:
        pe_off = struct.unpack_from("<I", data, 0x3C)[0]
        if pe_off + 0x18 > len(data) or data[pe_off : pe_off + 4] != b"PE\x00\x00":
            return None
        machine = struct.unpack_from("<H", data, pe_off + 4)[0]
        opt = pe_off + 24
        magic = struct.unpack_from("<H", data, opt)[0]
        bits = 64 if magic == 0x20B else 32
        entry = struct.unpack_from("<I", data, opt + 16)[0]
        subsystem = struct.unpack_from("<H", data, opt + 68)[0]
        return {
            "format": "pe",
            "arch": _PE_MACHINE.get(machine, f"machine_0x{machine:04x}"),
            "bits": bits,
            "entrypoint": f"0x{entry:08x}",
            "subsystem": _PE_SUBSYSTEM.get(subsystem, f"subsystem_{subsystem}"),
        }
    except (struct.error, IndexError):
        return None


def _elf_summary(data: bytes) -> dict[str, Any] | None:
    """解析 ELF 关键事实：架构 / 位数 / 入口点。非 ELF 返回 None。"""
    if len(data) < 24 or data[:4] != b"\x7fELF":
        return None
    try:
        bits = 64 if data[4] == 2 else 32
        endian = "<" if data[5] == 1 else ">"
        machine = struct.unpack_from(f"{endian}H", data, 18)[0]
        entry = struct.unpack_from(f"{endian}{'Q' if bits == 64 else 'I'}", data, 24)[0]
        return {
            "format": "elf",
            "arch": _ELF_MACHINE.get(machine, f"machine_0x{machine:04x}"),
            "bits": bits,
            "entrypoint": f"0x{entry:016x}" if bits == 64 else f"0x{entry:08x}",
            "subsystem": "posix",
        }
    except (struct.error, IndexError):
        return None


def _macho_summary(data: bytes) -> dict[str, Any] | None:
    """解析 Mach-O 关键事实：架构 / 位数 / 胖二进制标记。非 Mach-O 返回 None。"""
    if len(data) < 8:
        return None
    head_be = struct.unpack_from(">I", data, 0)[0]
    head_le = struct.unpack_from("<I", data, 0)[0]
    if head_be == 0xCAFEBABE or head_le == 0xCAFEBABE or head_be == 0xCAFEBABF:
        return {
            "format": "macho",
            "arch": "universal(fat)",
            "bits": 0,
            "entrypoint": "",
            "subsystem": "darwin",
        }
    for endian in (">", "<"):
        magic = struct.unpack_from(f"{endian}I", data, 0)[0]
        if magic in (0xFEEDFACE, 0xFEEDFACF):
            bits = 64 if magic == 0xFEEDFACF else 32
            cpu = struct.unpack_from(f"{endian}I", data, 4)[0]
            return {
                "format": "macho",
                "arch": _MACHO_CPU.get(cpu, f"cpu_0x{cpu:08x}"),
                "bits": bits,
                "entrypoint": "",
                "subsystem": "darwin",
            }
    return None


def _java_class_summary(data: bytes, filename: str) -> dict[str, Any] | None:
    """解析 Java class 文件关键事实：主版本号。非 class 返回 None。

    注意 Java class 魔数 0xCAFEBABE 与 Mach-O fat/universal 冲突，
    这里通过扩展名 ``.class`` 与主版本号范围（40~100）来区分。
    """
    if len(data) < 8 or data[:4] != b"\xca\xfe\xba\xbe":
        return None
    major = struct.unpack_from(">H", data, 6)[0]
    if (filename or "").lower().endswith(".class"):
        return {"format": "java_class", "major_version": major}
    # 无扩展名时，用版本号范围排除 Mach-O fat（nfat_arch 不会落在 40~100）
    if 40 <= major <= 100:
        return {"format": "java_class", "major_version": major}
    return None


# ---------------------------------------------------------------------------
# 源码识别表
# ---------------------------------------------------------------------------

SOURCE_LANGUAGES: dict[str, str] = {
    ".py": "Python",
    ".pyw": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".jsx": "JavaScript(JSX)",
    ".ts": "TypeScript",
    ".tsx": "TypeScript(TSX)",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".c": "C",
    ".h": "C/C++ header",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".cs": "C#",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".scala": "Scala",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".ps1": "PowerShell",
    ".bat": "Batch",
    ".cmd": "Batch",
    ".sql": "SQL",
    ".lua": "Lua",
    ".pl": "Perl",
    ".swift": "Swift",
    ".dart": "Dart",
    ".asm": "Assembly",
    ".s": "Assembly",
    ".r": "R",
    ".m": "Objective-C/MATLAB",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".ini": "INI",
    ".xml": "XML",
}

# 扩展名不可靠/缺失时的内容特征（模式, 语言, 特征名）
_CONTENT_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"<\?php"), "PHP", "<?php 标签"),
    (re.compile(r"^\s*#include\s*[<\"]", re.M), "C/C++", "#include 指令"),
    (re.compile(r"^\s*(?:from\s+[\w.]+\s+)?import\s+[\w.]", re.M), "Python", "import 语句"),
    (re.compile(r"^\s*(?:async\s+)?def\s+\w+\s*\(", re.M), "Python", "def 定义"),
    (re.compile(r"^\s*class\s+\w+.*:", re.M), "Python", "class 定义"),
    (re.compile(r"\bfunc\s+(?:\([^)]*\)\s*)?\w+\s*\(", re.M), "Go", "func 定义"),
    (re.compile(r"^\s*package\s+\w+", re.M), "Go", "package 声明"),
    (re.compile(r"^\s*fn\s+\w+\s*\(", re.M), "Rust", "fn 定义"),
    (re.compile(r"^\s*(?:pub\s+)?(?:struct|impl|enum)\s+\w+", re.M), "Rust", "struct/impl/enum"),
    (re.compile(r"^\s*(?:public|private|protected)\s+(?:static\s+)?(?:final\s+)?class\s+\w+", re.M), "Java", "类声明"),
    (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+\w+\s*\(", re.M), "JavaScript", "function 定义"),
    (re.compile(r"^\s*(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?\(", re.M), "JavaScript", "箭头函数赋值"),
    (re.compile(r"^\s*(?:import|export)\s+.*\bfrom\s+['\"]", re.M), "JavaScript/TypeScript", "ES Module 语句"),
    (re.compile(r"^\s*namespace\s+[\w.]+", re.M), "C#", "namespace 声明"),
    (re.compile(r"^\s*using\s+System", re.M), "C#", "using System"),
    (re.compile(r"^\s*use\s+(?:std|core|alloc)::", re.M), "Rust", "use 声明"),
    (re.compile(r"^\s*SUB\s+\w+|^\s*PROCEDURE\s+\w+", re.M | re.I), "Basic", "SUB/PROCEDURE"),
    (
        re.compile(r"^\s*(?:SELECT|INSERT|UPDATE|DELETE|CREATE)\s+.*\b(?:FROM|INTO|TABLE|SET)\b", re.M | re.I),
        "SQL",
        "SQL 语句",
    ),
    (re.compile(r"^\s*\{[\s\S]{0,80}\"[\w-]+\"\s*:", re.M), "JSON", "JSON 对象"),
)

_SHEBANG_LANGUAGES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^#!.*\bpython[\d.]*"), "Python"),
    (re.compile(r"^#!.*\bnode\b"), "JavaScript"),
    (re.compile(r"^#!.*\b(?:ba|z|k|d)?sh\b"), "Shell"),
    (re.compile(r"^#!.*\bruby\b"), "Ruby"),
    (re.compile(r"^#!.*\bperl\b"), "Perl"),
    (re.compile(r"^#!.*\bphp\b"), "PHP"),
)

# 可执行扩展名（魔数未命中时的软信号；要求内容为非文本）
_EXECUTABLE_EXTS: frozenset[str] = frozenset(
    {".exe", ".dll", ".sys", ".scr", ".ocx", ".cpl", ".so", ".dylib", ".elf", ".com"}
)


def _source_signals(text: str) -> tuple[list[str], str]:
    """扫描文本中的源码内容特征，返回（特征名列表, 语言猜测）。"""
    signals: list[str] = []
    first_lang = ""
    for pattern, lang, label in _CONTENT_PATTERNS:
        if pattern.search(text):
            signals.append(label)
            if not first_lang:
                first_lang = lang
    return signals, first_lang


def _shebang_language(text: str) -> str:
    first_line = text.split("\n", 1)[0].strip()
    if not first_line.startswith("#!"):
        return ""
    for pattern, lang in _SHEBANG_LANGUAGES:
        if pattern.match(first_line):
            return lang
    return "Shell"  # 未知解释器：shebang 本身即脚本强信号


# ---------------------------------------------------------------------------
# 规则基类与内置规则
# ---------------------------------------------------------------------------


class ClassificationRule(ABC):
    """分类规则基类（策略）。新增类型 = 子类化 + ``@register_rule``。"""

    name: ClassVar[str] = ""
    priority: ClassVar[int] = 0
    is_fallback: ClassVar[bool] = False  # 兜底规则：命中即"无法识别"的显式标记

    @abstractmethod
    def match(self, subject: ClassificationSubject) -> RuleMatch | None:
        """命中返回 :class:`RuleMatch`；不命中返回 ``None``。"""


_RULE_REGISTRY: list[type[ClassificationRule]] = []


def register_rule(cls: type[ClassificationRule]) -> type[ClassificationRule]:
    """注册分类规则（装饰器）。核心分类器只读注册表，不感知具体规则。"""
    if cls not in _RULE_REGISTRY:
        _RULE_REGISTRY.append(cls)
    return cls


def unregister_rule(name: str) -> bool:
    """按规则名注销（测试与动态下线用）。返回是否确实移除。"""
    for i, cls in enumerate(_RULE_REGISTRY):
        if cls.name == name:
            del _RULE_REGISTRY[i]
            return True
    return False


def default_rules() -> list[ClassificationRule]:
    """实例化注册表内全部规则（按优先级降序）。"""
    return sorted((cls() for cls in _RULE_REGISTRY), key=lambda r: r.priority, reverse=True)


@register_rule
class BinaryMagicRule(ClassificationRule):
    """魔数判定：PE / ELF / Mach-O —— 格式自证，最高权威。"""

    name = "binary_magic"
    priority = 100

    def match(self, subject: ClassificationSubject) -> RuleMatch | None:
        for summary in (_pe_summary(subject.data), _elf_summary(subject.data), _macho_summary(subject.data)):
            if summary is not None:
                arch = summary.get("arch", "unknown")
                bits = summary.get("bits", 0)
                return RuleMatch(
                    kind=InputKind.EXECUTABLE,
                    confidence=0.99,
                    reason=f"魔数命中：{str(summary['format']).upper()} 可执行格式，架构 {arch}（{bits} 位）",
                    evidence={"detected_by": "magic", **summary},
                )
        return None


@register_rule
class JavaClassRule(ClassificationRule):
    """Java class 判定：魔数 + 扩展名/版本号，优先级高于 Mach-O/PE 魔数。"""

    name = "java_class"
    priority = 101

    def match(self, subject: ClassificationSubject) -> RuleMatch | None:
        summary = _java_class_summary(subject.data, subject.filename)
        if summary is None:
            return None
        major = summary.get("major_version", 0)
        # Java 1.1 = 45，Java 17 = 61，Java 21 = 65
        java_version = f"Java {major - 44}" if major >= 45 else f"version_{major}"
        return RuleMatch(
            kind=InputKind.JAVA_CLASS,
            confidence=0.98,
            reason=f"Java class 魔数命中（0xCAFEBABE），{java_version}，主版本号 {major}",
            evidence={"detected_by": "magic", **summary},
        )


@register_rule
class SourceCodeRule(ClassificationRule):
    """源代码判定：扩展名映射 + 内容特征（shebang / 语法结构）。"""

    name = "source_code"
    priority = 80

    def match(self, subject: ClassificationSubject) -> RuleMatch | None:
        if not subject.text_like:
            return None
        suffix = subject.suffix
        ext_lang = SOURCE_LANGUAGES.get(suffix, "")
        signals, signal_lang = _source_signals(subject.text_sample)
        shebang_lang = _shebang_language(subject.text_sample)

        if not (ext_lang or signals or shebang_lang):
            return None

        if shebang_lang:
            confidence, basis = 0.9, f"shebang 指示 {shebang_lang} 脚本"
        elif ext_lang and signals:
            confidence, basis = 0.95, f"扩展名 {suffix} 与内容特征互证（{signals[0]} 等 {len(signals)} 项）"
        elif ext_lang:
            confidence, basis = 0.85, f"扩展名 {suffix} 映射为 {ext_lang}"
        elif len(signals) >= 2:
            confidence, basis = 0.8, f"内容特征命中 {len(signals)} 项（{signals[0]} 等）"
        else:
            confidence, basis = 0.55, f"仅单一内容特征（{signals[0]}），源码可能性偏高"

        if shebang_lang:
            detected_by = "shebang"
        elif ext_lang and signals:
            detected_by = "extension+content"
        elif ext_lang:
            detected_by = "extension"
        else:
            detected_by = "content"
        return RuleMatch(
            kind=InputKind.SOURCE_CODE,
            confidence=confidence,
            reason=f"识别为源代码：{basis}",
            evidence={
                "detected_by": detected_by,
                "language": shebang_lang or ext_lang or signal_lang or "Unknown",
                "extension": suffix,
                "signals": signals[:8],
            },
        )


@register_rule
class ExecutableExtensionRule(ClassificationRule):
    """可执行扩展名软信号：仅当内容为二进制（非文本）时生效。"""

    name = "executable_extension"
    priority = 60

    def match(self, subject: ClassificationSubject) -> RuleMatch | None:
        suffix = subject.suffix
        if suffix not in _EXECUTABLE_EXTS:
            return None
        if subject.text_like:
            # 内容可解码为文本 → 不据扩展名判可执行（如"纯文本却命名为 .exe"）
            return None
        return RuleMatch(
            kind=InputKind.EXECUTABLE,
            confidence=0.6,
            reason=f"扩展名 {suffix} 指示可执行文件，但未命中已知魔数（内容为二进制）",
            evidence={"detected_by": "extension_only", "format": "unknown", "extension": suffix},
        )


@register_rule
class TextRule(ClassificationRule):
    """文本判定：可解码、低控制字符比 → 自然语言描述。"""

    name = "text"
    priority = 20

    def match(self, subject: ClassificationSubject) -> RuleMatch | None:
        if not subject.text_like or not subject.text_sample:
            return None
        sample = subject.text_sample
        cjk = bool(re.search(r"[\u4e00-\u9fff]", sample))
        lines = sample.count("\n") + 1
        return RuleMatch(
            kind=InputKind.TEXT,
            confidence=0.85,
            reason=f"可解码文本（{len(sample)} 字符样本，{lines} 行{'，含中文' if cjk else ''}），按自然语言描述处理",
            evidence={
                "detected_by": "text",
                "sample_chars": len(sample),
                "lines": lines,
                "has_cjk": cjk,
                "extension": subject.suffix,
            },
        )


@register_rule
class BinaryFallbackRule(ClassificationRule):
    """兜底：无法识别 → 通用二进制分诊（显式 fallback 标记）。"""

    name = "binary_fallback"
    priority = 5
    is_fallback = True

    def match(self, subject: ClassificationSubject) -> RuleMatch | None:
        if subject.full_size == 0:
            reason = "输入为空，无法判定类型"
        elif not subject.text_like:
            reason = "未命中已知魔数/扩展名，且内容不可解码为文本"
        else:
            reason = "未命中已知魔数/扩展名/源码特征"
        return RuleMatch(
            kind=InputKind.BINARY,
            confidence=0.3,
            reason=f"{reason}，按通用二进制分诊处理",
            evidence={"detected_by": "fallback", "size": subject.full_size},
        )
