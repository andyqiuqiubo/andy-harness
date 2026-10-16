"""处理流程策略（每种输入类型一个处理器 + 注册表）。

每个处理器定义两件事：

- :meth:`InputHandler.build_plan` —— 该类型的**处理流程 / 中间产物 / 预期输出**；
- :meth:`InputHandler.analyze`    —— **轻量预分析**（不改动输入、只产出结构化事实，
  作为流程中间产物的预览与后续步骤的输入）。

新增输入类型 = 新增处理器类 + ``@register_handler``，核心分类/路由逻辑零改动。
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from pathlib import Path
from typing import Any, ClassVar

from harness.modules.reverse_engineering import pe_resources
from harness.modules.reverse_engineering.input_dispatch.models import (
    Classification,
    ClassificationSubject,
    FlowStep,
    InputKind,
    ProcessingPlan,
)

# 预分析阶段读取上限：超过则只报告规模、跳过深度预览（避免拖慢接口）
_ANALYZE_MAX_BYTES = 32 * 1024 * 1024


def _read_capped(path: Path, cap: int) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(cap)
    except OSError:
        return b""


def _entropy(data: bytes) -> float:
    """香农熵（bit/byte，0~8）：越高越接近压缩/加密数据。"""
    if not data:
        return 0.0
    counts = Counter(data)
    total = len(data)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


# ---------------------------------------------------------------------------
# 处理器基类与注册表
# ---------------------------------------------------------------------------


class InputHandler(ABC):
    """输入类型处理器基类（策略）。"""

    kind: ClassVar[str] = ""

    @abstractmethod
    def build_plan(self, subject: ClassificationSubject, classification: Classification) -> ProcessingPlan:
        """定义该类型的处理流程、中间产物与预期输出。"""

    def analyze(self, subject: ClassificationSubject, classification: Classification) -> dict[str, Any]:
        """轻量预分析（可选覆写）。默认不产出。"""
        return {}


_HANDLER_REGISTRY: dict[str, type[InputHandler]] = {}


def register_handler(cls: type[InputHandler]) -> type[InputHandler]:
    """注册处理器（装饰器），按 ``kind`` 索引；同 kind 后注册者覆盖先注册者。"""
    _HANDLER_REGISTRY[cls.kind] = cls
    return cls


def unregister_handler(kind: str) -> bool:
    """按 kind 注销处理器（测试与动态下线用）。"""
    return _HANDLER_REGISTRY.pop(kind, None) is not None


def default_handlers() -> dict[str, InputHandler]:
    """实例化注册表内全部处理器。"""
    return {kind: cls() for kind, cls in _HANDLER_REGISTRY.items()}


# ---------------------------------------------------------------------------
# 可执行文件：逆向分析 → 生成功能等价的新 exe
# ---------------------------------------------------------------------------


@register_handler
class ExecutableHandler(InputHandler):
    """可执行文件处理流程：全链路逆向 + 功能等价重建。"""

    kind = InputKind.EXECUTABLE

    def build_plan(self, subject: ClassificationSubject, classification: Classification) -> ProcessingPlan:
        return ProcessingPlan(
            kind=self.kind,
            flow="executable_reverse_engineering",
            goal="完成全链路逆向分析，并重建生成一个与原 exe 功能等价的新可执行文件",
            steps=[
                FlowStep("格式与架构识别", "魔数判定格式（PE/ELF/Mach-O），解析架构、位数、入口点与子系统"),
                FlowStep("静态逆向", "REA/Ghidra：函数清单、字符串、导入表、PE 资源（对话框/图标/版本信息）"),
                FlowStep("动态插桩", "Frida：运行轨迹与内存特征扫描（以静态字符串做运行时交叉验证）"),
                FlowStep("受限执行", "Firecracker/本地替代：沙箱内受限运行并采集行为事实"),
                FlowStep("自动化分析", "PentAGI：发现项汇总与风险评分"),
                FlowStep("证据固化", "Evidence v2：链式哈希证据包 + 离线可校验报告"),
                FlowStep(
                    "功能等价重建",
                    "按静态事实自动选择重建形态（process_manager/installer_wizard/…），生成新 exe 并闭环再验证",
                ),
            ],
            intermediate_artifacts=[
                "runs/<run_id>/run.json（运行清单：阶段状态/引擎/降级说明）",
                "runs/<run_id>/artifacts/（各阶段原始产物）",
                "facts 台账（阶段间事实流转：函数/字符串/导入/资源）",
                "evidence.json / evidence.md（证据包与可读报告）",
            ],
            expected_outputs=[
                "功能等价的新可执行文件（重建产物，如 rebuild/<name>_rebuilt.exe）",
                "Evidence v2 离线可校验证据包（篡改可精确定位）",
                "全阶段逆向报告（含引擎降级与替换说明）",
            ],
            next_action=(
                "POST /api/reverse-engineering/runs {subject: <文件路径>} 启动七阶段流水线："
                "末段「功能等价重建」会自动产出新 exe（也可用 POST /runs/{id}/rebuild 手工重建）"
            ),
        )

    def analyze(self, subject: ClassificationSubject, classification: Classification) -> dict[str, Any]:
        out: dict[str, Any] = {
            "format": classification.evidence.get("format"),
            "arch": classification.evidence.get("arch"),
            "bits": classification.evidence.get("bits"),
            "entrypoint": classification.evidence.get("entrypoint"),
            "subsystem": classification.evidence.get("subsystem"),
        }
        if not subject.path:
            return out
        path = Path(subject.path)
        if subject.full_size > _ANALYZE_MAX_BYTES:
            out["note"] = f"文件 {subject.full_size} 字节超过预分析上限（{_ANALYZE_MAX_BYTES}），跳过深度静态预览"
            return out
        data = _read_capped(path, _ANALYZE_MAX_BYTES)
        if not data:
            return out
        if classification.evidence.get("format") == "pe":
            dialogs = pe_resources.read_dialogs(data)
            version = pe_resources.read_version_info(data)
            imports = pe_resources.read_imports(data)
            strings = pe_resources.read_strings(data, min_len=6, limit=400)
            out["dialogs"] = {
                "count": int(dialogs.get("count") or 0),
                "titles": [str(d.get("title", "")) for d in (dialogs.get("dialogs") or [])[:10]],
            }
            out["version_info"] = {
                k: version[k] for k in ("ProductName", "FileDescription", "FileVersion", "CompanyName") if k in version
            }
            out["imports"] = {"count": len(imports), "dlls": imports[:30]}
            out["installer_signals"] = pe_resources.installer_signals(strings, imports)
            out["overlay_payload"] = pe_resources.detect_payload(data)
            out["rebuild_hint"] = "静态事实齐备后可自动选择重建形态，生成功能等价的新 exe"
        else:
            out["strings_preview"] = pe_resources.read_strings(data, min_len=6, limit=20)
        return out


# ---------------------------------------------------------------------------
# Java class：字节码反编译 → Java 源码/结构表示
# ---------------------------------------------------------------------------


@register_handler
class JavaClassHandler(InputHandler):
    """Java class 处理流程：javap 反编译/反汇编，产出可读的 Java 结构文本。"""

    kind = InputKind.JAVA_CLASS

    def build_plan(self, subject: ClassificationSubject, classification: Classification) -> ProcessingPlan:
        major = classification.evidence.get("major_version")
        version_hint = f"（Java 主版本号 {major}）" if major else ""
        return ProcessingPlan(
            kind=self.kind,
            flow="java_class_decompile",
            goal=f"将 Java class 字节码反编译为可读的 Java 源码/结构表示{version_hint}",
            steps=[
                FlowStep("格式校验", "确认 class 魔数、版本号与常量池结构"),
                FlowStep("javap 反编译", "使用 JDK javap 生成字节码反汇编与类结构"),
                FlowStep("产物落盘", "保存 .java.txt 结构文本作为可下载产物"),
                FlowStep("证据固化", "Evidence v2：哈希链固化 class 与反编译产物"),
            ],
            intermediate_artifacts=[
                "runs/<run_id>/uploads/<name>.class（原始 class 文件）",
                "runs/<run_id>/artifacts/<name>_decompiled.java.txt（javap 反编译文本）",
                "class 元数据（类名/父类/接口/方法清单）",
            ],
            expected_outputs=[
                "反编译后的 Java 结构文本（含类签名、字段、方法、字节码）",
                "Evidence v2 离线可校验证据包",
            ],
            next_action="上传 class 文件后，后端自动识别并仅执行 java_decompile + evidence_v2 阶段",
        )

    def analyze(self, subject: ClassificationSubject, classification: Classification) -> dict[str, Any]:
        return {
            "format": "java_class",
            "major_version": classification.evidence.get("major_version"),
            "note": "完整反编译由流水线 java_decompile 阶段执行；这里仅做轻量格式识别",
        }


# ---------------------------------------------------------------------------
# 源代码：代码理解 / 重构
# ---------------------------------------------------------------------------

_STRUCT_PATTERNS: dict[str, dict[str, str]] = {
    "Python": {
        "functions": r"^\s*(?:async\s+)?def\s+(\w+)",
        "classes": r"^\s*class\s+(\w+)",
        "imports": r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
    },
    "JavaScript": {
        "functions": (
            r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"
            r"|^\s*(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\("
        ),
        "classes": r"^\s*class\s+(\w+)",
        "imports": r"^\s*import\s+.*?\bfrom\s+['\"]([^'\"]+)",
    },
    "Go": {
        "functions": r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)",
        "classes": r"^\s*type\s+(\w+)\s+struct",
        "imports": r"^\s*\"([\w./-]+)\"",
    },
    "Rust": {
        "functions": r"^\s*(?:pub\s+)?fn\s+(\w+)",
        "classes": r"^\s*(?:pub\s+)?(?:struct|enum)\s+(\w+)",
        "imports": r"^\s*use\s+([\w:]+)",
    },
    "C/C++": {
        "functions": r"^\s*[\w:*&<>]+\s+(\w+)\s*\([^;]*\)\s*\{",
        "classes": r"^\s*(?:class|struct)\s+(\w+)",
        "imports": r"^\s*#include\s+[<\"]([^>\"]+)",
    },
}

_GENERIC_STRUCT: dict[str, str] = {
    "functions": r"\b(\w+)\s*\([^;{}]*\)\s*\{",
    "classes": r"\b(?:class|struct|interface)\s+(\w+)",
    "imports": r"^\s*(?:import|require|use|include)\b.*?([\w./-]+)\s*$",
}


def _structure_stats(text: str, language: str) -> dict[str, Any]:
    """语言无关的轻量结构统计（正则可选项；未知语言走通用模式）。"""
    lines = text.split("\n")
    blank = sum(1 for ln in lines if not ln.strip())
    comment = sum(1 for ln in lines if ln.strip().startswith(("#", "//", "/*", "*", "'")))
    patterns = dict(_GENERIC_STRUCT)
    for key, lang_patterns in _STRUCT_PATTERNS.items():
        if lang_patterns and language.startswith(key.split("/")[0]):
            patterns = dict(lang_patterns)
            break

    def _grab(key: str, limit: int) -> list[str]:
        rx = re.compile(patterns[key], re.M)
        found: list[str] = []
        for m in rx.finditer(text):
            value = next((g for g in m.groups() if g), "") if m.groups() else m.group(0)
            if value and value not in found:
                found.append(value)
            if len(found) >= limit:
                break
        return found

    funcs = _grab("functions", 30)
    classes = _grab("classes", 20)
    imports = _grab("imports", 30)
    return {
        "lines_total": len(lines),
        "lines_blank": blank,
        "lines_comment": comment,
        "lines_code": len(lines) - blank - comment,
        "function_count": len(funcs),
        "class_count": len(classes),
        "import_count": len(imports),
        "functions": funcs,
        "classes": classes,
        "imports": imports,
    }


@register_handler
class SourceCodeHandler(InputHandler):
    """源代码处理流程：代码理解 → 结构提取 → 重构/改造。"""

    kind = InputKind.SOURCE_CODE

    def build_plan(self, subject: ClassificationSubject, classification: Classification) -> ProcessingPlan:
        language = str(classification.evidence.get("language") or "Unknown")
        return ProcessingPlan(
            kind=self.kind,
            flow="source_code_understanding_refactor",
            goal=f"理解 {language} 源码的结构与职责，产出行为等价的改造/重构后代码与说明",
            steps=[
                FlowStep("语言与语法识别", f"确认语言（{language}）与版本相关语法特征"),
                FlowStep("结构提取", "解析模块/类/函数/依赖，形成结构化索引"),
                FlowStep("语义摘要", "梳理职责边界与数据流，输出代码理解报告"),
                FlowStep("重构方案", "给出重构/改造计划（含兼容性与风险约束）"),
                FlowStep("改造产出", "生成重构后的源码（保持行为等价，附差异说明）"),
            ],
            intermediate_artifacts=[
                "结构索引（类/函数/依赖清单与计数）",
                "依赖图（模块级 import/引用关系）",
                "代码理解与重构方案报告",
            ],
            expected_outputs=[
                "重构后的源码文件（或补丁 + 差异说明）",
                "代码理解报告（结构/职责/数据流）",
            ],
            next_action="将结构索引作为上下文提交对话或工作流，完成语义摘要与重构产出",
        )

    def analyze(self, subject: ClassificationSubject, classification: Classification) -> dict[str, Any]:
        language = str(classification.evidence.get("language") or "")
        out: dict[str, Any] = {"language": language}
        if not subject.path:
            text = subject.text_sample
        else:
            if subject.full_size > _ANALYZE_MAX_BYTES:
                out["note"] = "源码超过预分析上限，跳过结构提取"
                return out
            raw = _read_capped(Path(subject.path), _ANALYZE_MAX_BYTES)
            text = raw.decode("utf-8", errors="ignore") if raw else ""
        if not text:
            return out
        out["structure"] = _structure_stats(text, language)
        out["header_preview"] = "\n".join(text.split("\n")[:15])
        return out


# ---------------------------------------------------------------------------
# 文本描述：自然语言需求 → 实现
# ---------------------------------------------------------------------------

_ACTION_VERBS: tuple[str, ...] = (
    "实现", "生成", "支持", "解析", "创建", "构建", "优化", "重构", "转换", "提取",
    "分析", "部署", "测试", "修复", "增加", "集成", "接入", "调用", "输出", "校验",
)
_CONSTRAINT_HINTS: tuple[str, ...] = ("必须", "不得", "至少", "最多", "不超过", "需要", "应当", "禁止", "优先", "只能")
_ARTIFACT_HINTS: tuple[str, ...] = (
    "exe", "dll", "脚本", "接口", "api", "数据库", "前端", "后端", "报告", "文档",
    "模型", "测试", "配置", "命令行", "服务", "页面", "爬虫", "工作流",
)


@register_handler
class TextHandler(InputHandler):
    """文本描述处理流程：需求解析 → 规格化 → 实现 → 校验。"""

    kind = InputKind.TEXT

    def build_plan(self, subject: ClassificationSubject, classification: Classification) -> ProcessingPlan:
        return ProcessingPlan(
            kind=self.kind,
            flow="natural_language_requirement_implementation",
            goal="把自然语言需求解析为可执行规格，并落地实现为可用产物（代码/配置/流程）",
            steps=[
                FlowStep("需求解析", "抽取目标、约束、交付物与验收口径（启发式预解析 + 语义理解）"),
                FlowStep("规格化", "形成实现规格：接口/数据/边界条件与验收标准"),
                FlowStep("实现", "生成实现产物（代码模块/工具链配置/工作流定义）"),
                FlowStep("校验", "按验收标准自测（样例驱动），输出验证结果与使用说明"),
            ],
            intermediate_artifacts=[
                "需求结构（目标/约束/交付物/动作清单）",
                "实现规格与待生成文件清单",
                "验证用例与自测结果",
            ],
            expected_outputs=[
                "实现产物（代码/配置/流程定义）",
                "使用说明（如何运行与验证）",
            ],
            next_action="确认需求结构后进入实现阶段（生成代码/配置并执行验证）",
        )

    def analyze(self, subject: ClassificationSubject, classification: Classification) -> dict[str, Any]:
        text = subject.text_sample
        if not text:
            return {"note": "无文本样本"}
        sentences = [s for s in re.split(r"[。！？!?\n]+", text) if s.strip()]
        verbs = [v for v in _ACTION_VERBS if v in text]
        constraints = [s.strip() for s in sentences if any(h in s for h in _CONSTRAINT_HINTS)][:10]
        artifacts = [a for a in _ARTIFACT_HINTS if a in text.lower()]
        return {
            "stats": {
                "chars": len(text),
                "sentences": len(sentences),
                "has_cjk": bool(re.search(r"[\u4e00-\u9fff]", text)),
            },
            "action_verbs": verbs,
            "constraints": constraints,
            "mentioned_artifacts": artifacts,
            "note": "启发式预解析（供后续流程与实现阶段消费）",
        }


# ---------------------------------------------------------------------------
# 兜底：通用二进制分诊
# ---------------------------------------------------------------------------


@register_handler
class BinaryHandler(InputHandler):
    """兜底处理流程：无法识别 → 安全分诊与处置建议。"""

    kind = InputKind.BINARY

    def build_plan(self, subject: ClassificationSubject, classification: Classification) -> ProcessingPlan:
        return ProcessingPlan(
            kind=self.kind,
            flow="generic_binary_triage",
            goal="对无法识别的二进制输入完成安全分诊，给出处置建议与后续路由",
            steps=[
                FlowStep("头部与熵值分析", "检查头部字节、熵值、可打印占比，判断压缩/加密/结构化特征"),
                FlowStep("字符串与特征提取", "提取可打印串与特征常量，寻找格式线索"),
                FlowStep("分类建议", "结合特征给出最可能的格式假设（含置信度）"),
                FlowStep("处置路由", "路由到可执行逆向 / 数据解析 / 人工研判"),
            ],
            intermediate_artifacts=["分诊事实（大小/熵/头部/字符串）", "格式假设列表"],
            expected_outputs=["二进制分诊报告", "后续处置建议（路由到哪个处理流程）"],
            next_action="若确认可执行容器特征，转 executable 流程深挖；否则按数据文件解析或人工研判",
        )

    def analyze(self, subject: ClassificationSubject, classification: Classification) -> dict[str, Any]:
        head = subject.data[:4096]
        printable = sum(1 for b in head if 32 <= b < 127 or b in (9, 10, 13))
        return {
            "size": subject.full_size,
            "entropy": round(_entropy(head), 3),
            "printable_ratio": round(printable / max(1, len(head)), 3),
            "head_hex": subject.data[:32].hex(),
            "strings_preview": pe_resources.read_strings(
                subject.data[: min(len(subject.data), 1 << 20)], min_len=6, limit=15
            ),
            "recommendation": "熵值高且无可打印串 → 可能压缩/加密；出现容器特征 → 转可执行逆向",
        }
