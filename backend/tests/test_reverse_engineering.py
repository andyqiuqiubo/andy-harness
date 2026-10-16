"""工程强逆向流水线 —— 全流程验证测试。

覆盖历史质量事故的回归（错误串被当成功 / 0 结果假 OK / 超时无降级）、
新增防护（可重试重试、阶段异常隔离、run_id 穿越校验、索引容错）、
启发式精确率修正（IP 版本串 / 注册表单反斜杠 / API 词边界分级）与
沙箱替代层的事实性（网络标注、超时树杀、本地执行开关）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harness.modules.mcp_client import service as mcp_service_module
from harness.modules.reverse_engineering import pipeline as pipeline_module
from harness.modules.reverse_engineering import stages as stages_module
from harness.modules.reverse_engineering.models import (
    STAGE_EVIDENCE,
    RunContext,
    StageResult,
    StageStatus,
)
from harness.modules.reverse_engineering.pipeline import ReverseEngineeringPipeline
from harness.modules.reverse_engineering.stages import (
    BaseStage,
    FirecrackerSandboxStage,
    FridaDynamicStage,
    PentAGIAnalysisStage,
    ReaStaticStage,
    _extract_ips,
    _injection_hint,
    _match_apis,
    _mcp_error,
)

MCP_ERR = stages_module.MCP_ERR_PREFIX


def _javac_for_test() -> Path | None:
    """定位项目内隔离 JDK 的 javac。"""
    cand = Path(__file__).resolve().parents[2] / ".tools" / "jdk-21" / "bin" / "javac.exe"
    return cand if cand.is_file() else None


requires_java_class = pytest.mark.skipif(_javac_for_test() is None, reason="javac 未找到")


def _err(code: str, *, retryable: bool = False, message: str = "boom") -> str:
    """构造一个 MCP 工具错误串（与 mcp_client/service.py 的格式一致）。"""
    return f'{MCP_ERR}: {{"error":{{"code":"{code}","retryable":{str(retryable).lower()},"message":"{message}"}}}}'


def _procs_ok() -> str:
    return json.dumps(
        {
            "normalized_result": [
                {"address": "0x401000", "value": "main", "procedure": {"external": False, "thunk": False}},
                {
                    "address": "EXTERNAL:0x1",
                    "value": "SHLWAPI.DLL::ColorRGBToHLS",
                    "procedure": {"external": True, "thunk": False},
                },
            ],
        }
    )


def _strings_ok() -> str:
    return json.dumps(
        {
            "normalized_result": [
                {"value": "hello-world-string"},
                {"value": "http://evil.example/gate"},
            ],
        }
    )


class FakeMCPService:
    """可编排响应的 MCP 服务替身（记录调用）。

    responses 的值可以是：str（固定返回）、Exception（抛出）、
    list（按调用次序弹出，最后一项重复返回）、callable(args)->str。
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def connect_server(self, cfg: Any) -> list[Any]:
        return []

    async def disconnect_all(self) -> None:
        return None

    async def call_tool(self, server: str, tool: str, args: dict[str, Any]) -> str:
        self.calls.append((tool, dict(args)))
        resp = self.responses.get(tool, "")
        if isinstance(resp, list):
            return str(resp.pop(0)) if len(resp) > 1 else str(resp[0])
        if callable(resp):
            return str(resp(args))
        if isinstance(resp, Exception):
            raise resp
        return str(resp)

    def count(self, tool: str) -> int:
        return sum(1 for t, _ in self.calls if t == tool)


def _ctx(
    tmp_path: Path,
    facts: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> RunContext:
    subject = tmp_path / "sample.bin"
    if not subject.exists():
        subject.write_bytes(
            b"MZ\x90\x00 HELLO-ROOT-KIT-VISIBLE-STRING "
            b"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run "
            b"203.0.113.10 5.1.0.0"
        )
    work = tmp_path / "run"
    work.mkdir(exist_ok=True)
    return RunContext(
        run_id="testrun1",
        subject=subject,
        work_dir=work,
        facts=dict(facts or {}),
        config=dict(config or {}),
    )


def _patch_stage(monkeypatch: pytest.MonkeyPatch, stage_cls: type, responses: dict[str, Any]) -> FakeMCPService:
    fake = FakeMCPService(responses)
    monkeypatch.setattr(mcp_service_module, "MCPClientService", lambda **kw: fake)
    monkeypatch.setattr(stage_cls, "check", lambda self: (True, ""))
    return fake


# ── MCP 错误识别与重试（根因防护层） ──────────────────────────────────────
class TestMcpErrorRecognition:
    def test_normal_json_is_not_error(self) -> None:
        assert _mcp_error('{"result":{"ok":true}}') is None

    def test_error_string_is_parsed(self) -> None:
        err = _mcp_error(_err("provider_timeout", retryable=True, message="Analysis took too long"))
        assert err is not None
        assert err["code"] == "provider_timeout"
        assert err["retryable"] is True

    def test_non_json_payload(self) -> None:
        err = _mcp_error(f"{MCP_ERR}: not-a-json")
        assert err is not None
        assert err["code"] == "tool_error"

    def test_none_and_empty(self) -> None:
        assert _mcp_error(None) is None
        assert _mcp_error("") is None


class TestCallRetry:
    async def test_retryable_error_is_retried_then_succeeds(self) -> None:
        stage = ReaStaticStage()
        fake = FakeMCPService({"tool": [_err("provider_timeout", retryable=True), "ok-result"]})
        out = await stage._call_retry(fake, "srv", "tool", {}, attempts=2, delay=0)
        assert out == "ok-result"
        assert fake.count("tool") == 2

    async def test_non_retryable_error_returns_immediately(self) -> None:
        stage = ReaStaticStage()
        fake = FakeMCPService({"tool": _err("invalid_request")})
        out = await stage._call_retry(fake, "srv", "tool", {}, attempts=3, delay=0)
        assert _mcp_error(out) is not None
        assert fake.count("tool") == 1

    async def test_exception_retried_then_raised(self) -> None:
        stage = ReaStaticStage()
        fake = FakeMCPService({"tool": RuntimeError("MCP request timeout")})
        with pytest.raises(RuntimeError):
            await stage._call_retry(fake, "srv", "tool", {}, attempts=2, delay=0)
        assert fake.count("tool") == 2

    async def test_last_retryable_error_is_returned(self) -> None:
        stage = ReaStaticStage()
        fake = FakeMCPService({"tool": _err("provider_timeout", retryable=True)})
        out = await stage._call_retry(fake, "srv", "tool", {}, attempts=2, delay=0)
        err = _mcp_error(out)
        assert err is not None and err.get("code") == "provider_timeout"
        assert fake.count("tool") == 2


# ── REA 静态阶段：错误检测 / 降级 / 空结果 / 部分成功保留 ────────────────
class TestReaStaticStage:
    async def test_success_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_stage(
            monkeypatch,
            ReaStaticStage,
            {
                "open_binary": json.dumps({"result": {"path": "x", "format": "pe"}}),
                "list_procedures": _procs_ok(),
                "list_strings": _strings_ok(),
                "procedure_pseudo_code": "int main(void){return 0;}",
            },
        )
        ctx = _ctx(tmp_path, config={"rea_attempts": 1, "rea_retry_delay": 0})
        res = await ReaStaticStage().run(ctx)
        assert res.status == StageStatus.OK
        assert res.facts["function_count"] == 2
        assert res.facts["string_count"] == 2
        assert res.facts["decompiled"] == ["main"]
        assert res.facts["deep_analysis"]["available"] is True
        assert "2 个函数" in res.summary

    async def test_provider_timeout_must_not_be_reported_as_ok(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """历史事故回归：list_procedures/list_strings 返回 provider_timeout 错误串时，
        旧实现解析成空列表并报 'OK：0 个函数、0 条字符串'。"""
        _patch_stage(
            monkeypatch,
            ReaStaticStage,
            {
                "open_binary": json.dumps({"result": {"path": "x", "format": "pe"}}),
                "list_procedures": _err("provider_timeout", message="Analysis took too long"),
                "list_strings": _err("provider_timeout", message="Analysis took too long"),
                "inspect_pe_resources": json.dumps({"resources": []}),
            },
        )
        ctx = _ctx(tmp_path, config={"rea_attempts": 1, "rea_retry_delay": 0})
        res = await ReaStaticStage().run(ctx)
        assert res.status == StageStatus.SUBSTITUTED
        assert res.engine == "rea(static-fallback)"
        assert res.facts["deep_analysis"]["code"] == "provider_timeout"
        # 降级路径用本地提取兜底，不再出现"0 字符串"的假成功
        assert res.facts["string_count"] > 0
        assert str(res.facts["strings_source"]).startswith("local-extract")
        assert "provider_timeout" in res.summary

    async def test_retryable_timeout_recovers_on_second_attempt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_stage(
            monkeypatch,
            ReaStaticStage,
            {
                "open_binary": json.dumps({"result": {"path": "x", "format": "pe"}}),
                "list_procedures": [_err("provider_timeout", retryable=True), _procs_ok()],
                "list_strings": [_err("provider_timeout", retryable=True), _strings_ok()],
                "procedure_pseudo_code": "int main(void){return 1;}",
            },
        )
        ctx = _ctx(tmp_path, config={"rea_attempts": 2, "rea_retry_delay": 0})
        res = await ReaStaticStage().run(ctx)
        assert res.status == StageStatus.OK
        assert res.facts["function_count"] == 2
        assert any("重试" in x for x in res.logs)

    async def test_empty_deep_results_degrade(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_stage(
            monkeypatch,
            ReaStaticStage,
            {
                "open_binary": json.dumps({"result": {"path": "x"}}),
                "list_procedures": json.dumps({"normalized_result": []}),
                "list_strings": json.dumps({"normalized_result": []}),
                "inspect_pe_resources": json.dumps({"resources": []}),
            },
        )
        ctx = _ctx(tmp_path, config={"rea_attempts": 1, "rea_retry_delay": 0})
        res = await ReaStaticStage().run(ctx)
        assert res.status == StageStatus.SUBSTITUTED
        assert res.facts["deep_analysis"]["code"] == "empty_result"
        assert res.facts["string_count"] > 0

    async def test_open_binary_exception_degrades_not_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """历史事故回归：open_binary 客户端超时曾把整阶段打成 FAILED。"""
        _patch_stage(
            monkeypatch,
            ReaStaticStage,
            {
                "open_binary": RuntimeError("MCP request timeout: tools/call server=rea-x"),
                "inspect_pe_resources": json.dumps({"resources": []}),
            },
        )
        ctx = _ctx(tmp_path, config={"rea_attempts": 1, "rea_retry_delay": 0})
        res = await ReaStaticStage().run(ctx)
        assert res.status == StageStatus.SUBSTITUTED
        assert res.facts["deep_analysis"]["code"] == "request_failed"
        assert res.facts["string_count"] > 0

    async def test_pseudo_code_error_is_not_stored_as_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_stage(
            monkeypatch,
            ReaStaticStage,
            {
                "open_binary": json.dumps({"result": {"path": "x"}}),
                "list_procedures": _procs_ok(),
                "list_strings": _strings_ok(),
                "procedure_pseudo_code": _err("provider_timeout"),
            },
        )
        ctx = _ctx(tmp_path, config={"rea_attempts": 1, "rea_retry_delay": 0})
        res = await ReaStaticStage().run(ctx)
        assert res.status == StageStatus.OK
        assert res.facts["decompiled"] == []
        assert "rea-pseudo-code.json" not in [a.name for a in res.artifacts]

    async def test_partial_success_keeps_functions(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """procs 成功 + strings 失败：降级时必须保留已拿到的函数清单。"""
        _patch_stage(
            monkeypatch,
            ReaStaticStage,
            {
                "open_binary": json.dumps({"result": {"path": "x"}}),
                "list_procedures": _procs_ok(),
                "list_strings": _err("provider_timeout"),
                "inspect_pe_resources": json.dumps({"resources": []}),
            },
        )
        ctx = _ctx(tmp_path, config={"rea_attempts": 1, "rea_retry_delay": 0})
        res = await ReaStaticStage().run(ctx)
        assert res.status == StageStatus.SUBSTITUTED
        assert res.facts["function_count"] == 2
        assert res.facts["string_count"] > 0
        assert "保留深度函数 2 个" in res.summary


class TestExtractNamesRegression:
    def test_subject_and_provider_name_not_mistaken_for_functions(self) -> None:
        """历史回归（run 19b438299a28）：旧实现把 subject.name / provider.name
        当成函数名，893KB 的产物只解析出 2 个"函数"。"""
        raw = json.dumps(
            {
                "subject": {"name": "C:\\samples\\procexp64.exe"},
                "provider": {"name": "Ghidra"},
                "normalized_result": [
                    {"address": "0x401000", "value": "main"},
                    {"address": "0x402000", "value": "WinMain"},
                ],
            }
        )
        assert stages_module._extract_names(raw) == ["main", "WinMain"]


# ── Frida 动态阶段：注入失败诊断 ─────────────────────────────────────────
class TestFridaStage:
    async def test_injection_failure_maps_readable_hint(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        spawn_err = (
            f'{MCP_ERR}: {{"error":{{"code":-32000,'
            '"message":"unable to spawn executable at \'x.exe\': 0x000002e4"}}}}'
        )
        _patch_stage(
            monkeypatch,
            FridaDynamicStage,
            {
                "frida_status": json.dumps({"frida_version": "17.23.3", "available": True}),
                "frida_run_and_trace": spawn_err,
            },
        )
        ctx = _ctx(tmp_path, facts={"strings": ["kernel32.dll", "CreateFileW"]})
        res = await FridaDynamicStage().run(ctx)
        assert res.status == StageStatus.SUBSTITUTED
        assert res.engine == "frida-static-inference"
        assert res.facts["injection"]["hint"].startswith("样本需要提升权限")
        assert isinstance(res.facts["inferred_apis"], list)

    def test_hint_mapping(self) -> None:
        assert "提升权限" in _injection_hint("... 0x000002e4")
        assert "frida-agent" in _injection_hint("refused to load frida-agent, or terminated")
        assert "注入期间退出" in _injection_hint("terminated during injection")
        assert _injection_hint("something else") == ""


# ── Firecracker 阶段：本地替代执行的事实性与边界 ─────────────────────────
class TestFirecrackerLocalSubstitute:
    async def test_local_exec_records_honest_network_label(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ANDY_REVENG_FORCE_FIRECRACKER", "0")
        ctx = _ctx(
            tmp_path,
            config={
                "sandbox_args": ["-c", "print('sandbox-ok')"],
                "sandbox_timeout": 15,
            },
        )
        ctx.subject = Path(sys.executable)  # 用解释器当"样本"，真实小执行
        res = await FirecrackerSandboxStage().run(ctx)
        assert res.status == StageStatus.SUBSTITUTED
        assert res.facts["exit_code"] == 0
        assert "sandbox-ok" in res.facts["stdout"]
        art = next(a for a in res.artifacts if a.name == "sandbox-execution.json")
        data = json.loads(Path(art.path).read_text(encoding="utf-8"))
        # 旧版谎称 network blocked —— 本地替代后端并不隔离网络
        assert "not-isolated" in data["network"]

    async def test_timeout_kills_process_tree(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("ANDY_REVENG_FORCE_FIRECRACKER", "0")
        ctx = _ctx(
            tmp_path,
            config={
                "sandbox_args": ["-c", "import time; time.sleep(30)"],
                "sandbox_timeout": 0.8,
            },
        )
        ctx.subject = Path(sys.executable)
        res = await FirecrackerSandboxStage().run(ctx)
        assert res.status == StageStatus.SUBSTITUTED
        assert res.facts.get("timeout") is True
        assert res.facts.get("exit_code") is None
        assert "进程树" in res.summary
        art = next(a for a in res.artifacts if a.name == "sandbox-execution.json")
        data = json.loads(Path(art.path).read_text(encoding="utf-8"))
        assert data["timed_out"] is True
        assert data["process_tree_kill"]

    async def test_local_exec_disabled_by_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("ANDY_REVENG_FORCE_FIRECRACKER", "0")
        monkeypatch.setenv("ANDY_REVENG_LOCAL_EXEC", "0")
        ctx = _ctx(tmp_path, config={"sandbox_args": ["-c", "print('never')"]})
        ctx.subject = Path(sys.executable)
        res = await FirecrackerSandboxStage().run(ctx)
        assert res.status == StageStatus.SKIPPED
        assert "sandbox-execution.json" not in [a.name for a in res.artifacts]
        # 沙箱配置契约产物仍然保留
        assert "firecracker-machine-config.json" in [a.name for a in res.artifacts]


# ── PentAGI 本地分析器：启发式精确率/召回率 ───────────────────────────────
def _analyze_with(facts: dict[str, Any]) -> list[dict[str, Any]]:
    ctx = RunContext(run_id="t", subject=Path("x"), work_dir=Path("."), facts=dict(facts))
    return PentAGIAnalysisStage._analyze(ctx)


def _ids(findings: list[dict[str, Any]]) -> set[str]:
    return {f["id"] for f in findings}


class TestIndicatorExtraction:
    def test_version_string_is_not_reported_as_ip(self) -> None:
        """历史事故回归：5.1.0.0（Windows 版本串）曾被 NET-001 当成 IP 上报。"""
        assert _extract_ips(["Windows NT 5.1.0.0 build"]) == []

    def test_real_ip_is_reported(self) -> None:
        assert _extract_ips(["C2 at 203.0.113.10 found"]) == ["203.0.113.10"]

    def test_invalid_octets_and_leading_zeros_rejected(self) -> None:
        assert _extract_ips(["999.1.1.1", "192.168.001.001", "10.0.19045.1234"]) == []

    def test_net_finding_uses_validated_ips(self) -> None:
        findings = _analyze_with({"strings": ["5.1.0.0", "203.0.113.10"]})
        net = next(f for f in findings if f["id"] == "NET-001")
        assert net["evidence"]["ips"] == ["203.0.113.10"]

    def test_registry_single_backslash_is_detected(self) -> None:
        """历史缺陷回归：旧正则要求两个反斜杠，PER-001 从未命中过。"""
        findings = _analyze_with({"strings": ["SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run"]})
        assert "PER-001" in _ids(findings)


class TestApiMatching:
    def test_substring_does_not_match_prefixed_api(self) -> None:
        """NtOpenProcess 不应被当成 OpenProcess（词边界）。"""
        assert "OpenProcess" not in _match_apis("NtOpenProcess and ZwOpenProcessX", {"OpenProcess"})

    def test_word_boundary_match(self) -> None:
        assert "OpenProcess" in _match_apis("call OpenProcess now", {"OpenProcess"})

    def test_low_signal_api_only_info(self) -> None:
        findings = _analyze_with({"strings": ["LoadLibraryA", "GetProcAddress"]})
        ids = _ids(findings)
        assert "API-003" in ids
        assert "API-001" not in ids
        assert "API-002" not in ids

    def test_injection_api_is_high(self) -> None:
        findings = _analyze_with({"strings": ["CreateRemoteThread"]})
        hit = next(f for f in findings if f["id"] == "API-001")
        assert hit["severity"] == "high"

    def test_other_suspicious_api_is_medium(self) -> None:
        findings = _analyze_with({"functions": ["NtOpenProcess"], "strings": ["URLDownloadToFileA"]})
        hit = next(f for f in findings if f["id"] == "API-002")
        assert hit["severity"] == "medium"
        # OpenProcess 因词边界不应出现在任何 API 命中里
        assert "OpenProcess" not in json.dumps(findings, ensure_ascii=False)

    def test_crypto_marker_word_boundary(self) -> None:
        # "aesthetic" 含子串 "aes"，词边界匹配必须把它排除
        assert "CRY-001" not in _ids(_analyze_with({"strings": ["aesthetic-design"]}))
        assert "CRY-001" in _ids(_analyze_with({"strings": ["use AES-256 here"]}))


# ── 编排器：阶段隔离 / 三态状态 / 校验 / 索引容错 ─────────────────────────
class _FakeStage(BaseStage):
    """可编排的测试阶段。"""

    def __init__(self, name: str, kind: str = "ok") -> None:
        self.name = name
        self.target_engine = "fake"
        self.kind = kind
        self.ran = False

    def check(self) -> tuple[bool, str]:
        return True, ""

    async def run(self, ctx: RunContext) -> StageResult:
        self.ran = True
        if self.kind == "crash":
            raise RuntimeError("boom-in-stage")
        status = {"ok": StageStatus.OK, "sub": StageStatus.SUBSTITUTED}[self.kind]
        return StageResult(
            stage=self.name,
            status=status,
            engine="fake",
            target_engine=self.target_engine,
            summary=self.kind,
            facts={"k": self.name},
        )


def _fake_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kinds: dict[str, str]
) -> tuple[ReverseEngineeringPipeline, dict[str, _FakeStage]]:
    stages = {name: _FakeStage(name, kind) for name, kind in kinds.items()}
    monkeypatch.setattr(pipeline_module, "build_stages", lambda: list(stages.values()))
    pipe = ReverseEngineeringPipeline(runs_dir=tmp_path / "runs")
    return pipe, stages


class TestPipelineOrchestration:
    async def test_all_ok(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        pipe, _ = _fake_pipeline(monkeypatch, tmp_path, {"rea_static": "ok", STAGE_EVIDENCE: "ok"})
        subject = tmp_path / "s.bin"
        subject.write_bytes(b"MZ-fake")
        rec = await pipe.run(subject)
        assert rec.status == "ok"
        assert (tmp_path / "runs" / rec.run_id / "run.json").is_file()

    async def test_degraded_status_for_substituted(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        pipe, _ = _fake_pipeline(monkeypatch, tmp_path, {"rea_static": "sub", STAGE_EVIDENCE: "ok"})
        subject = tmp_path / "s.bin"
        subject.write_bytes(b"MZ-fake")
        rec = await pipe.run(subject)
        assert rec.status == "degraded"

    async def test_stage_crash_is_isolated_and_run_persists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """阶段崩溃必须转成 FAILED 并继续执行后续阶段，run.json 必须落盘。"""
        pipe, stages = _fake_pipeline(
            monkeypatch,
            tmp_path,
            {
                "rea_static": "crash",
                "frida_dynamic": "ok",
                STAGE_EVIDENCE: "ok",
            },
        )
        subject = tmp_path / "s.bin"
        subject.write_bytes(b"MZ-fake")
        rec = await pipe.run(subject)
        assert rec.status == "failed"
        assert "boom-in-stage" in rec.stages["rea_static"].error
        # 后续阶段没有被崩溃阻断
        assert stages["frida_dynamic"].ran is True
        assert stages[STAGE_EVIDENCE].ran is True
        data = json.loads((tmp_path / "runs" / rec.run_id / "run.json").read_text(encoding="utf-8"))
        assert data["status"] == "failed"

    async def test_invalid_run_id_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        pipe, _ = _fake_pipeline(monkeypatch, tmp_path, {"rea_static": "ok", STAGE_EVIDENCE: "ok"})
        subject = tmp_path / "s.bin"
        subject.write_bytes(b"MZ")
        for bad in ("..", "a/b", "abc", "../../etc/passwd"[:6].replace("/", "_")):
            with pytest.raises(ValueError):
                await pipe.run(subject, {"run_id": bad})

    async def test_unknown_stage_name_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        pipe, _ = _fake_pipeline(monkeypatch, tmp_path, {"rea_static": "ok"})
        subject = tmp_path / "s.bin"
        subject.write_bytes(b"MZ")
        with pytest.raises(ValueError):
            await pipe.run(subject, {"stages": ["not-a-stage"]})

    def test_get_run_rejects_traversal(self, tmp_path: Path) -> None:
        pipe = ReverseEngineeringPipeline(runs_dir=tmp_path / "runs")
        assert pipe.get_run("..") is None
        assert pipe.get_run("bad..id") is None
        assert pipe.get_run("nonexistent1") is None

    def test_corrupt_index_does_not_crash_list(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        (runs / "index.json").write_text("{not-json", encoding="utf-8")
        pipe = ReverseEngineeringPipeline(runs_dir=runs)
        assert pipe.list_runs() == []


# ── Evidence v2 端到端：真实证据包 + 离线校验 + 索引可见 ──────────────────
class TestEvidenceEndToEnd:
    async def test_evidence_only_run_produces_verified_bundle(self, tmp_path: Path) -> None:
        pipe = ReverseEngineeringPipeline(runs_dir=tmp_path / "runs")
        subject = tmp_path / "subject.bin"
        subject.write_bytes(b"MZ\x90\x00 EVIDENCE-SUBJECT-BYTES")
        rec = await pipe.run(subject, {"stages": ["evidence_v2"], "run_id": "evidtest1"})
        # 其余阶段被跳过 → 三态语义为 degraded（旧版会谎报 ok）
        assert rec.status == "degraded"
        assert rec.evidence_bundle_id == "evidtest1"
        assert rec.evidence_report.get("ok") is True
        assert rec.evidence_report.get("items_checked", 0) >= 2
        run_dir = tmp_path / "runs" / "evidtest1"
        assert (run_dir / "evidence.json").is_file()
        assert (run_dir / "evidence.md").is_file()
        data = pipe.get_run("evidtest1")
        assert data is not None and data["status"] == "degraded"
        entry = next(x for x in pipe.list_runs() if x["run_id"] == "evidtest1")
        assert entry["status"] == "degraded"  # 索引状态与 run.json 一致


# ── REST API：错误码语义（历史 APIError 误用导致恒 400） ──────────────────
class TestRestApi:
    @pytest.fixture
    def client(self) -> TestClient:
        from harness.main import app

        with TestClient(app) as c:
            yield c

    def test_status_endpoint(self, client: TestClient) -> None:
        r = client.get("/api/reverse-engineering/status")
        assert r.status_code == 200
        stages = r.json()["stages"]
        assert {s["stage"] for s in stages} >= {"rea_static", "evidence_v2"}

    def test_missing_run_returns_404(self, client: TestClient) -> None:
        r = client.get("/api/reverse-engineering/runs/nonexistent1")
        assert r.status_code == 404
        assert r.json()["code"] == "REV_ENG_RUN_NOT_FOUND"

    def test_invalid_run_id_shape_returns_404(self, client: TestClient) -> None:
        r = client.get("/api/reverse-engineering/runs/bad..id")
        assert r.status_code == 404

    def test_missing_subject_returns_404(self, client: TestClient, tmp_path: Path) -> None:
        r = client.post("/api/reverse-engineering/runs", json={"subject": str(tmp_path / "nope.bin")})
        assert r.status_code == 404
        assert r.json()["code"] == "REV_ENG_SUBJECT_NOT_FOUND"

    def test_invalid_config_returns_400(self, client: TestClient, tmp_path: Path) -> None:
        f = tmp_path / "s.bin"
        f.write_bytes(b"MZ")
        r = client.post(
            "/api/reverse-engineering/runs",
            json={"subject": str(f), "config": {"run_id": ".."}},
        )
        assert r.status_code == 400
        assert r.json()["code"] == "REV_ENG_INVALID_CONFIG"

    def test_progress_sse_end_to_end(self, client: TestClient, tmp_path: Path) -> None:
        """端到端：POST 后台运行 → SSE 订阅进度 → 阶段/步骤/收口事件齐全且零重复。

        回归点：SSE 端点先回放历史再增量推送，两通道不得重复投递同一事件。
        """
        f = tmp_path / "s.bin"
        f.write_bytes(b"MZ" + b"\x00" * 64 + b"harness" * 16)

        r = client.post(
            "/api/reverse-engineering/runs",
            json={"subject": str(f), "config": {"stages": ["evidence_v2"]}},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "running"
        run_id = r.json()["run_id"]

        events: list[dict[str, Any]] = []
        with client.stream("GET", f"/api/reverse-engineering/runs/{run_id}/progress") as s:
            assert s.status_code == 200
            assert s.headers["content-type"].startswith("text/event-stream")
            for line in s.iter_lines():
                if not line.startswith("data: "):
                    continue
                events.append(json.loads(line[len("data: ") :]))
                if events[-1].get("type") in ("run_done", "run_error"):
                    break

        types = [e["type"] for e in events]
        starts = [e for e in events if e["type"] == "phase_start"]
        dones = [e for e in events if e["type"] == "phase_done"]
        # 七阶段（含被跳过的）各一对 phase_start / phase_done，序号与总数正确
        assert len(starts) == 7
        assert len(dones) == 7
        assert {e["stage"] for e in starts} == {e["stage"] for e in dones}
        assert sorted(e["stage_index"] for e in starts) == list(range(7))
        assert all(e["stage_total"] == 7 for e in starts)
        assert "rebuild_exe" in {e["stage"] for e in starts}
        # 步骤级进度（evidence 阶段 4 步）与运行收口
        assert "phase_progress" in types
        assert types.count("run_done") == 1
        assert types[-1] == "run_done"
        # 关键回归：任何事件不得重复（回放 + 增量双通道）
        keys = [(e["type"], e.get("stage"), e.get("done")) for e in events]
        assert len(keys) == len(set(keys)), f"出现重复事件：{keys}"

    def test_progress_sse_missing_run_returns_404(self, client: TestClient) -> None:
        """回归点：订阅不存在的 run 必须 404，而不是挂起等待永不发生的事件。"""
        r = client.get("/api/reverse-engineering/runs/nonexistent1/progress")
        assert r.status_code == 404
        assert r.json()["code"] == "REV_ENG_RUN_NOT_FOUND"

    def _write_terminal_run(self, runs_dir: Path, run_id: str, status: str) -> Path:
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True)
        rec = {
            "run_id": run_id,
            "status": status,
            "subject_name": "a.bin",
            "error": f"运行未完成（{status}）" if status in ("failed", "interrupted") else "",
        }
        (run_dir / "run.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        return runs_dir

    def test_progress_sse_terminal_run_synthesizes_final_event(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """后端重启场景：run 已终结、内存事件总线为空 → SSE 合成终态事件而非永久空挂。"""
        from harness.api.rest import reverse_engineering as rest_mod
        from harness.modules.reverse_engineering.pipeline import ReverseEngineeringPipeline

        runs_dir = self._write_terminal_run(tmp_path / "runs", "oldrun001", "ok")
        monkeypatch.setattr(rest_mod, "_pipeline", lambda: ReverseEngineeringPipeline(runs_dir=runs_dir))

        events: list[dict[str, Any]] = []
        with client.stream("GET", "/api/reverse-engineering/runs/oldrun001/progress") as s:
            assert s.status_code == 200
            for line in s.iter_lines():
                if not line.startswith("data: "):
                    continue
                events.append(json.loads(line[len("data: ") :]))
                break  # 终态事件应为唯一一条，读完即收口
        assert len(events) == 1
        assert events[0]["type"] == "run_done"
        assert events[0]["record"]["run_id"] == "oldrun001"

    def test_progress_sse_interrupted_run_synthesizes_error(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """遗留 running 被启动收敛为 interrupted 后，SSE 订阅应得到 run_error 而非空挂。"""
        from harness.api.rest import reverse_engineering as rest_mod
        from harness.modules.reverse_engineering.pipeline import ReverseEngineeringPipeline

        runs_dir = self._write_terminal_run(tmp_path / "runs", "deadrun01", "interrupted")
        monkeypatch.setattr(rest_mod, "_pipeline", lambda: ReverseEngineeringPipeline(runs_dir=runs_dir))

        events: list[dict[str, Any]] = []
        with client.stream("GET", "/api/reverse-engineering/runs/deadrun01/progress") as s:
            assert s.status_code == 200
            for line in s.iter_lines():
                if not line.startswith("data: "):
                    continue
                events.append(json.loads(line[len("data: ") :]))
                break
        assert len(events) == 1
        assert events[0]["type"] == "run_error"
        assert "interrupted" in events[0]["error"]

    def test_upload_exe_is_recognized(self, client: TestClient, tmp_path: Path) -> None:
        """multipart 上传 PE 文件后，后端自动识别为 executable 并返回处理流程。"""
        f = tmp_path / "sample.exe"
        f.write_bytes(b"MZ" + b"\x00" * 64 + b"harness" * 16)
        with f.open("rb") as fh:
            r = client.post(
                "/api/reverse-engineering/runs/upload",
                files={"file": ("sample.exe", fh, "application/octet-stream")},
                data={"notes": "upload test"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "running"
        assert body["classification"]["kind"] == "executable"
        assert body["plan"]["flow"] == "executable_reverse_engineering"
        assert (Path(body["subject_path"]).parent / "..").resolve().name == body["run_id"]

    def test_upload_rejects_oversized_file(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回归点：上传超过 MAX_UPLOAD_BYTES 必须 413 且清掉残片，不能无上限写盘。"""
        from harness.api.rest import reverse_engineering as rest_mod
        from harness.modules.reverse_engineering.pipeline import ReverseEngineeringPipeline

        runs_dir = tmp_path / "runs"
        monkeypatch.setattr(rest_mod, "_pipeline", lambda: ReverseEngineeringPipeline(runs_dir=runs_dir))
        monkeypatch.setattr(rest_mod, "MAX_UPLOAD_BYTES", 4096)

        f = tmp_path / "big.bin"
        f.write_bytes(b"MZ" + b"\x00" * 8192)
        with f.open("rb") as fh:
            r = client.post(
                "/api/reverse-engineering/runs/upload",
                files={"file": ("big.bin", fh, "application/octet-stream")},
                data={"notes": "too big"},
            )
        assert r.status_code == 413
        assert r.json()["code"] == "REV_ENG_UPLOAD_TOO_LARGE"
        # 超限后不得残留半截样本，否则会被后续流水线当成完整输入
        assert list(runs_dir.glob("*/uploads/big.bin")) == []

    def test_upload_dot_filename_falls_back_to_default(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回归点：文件名净化保留 '.'，纯点名（"."/".."）须回退默认名而非指向目录。"""
        from harness.api.rest import reverse_engineering as rest_mod
        from harness.modules.reverse_engineering.pipeline import ReverseEngineeringPipeline

        runs_dir = tmp_path / "runs"
        monkeypatch.setattr(rest_mod, "_pipeline", lambda: ReverseEngineeringPipeline(runs_dir=runs_dir))

        f = tmp_path / "payload.bin"
        f.write_bytes(b"MZ" + b"\x00" * 64)
        with f.open("rb") as fh:
            r = client.post(
                "/api/reverse-engineering/runs/upload",
                files={"file": ("..", fh, "application/octet-stream")},
                data={"notes": "dot name"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["subject_name"] == "upload.bin"
        assert Path(body["subject_path"]).name == "upload.bin"

    @requires_java_class
    def test_upload_java_class_routes_to_decompile(self, client: TestClient, tmp_path: Path) -> None:
        """multipart 上传 .class 文件后，后端自动识别为 java_class 并仅路由到反编译阶段。"""
        src = tmp_path / "Hello.java"
        src.write_text(
            "public class Hello {\n"
            "    public String greet(String name) { return \"Hello, \" + name; }\n"
            "}\n",
            encoding="utf-8",
        )
        subprocess.run([str(_javac_for_test()), str(src)], check=True, cwd=tmp_path)
        cls = tmp_path / "Hello.class"
        assert cls.is_file()

        with cls.open("rb") as fh:
            r = client.post(
                "/api/reverse-engineering/runs/upload",
                files={"file": ("Hello.class", fh, "application/octet-stream")},
                data={"notes": "java test"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["classification"]["kind"] == "java_class"
        assert body["plan"]["flow"] == "java_class_decompile"
        assert Path(body["subject_path"]).name == "Hello.class"


# ── 重启收敛：遗留 running → interrupted ─────────────────────────────────
class TestStaleRunReconciliation:
    def test_reconcile_marks_running_as_interrupted(self, tmp_path: Path) -> None:
        from harness.modules.reverse_engineering.pipeline import _reconcile_stale_runs

        runs_dir = tmp_path / "runs"
        stale = runs_dir / "stalerun1"
        stale.mkdir(parents=True)
        (stale / "run.json").write_text(
            json.dumps({"run_id": "stalerun1", "status": "running"}), encoding="utf-8"
        )
        done = runs_dir / "donerun01"
        done.mkdir()
        (done / "run.json").write_text(
            json.dumps({"run_id": "donerun01", "status": "ok"}), encoding="utf-8"
        )
        (runs_dir / "index.json").write_text(
            json.dumps(
                {
                    "stalerun1": {"run_id": "stalerun1", "status": "running"},
                    "donerun01": {"run_id": "donerun01", "status": "ok"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        assert _reconcile_stale_runs(runs_dir) == 1
        stale_data = json.loads((stale / "run.json").read_text(encoding="utf-8"))
        assert stale_data["status"] == "interrupted"
        assert stale_data["finished_at"]
        assert "interrupted" in stale_data["error"]
        done_data = json.loads((done / "run.json").read_text(encoding="utf-8"))
        assert done_data["status"] == "ok"  # 已终态的记录不动
        idx = json.loads((runs_dir / "index.json").read_text(encoding="utf-8"))
        assert idx["stalerun1"]["status"] == "interrupted"
        assert idx["donerun01"]["status"] == "ok"
        # 幂等：再跑一遍收敛数为 0
        assert _reconcile_stale_runs(runs_dir) == 0

    def test_pipeline_init_reconciles_stale_runs(self, tmp_path: Path) -> None:
        """实例化流水线即自动收敛该 runs_dir 的遗留 running（每目录每进程一次）。"""
        from harness.modules.reverse_engineering.pipeline import ReverseEngineeringPipeline

        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "stalerun2"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps({"run_id": "stalerun2", "status": "running"}), encoding="utf-8"
        )
        pipe = ReverseEngineeringPipeline(runs_dir=runs_dir)
        data = pipe.get_run("stalerun2")
        assert data is not None and data["status"] == "interrupted"
        assert data["finished_at"]

