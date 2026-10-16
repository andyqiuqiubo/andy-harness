"""输入分类与处理流程路由 —— 规则 / 优先级 / 兜底 / 扩展性 / REST 端到端验证。

覆盖需求：

- 输入类型识别：可执行文件（格式+架构）、源代码（语言）、文本描述；
- 分类判断逻辑：魔数 > 内容特征 > 扩展名软信号 > 文本 > 兜底，优先级清晰；
- 处理流程定义：每类输入的流程 / 中间产物 / 预期输出；
- 可扩展性：注册新规则 + 新处理器即可支持新类型，核心逻辑零改动。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harness.modules.reverse_engineering.input_dispatch import (
    ClassificationRule,
    InputClassifier,
    InputHandler,
    ProcessingPlan,
    RuleMatch,
    classify_input,
    register_handler,
    register_rule,
    route_input,
    route_text,
    subject_from_path,
    subject_from_text,
    unregister_handler,
    unregister_rule,
)

# backend/tests → parents[2] = src；样本目录 src/rev_eng/samples
_SAMPLES = Path(__file__).resolve().parents[2] / "rev_eng" / "samples"
_PE_SAMPLE = _SAMPLES / "native" / "find.exe"  # Sysinternals find（PE，控制台）
_INSTALLER_SAMPLE = _SAMPLES / "7z2604.exe"  # 7-Zip 安装器（PE + 7z 尾部载荷）

requires_pe = pytest.mark.skipif(not _PE_SAMPLE.is_file(), reason="PE 样本缺失（rev_eng/samples）")
requires_installer = pytest.mark.skipif(not _INSTALLER_SAMPLE.is_file(), reason="安装器样本缺失")

# 确定性的"未知二进制"：0~255 全字节循环，非文本且无已知魔数
_OPAQUE_BYTES = bytes(range(256)) * 8


# ---------------------------------------------------------------------------
# ① 可执行文件：格式与架构识别
# ---------------------------------------------------------------------------


class TestExecutable:
    @requires_pe
    def test_pe_magic_gives_format_and_arch(self) -> None:
        res = route_input(_PE_SAMPLE, analyze=False)
        c = res.classification
        assert c.kind == "executable"
        assert c.matched_rule == "binary_magic"
        assert c.confidence >= 0.9
        assert c.evidence["format"] == "pe"
        assert c.evidence["arch"] in {"x86", "x64", "arm", "arm64"}
        assert c.evidence["bits"] in {32, 64}
        assert str(c.evidence["entrypoint"]).startswith("0x")

    @requires_installer
    def test_installer_sample_full_static_facts(self) -> None:
        """真实安装器：对话框 / 版本 / 7z 载荷 / 安装器特征全部可解析。"""
        res = route_input(_INSTALLER_SAMPLE, analyze=True)
        assert res.classification.kind == "executable"
        assert res.analysis["dialogs"]["count"] >= 1
        payload = res.analysis["overlay_payload"]
        assert payload is not None and payload["type"] == "7z"
        assert res.analysis["installer_signals"], "应识别出安装器能力特征"

    @requires_pe
    def test_unified_entry_classify_input(self) -> None:
        c = classify_input(_PE_SAMPLE)
        assert c.kind == "executable"
        assert c.to_dict()["kind"] == "executable"


def _javac_path() -> Path | None:
    """定位项目内隔离 JDK 的 javac；优先于系统 PATH。"""
    cand = Path(__file__).resolve().parents[2] / ".tools" / "jdk-21" / "bin" / "javac.exe"
    if cand.is_file():
        return cand
    path = shutil.which("javac")
    return Path(path) if path else None


requires_java_class = pytest.mark.skipif(_javac_path() is None, reason="javac 未找到")


# ---------------------------------------------------------------------------
# ② Java class：魔数识别与处理流程
# ---------------------------------------------------------------------------


class TestJavaClass:
    @requires_java_class
    def test_class_magic_and_plan(self, tmp_path: Path) -> None:
        src = tmp_path / "Hello.java"
        src.write_text(
            "public class Hello {\n"
            "    public String greet(String name) { return \"Hello, \" + name; }\n"
            "}\n",
            encoding="utf-8",
        )
        subprocess.run([str(_javac_path()), str(src)], check=True, cwd=tmp_path)
        cls = tmp_path / "Hello.class"
        assert cls.is_file()

        res = route_input(cls, analyze=False)
        c = res.classification
        assert c.kind == "java_class"
        assert c.matched_rule == "java_class"
        assert c.evidence["format"] == "java_class"
        assert c.evidence["major_version"] >= 45
        assert res.plan.flow == "java_class_decompile"
        assert res.plan.expected_outputs


# ---------------------------------------------------------------------------
# ③ 源代码：语言识别 + 结构提取
# ---------------------------------------------------------------------------


class TestSourceCode:
    def test_python_extension_and_structure(self, tmp_path: Path) -> None:
        f = tmp_path / "demo.py"
        f.write_text(
            "import os\n\n\nclass Box:\n    pass\n\n\ndef main() -> str:\n    return os.getcwd()\n",
            encoding="utf-8",
        )
        res = route_input(f)
        c = res.classification
        assert c.kind == "source_code"
        assert c.evidence["language"] == "Python"
        assert c.matched_rule == "source_code"
        structure = res.analysis["structure"]
        assert structure["function_count"] >= 1
        assert structure["class_count"] >= 1
        assert "os" in structure["imports"]

    def test_shebang_without_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "run_me"
        f.write_text("#!/usr/bin/env python3\nprint('hi')\n", encoding="utf-8")
        c = classify_input(f)
        assert c.kind == "source_code"
        assert c.evidence["language"] == "Python"
        assert c.evidence["detected_by"] == "shebang"

    def test_text_submission_with_filename_hint(self) -> None:
        """内存文本 + 文件名提示（.go）也能走源码分支。"""
        res = route_text(
            "package main\n\nfunc main() {\n}\n",
            filename="main.go",
        )
        assert res.classification.kind == "source_code"
        assert res.classification.evidence["language"] == "Go"


# ---------------------------------------------------------------------------
# ③ 文本描述：自然语言需求
# ---------------------------------------------------------------------------


class TestText:
    def test_chinese_requirement(self) -> None:
        res = route_text("实现一个批量图片压缩工具，必须支持 PNG 与 JPG，输出统计报告。")
        c = res.classification
        assert c.kind == "text"
        assert c.evidence["has_cjk"] is True
        analysis = res.analysis
        assert "实现" in analysis["action_verbs"]
        assert analysis["constraints"], "应抽出含约束词的需求句"

    def test_plain_english_text(self, tmp_path: Path) -> None:
        f = tmp_path / "req.txt"
        f.write_text("Please build a small CLI tool that renames files in batches.\n", encoding="utf-8")
        assert classify_input(f).kind == "text"


# ---------------------------------------------------------------------------
# ④ 兜底：无法识别的输入
# ---------------------------------------------------------------------------


class TestFallback:
    def test_opaque_binary_falls_back(self, tmp_path: Path) -> None:
        f = tmp_path / "unknown.bin"
        f.write_bytes(_OPAQUE_BYTES)
        res = route_input(f)
        c = res.classification
        assert c.kind == "binary"
        assert c.fallback is True
        assert c.matched_rule == "binary_fallback"
        assert res.plan.flow == "generic_binary_triage"
        assert res.analysis["entropy"] > 7.0  # 近乎均匀分布

    def test_empty_input_falls_back_with_reason(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        c = classify_input(f)
        assert c.kind == "binary" and c.fallback is True
        assert "空" in c.reasons[0]


# ---------------------------------------------------------------------------
# ⑤ 优先级：魔数 > 内容 > 扩展名软信号 > 文本 > 兜底
# ---------------------------------------------------------------------------


class TestPriority:
    @requires_pe
    def test_magic_beats_misleading_extension(self, tmp_path: Path) -> None:
        """扩展名为 .txt 但内容是真实 PE → 仍判可执行（魔数最高权威）。"""
        f = tmp_path / "note.txt"
        f.write_bytes(_PE_SAMPLE.read_bytes())
        c = classify_input(f)
        assert c.kind == "executable"
        assert c.matched_rule == "binary_magic"

    def test_text_content_named_exe_is_not_executable(self, tmp_path: Path) -> None:
        """纯文本却命名为 .exe → 不据扩展名判可执行（防误判护栏）。"""
        f = tmp_path / "readme.exe"
        f.write_text("This is a plain text note, not a real executable.\n", encoding="utf-8")
        c = classify_input(f)
        assert c.kind == "text"
        assert c.evidence["extension"] == ".exe"

    def test_binary_with_exe_extension_uses_soft_signal(self, tmp_path: Path) -> None:
        """二进制内容 + .dll 扩展名（无魔数）→ 软信号判可执行，置信度较低。"""
        f = tmp_path / "blob.dll"
        f.write_bytes(_OPAQUE_BYTES)
        c = classify_input(f)
        assert c.kind == "executable"
        assert c.matched_rule == "executable_extension"
        assert c.confidence < 0.9

    def test_priority_ordering_of_rules(self) -> None:
        """注册表默认顺序即判定顺序：100 > 80 > 60 > 20 > 5。"""
        priorities = [r.priority for r in InputClassifier()._rules]
        assert priorities == sorted(priorities, reverse=True)
        by_name = {r.name: r.priority for r in InputClassifier()._rules}
        assert by_name["binary_magic"] > by_name["source_code"] > by_name["executable_extension"]
        assert by_name["executable_extension"] > by_name["text"] > by_name["binary_fallback"]


# ---------------------------------------------------------------------------
# ⑥ 处理流程定义：每类输入的流程 / 中间产物 / 预期输出
# ---------------------------------------------------------------------------


class TestPlans:
    @pytest.mark.parametrize(
        ("subject", "kind", "flow"),
        [
            (
                subject_from_text("import os\nprint(os.getcwd())\n", "a.py"),
                "source_code",
                "source_code_understanding_refactor",
            ),
            (
                subject_from_text("写一个爬虫，抓取网页标题"),
                "text",
                "natural_language_requirement_implementation",
            ),
            (subject_from_text("", ""), "binary", "generic_binary_triage"),  # 空输入走兜底
        ],
    )
    def test_plan_structure_complete(self, subject: Any, kind: str, flow: str) -> None:
        res = InputClassifier().route(subject, analyze=False)
        assert res.classification.kind == kind
        assert res.plan.flow == flow
        assert res.plan.steps, "流程必须有步骤"
        assert res.plan.intermediate_artifacts, "必须声明中间产物"
        assert res.plan.expected_outputs, "必须声明预期输出"
        assert res.plan.goal

    @requires_pe
    def test_executable_plan_structure_complete(self) -> None:
        res = route_input(_PE_SAMPLE, analyze=False)
        assert res.classification.kind == "executable"
        assert res.plan.flow == "executable_reverse_engineering"
        assert res.plan.steps and res.plan.intermediate_artifacts and res.plan.expected_outputs

    @requires_pe
    def test_executable_goal_is_functional_equivalent_rebuild(self) -> None:
        """可执行文件的最终目标：生成功能等价的新 exe。"""
        res = route_input(_PE_SAMPLE, analyze=False)
        assert "等价" in res.plan.goal
        text = res.plan.goal + " ".join(res.plan.expected_outputs) + res.plan.next_action
        assert "exe" in text.lower()

    @requires_pe
    def test_result_is_json_serializable(self) -> None:
        res = route_input(_PE_SAMPLE, analyze=False)
        dumped = json.dumps(res.to_dict(), ensure_ascii=False)
        assert json.loads(dumped)["classification"]["kind"] == "executable"


# ---------------------------------------------------------------------------
# ⑦ 可扩展性：新类型 = 新规则 + 新处理器（核心零改动）
# ---------------------------------------------------------------------------


class TestExtensibility:
    def test_register_new_kind_end_to_end(self, tmp_path: Path) -> None:
        """新增 "archive" 类型：注册规则 + 处理器即可全链路生效。"""

        @register_rule
        class ArchiveMagicRule(ClassificationRule):
            name = "_test_archive_magic"
            priority = 95  # 低于可执行魔数(100)、高于源码(80)

            def match(self, subject: Any) -> RuleMatch | None:
                if subject.data[:4] == b"PK\x03\x04":
                    return RuleMatch(
                        kind="_test_archive",
                        confidence=0.95,
                        reason="ZIP 魔数命中",
                        evidence={"format": "zip"},
                    )
                return None

        @register_handler
        class ArchiveHandler(InputHandler):
            kind = "_test_archive"

            def build_plan(self, subject: Any, classification: Any) -> ProcessingPlan:
                return ProcessingPlan(
                    kind=self.kind,
                    flow="_test_archive_extract",
                    goal="解包并清点归档内容",
                    expected_outputs=["解包后的文件清单"],
                )

        try:
            f = tmp_path / "a.zip"
            f.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
            res = route_input(f, analyze=False)
            assert res.classification.kind == "_test_archive"
            assert res.classification.matched_rule == "_test_archive_magic"
            assert res.plan.flow == "_test_archive_extract"
        finally:
            assert unregister_rule("_test_archive_magic") is True
            assert unregister_handler("_test_archive") is True

    def test_instance_level_injection(self, tmp_path: Path) -> None:
        """实例注入：不改全局注册表也能定制规则集/处理器集。"""

        class AlwaysExecRule(ClassificationRule):
            name = "always_exec"
            priority = 1000

            def match(self, subject: Any) -> RuleMatch | None:
                return RuleMatch(kind="executable", confidence=0.5, reason="test")

        f = tmp_path / "x.unknown"
        f.write_bytes(_OPAQUE_BYTES)
        injected = InputClassifier(rules=[AlwaysExecRule()])
        assert injected.classify(subject_from_path(f)).matched_rule == "always_exec"
        # 默认分类器不受影响
        assert classify_input(f).kind == "binary"

    def test_unregistered_kind_returns_explicit_plan(self, tmp_path: Path) -> None:
        """只有规则没有处理器：返回显式降级计划而非抛错。"""

        @register_rule
        class OrphanRule(ClassificationRule):
            name = "_test_orphan"
            priority = 999

            def match(self, subject: Any) -> RuleMatch | None:
                return RuleMatch(kind="_test_orphan_kind", confidence=0.9, reason="orphan")

        try:
            f = tmp_path / "x.bin"
            f.write_bytes(b"x")
            res = route_input(f, analyze=False)
            assert res.classification.kind == "_test_orphan_kind"
            assert res.plan.flow == "unregistered"
            assert "register_handler" in res.plan.next_action
        finally:
            assert unregister_rule("_test_orphan") is True


# ---------------------------------------------------------------------------
# ⑧ REST 端到端
# ---------------------------------------------------------------------------


class TestClassifyRest:
    @pytest.fixture
    def client(self) -> Any:
        from harness.main import app

        with TestClient(app) as c:
            yield c

    def test_classify_text(self, client: TestClient) -> None:
        r = client.post("/api/reverse-engineering/classify", json={"text": "把 exe 逆向成等价程序"})
        assert r.status_code == 200
        body = r.json()
        assert body["classification"]["kind"] == "text"
        assert body["plan"]["flow"] == "natural_language_requirement_implementation"

    @requires_pe
    def test_classify_file(self, client: TestClient) -> None:
        r = client.post("/api/reverse-engineering/classify", json={"subject": str(_PE_SAMPLE), "analyze": False})
        assert r.status_code == 200
        body = r.json()
        assert body["classification"]["kind"] == "executable"
        assert body["classification"]["evidence"]["format"] == "pe"

    def test_classify_missing_file_404(self, client: TestClient, tmp_path: Path) -> None:
        r = client.post("/api/reverse-engineering/classify", json={"subject": str(tmp_path / "nope.bin")})
        assert r.status_code == 404
        assert r.json()["code"] == "REV_ENG_SUBJECT_NOT_FOUND"

    def test_classify_empty_request_400(self, client: TestClient) -> None:
        r = client.post("/api/reverse-engineering/classify", json={})
        assert r.status_code == 400
        assert r.json()["code"] == "REV_ENG_INVALID_CONFIG"
