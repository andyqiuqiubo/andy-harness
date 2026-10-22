"""三大核心模块安全回归测试（体检报告 P0/P1 修复批次）。

覆盖：
- P0-1 路径穿越：``sid`` 白名单 + ``_session_dir()`` 守卫（读写两侧）；
- P0-2 子进程环境泄漏：录制器/逆向子进程 env 白名单；
- P1-1 审批默认值：默认策略必须是安全档（degraded），非法值不得回落 never；
- P1-4 SSRF：Embedding / Rerank / ASR / Vision 端点必须过 net_guard。

全部为纯单元/组件级测试，不触碰真实桌面与网络。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.infra.safe_path import UnsafePathError, is_safe_identifier, safe_join
from harness.infra.subproc import (
    SENSITIVE_ENV_DENYLIST,
    assert_env_isolated,
    build_env_allowlist,
)
from harness.modules.desktop_repeat.config import RepeatConfig
from harness.modules.desktop_repeat.errors import RepeatError
from harness.modules.desktop_repeat.safety_gate import SafetyGate

# ── P0-1：路径守卫 ────────────────────────────────────────────────────


class TestSafePath:
    """``harness.infra.safe_path`` 标识符白名单 + 越界防护。"""

    @pytest.mark.parametrize("bad", [
        "..", ".", "", "a/b", "a\\b", "../etc", "..\\windows",
        ".hidden", "a/../b", "/abs", "C:\\abs",
    ])
    def test_rejects_unsafe_identifiers(self, bad: str) -> None:
        assert not is_safe_identifier(bad)

    @pytest.mark.parametrize("ok", [
        "r-20261020-120000-a1b2c3", "run-abc123", "wf_1", "A.b-c_d", "s01",
    ])
    def test_accepts_valid_identifiers(self, ok: str) -> None:
        assert is_safe_identifier(ok)

    def test_safe_join_rejects_traversal(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        root.mkdir()
        with pytest.raises(UnsafePathError):
            safe_join(root, "..")
        with pytest.raises(UnsafePathError):
            safe_join(root, "../victim")
        with pytest.raises(UnsafePathError):
            safe_join(root, "a/../../b")

    def test_safe_join_stays_inside_root(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        root.mkdir()
        target = safe_join(root, "r-1")
        assert target.parent == root.resolve()
        assert target.name == "r-1"

    def test_safe_join_rejects_empty_parts(self, tmp_path: Path) -> None:
        with pytest.raises(UnsafePathError):
            safe_join(tmp_path)


class TestRepeatSessionDirGuard:
    """P0-1：service 层的 ``_session_dir()`` 守卫。"""

    @staticmethod
    def _svc(tmp_path: Path):
        from harness.modules.desktop_repeat.service import DesktopRepeatService

        class _DB:
            def execute(self, *a, **k):  # noqa: ANN002, ANN003
                class _Cur:
                    rowcount = 0

                return _Cur()

            def query(self, *a, **k):  # noqa: ANN002, ANN003
                return []

        root = tmp_path / "sessions"
        return DesktopRepeatService(_DB(), root)

    @pytest.mark.parametrize("bad", ["..", "../..", "a/../..", "..\\x", "."])
    def test_rejects_traversal_sid(self, tmp_path: Path, bad: str) -> None:
        svc = self._svc(tmp_path)
        with pytest.raises(RepeatError) as ei:
            svc._session_dir(bad)
        assert ei.value.code == "RP-6001"

    def test_delete_session_refuses_traversal(self, tmp_path: Path) -> None:
        """PoC 回归：``DELETE /sessions/..`` 不得删除 sessions_root 的父目录。"""
        svc = self._svc(tmp_path)
        victim = tmp_path / "victim"
        victim.mkdir()
        marker = victim / "keep.txt"
        marker.write_text("x", encoding="utf-8")

        with pytest.raises(RepeatError):
            svc.delete_session("..")
        with pytest.raises(RepeatError):
            svc.delete_session("../victim")

        assert victim.exists(), "路径穿越删除了根目录之外的目录！"
        assert marker.exists()

    def test_delete_session_valid_id_works(self, tmp_path: Path) -> None:
        svc = self._svc(tmp_path)
        sid = "r-20261020-000000-abcdef"
        sdir = svc._session_dir(sid)
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "session.json").write_text("{}", encoding="utf-8")
        assert svc.delete_session(sid) is True
        assert not sdir.exists()

    def test_read_session_rejects_traversal(self, tmp_path: Path) -> None:
        svc = self._svc(tmp_path)
        with pytest.raises(RepeatError):
            svc.get_session("..")

    def test_vision_status_rejects_traversal(self, tmp_path: Path) -> None:
        svc = self._svc(tmp_path)
        with pytest.raises(RepeatError):
            svc.vision_status("../..")

            safe_join(tmp_path)



# ── P0-2：子进程环境变量白名单 ───────────────────────────────────────


class TestSubprocEnvAllowlist:
    """P0-2：子进程不得继承完整 ``os.environ``。"""

    def test_allowlist_drops_sensitive_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        monkeypatch.setenv("HARNESS_KB_ASR_API_KEY", "asr-secret")
        monkeypatch.setenv("MY_DB_PASSWORD", "db-secret")
        monkeypatch.setenv("HARNESS_REPEAT_FRAME_TIER", "forensic")
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

        env = build_env_allowlist()

        assert "OPENAI_API_KEY" not in env
        assert "HARNESS_KB_ASR_API_KEY" not in env
        assert "MY_DB_PASSWORD" not in env
        assert env.get("HARNESS_REPEAT_FRAME_TIER") == "forensic"
        assert_env_isolated(env)

    def test_allowlist_is_subset_of_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOTALLY_UNRELATED_SECRET", "x")
        env = build_env_allowlist()
        assert "TOTALLY_UNRELATED_SECRET" not in env

    def test_sensitive_never_passthrough_even_if_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        env = build_env_allowlist(extra_allow=("OPENAI_API_KEY",),
                                  extra_vars={"OPENAI_API_KEY": "sk-other"})
        assert "OPENAI_API_KEY" not in env

    def test_assert_env_isolated_detects_leak(self) -> None:
        with pytest.raises(AssertionError):
            assert_env_isolated({"OPENAI_API_KEY": "sk-secret"})

    def test_denylist_contains_project_prefixed_secret(self) -> None:
        """``HARNESS_*`` 前缀放行不得把项目内凭据一起带出去。"""
        assert "HARNESS_KB_ASR_API_KEY" in SENSITIVE_ENV_DENYLIST



class TestRecorderSpawnEnv:
    """P0-2：录制器子进程实际使用的 env 必须是白名单。"""

    def test_spawn_recorder_uses_allowlist(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from harness.modules.desktop_repeat.service import DesktopRepeatService

        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        monkeypatch.setenv("DB_PASSWORD", "db-secret")

        captured: dict[str, object] = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):  # noqa: ANN001, ANN003
                captured["cmd"] = cmd
                captured["env"] = kwargs.get("env")

            def poll(self):  # noqa: ANN102, ANN201
                return None

        class _DB:
            def execute(self, *a, **k):  # noqa: ANN002, ANN003
                class _Cur:
                    rowcount = 0

                return _Cur()

            def query(self, *a, **k):  # noqa: ANN002, ANN003
                return []

        import harness.modules.desktop_repeat.service as svc_mod

        monkeypatch.setattr(svc_mod.subprocess, "Popen", _FakePopen)

        svc = DesktopRepeatService(_DB(), tmp_path / "sessions")
        sdir = tmp_path / "sessions" / "r-x"
        (sdir / "runtime").mkdir(parents=True, exist_ok=True)
        svc._spawn_recorder(sdir)

        env = captured["env"]
        assert isinstance(env, dict)
        assert "OPENAI_API_KEY" not in env
        assert "DB_PASSWORD" not in env
        assert env.get("PYTHONIOENCODING") == "utf-8"


# ── P1-1：审批默认值 ─────────────────────────────────────────────────


class TestApprovalPolicyDefault:
    """P1-1：危险动作默认免审（never）是不可接受的安全默认值。"""

    def test_default_policy_is_not_never(self) -> None:
        assert RepeatConfig().approval_policy == "degraded"

    def test_load_default_policy_is_safe(self) -> None:
        assert RepeatConfig.load().approval_policy == "degraded"

    def test_invalid_policy_does_not_fall_back_to_never(self) -> None:
        """非法值必须回落到安全档，而不是静默关闭全部审批。"""
        cfg = RepeatConfig.load({"repeat.approval_policy": "bogus"})
        assert cfg.approval_policy == "degraded"
        cfg2 = RepeatConfig.load({"approval_policy": "bogus"})
        assert cfg2.approval_policy == "degraded"

    def test_explicit_never_still_supported(self) -> None:
        cfg = RepeatConfig.load({"repeat.approval_policy": "never"})
        assert cfg.approval_policy == "never"

    def test_gate_default_policy_is_safe(self) -> None:
        assert SafetyGate().approval_policy == "degraded"

    def test_dangerous_action_requires_approval_by_default(self) -> None:
        """默认档下，危险动作必须产生待审批（而不是直接放行）。"""
        gate = SafetyGate(mode="interactive", approval_provider=None)
        d = gate.check(
            run_id="run-1", sid="s01", action="click", risk="dangerous",
            app_hint={}, params={}, scope_ok=True, step_index=0,
        )
        assert d.allow is False
        assert d.pending_approval is True
        assert d.code == "RP-4003"

    def test_explicit_never_allows_dangerous(self) -> None:
        """显式配置 never 时行为不变（无人值守场景仍可用）。"""
        gate = SafetyGate(mode="interactive", approval_provider=None, approval_policy="never")
        d = gate.check(
            run_id="run-1", sid="s01", action="click", risk="dangerous",
            app_hint={}, params={}, scope_ok=True, step_index=0,
        )
        assert d.allow is True


# ── P1-4：SSRF 护栏接入 ─────────────────────────────────────────────


class TestKnowledgeBaseNetGuard:
    """P1-4：知识库的外部请求必须过 ``net_guard``。"""

    def test_embedding_client_rejects_private_endpoint(self) -> None:
        import asyncio

        from harness.modules.knowledge_base.embedding import EmbeddingClient, EmbeddingError

        client = EmbeddingClient(endpoint="http://127.0.0.1:8000/v1",
                                 api_key="k", model_name="m")
        with pytest.raises(EmbeddingError):
            asyncio.run(client.embed(["hello"]))

    def test_rerank_client_rejects_private_endpoint(self) -> None:
        import asyncio

        from harness.modules.knowledge_base.embedding import EmbeddingError, RerankClient

        client = RerankClient(endpoint="http://169.254.169.254", api_key="k", model_name="m")
        with pytest.raises(EmbeddingError):
            asyncio.run(client.rerank("q", ["d"]))

    def test_asr_adapter_rejects_private_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from harness.modules.knowledge_base.adapters import _transcribe_via_openai
        from harness.modules.knowledge_base.parsers import ParseError

        monkeypatch.setenv("HARNESS_KB_ASR_ENDPOINT", "http://127.0.0.1:9000")
        with pytest.raises(ParseError):
            _transcribe_via_openai(b"audio", "a.mp3")

    def test_vision_client_rejects_private_endpoint(self) -> None:
        import asyncio

        from harness.modules.desktop_repeat.vision.client import BailianVisionClient

        client = BailianVisionClient(api_key="k", base_url="http://127.0.0.1:9999")
        assert asyncio.run(client.analyze_images([], "prompt")) is None

    def test_vision_client_blocks_private_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """端点不安全时必须在发请求前短路（不触网）。"""
        import asyncio

        from harness.modules.desktop_repeat.vision import client as vc

        called: dict[str, object] = {}

        class _FakeClient:
            def __init__(self, *a, **k):  # noqa: ANN002, ANN003, ANN004
                called["init"] = True

            async def __aenter__(self):  # noqa: ANN204
                return self

            async def __aexit__(self, *a):  # noqa: ANN002, ANN003, ANN204
                return False

            async def post(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
                called["post"] = True
                raise AssertionError("不安全端点不应发起请求")

        monkeypatch.setattr(vc.httpx, "AsyncClient", _FakeClient)
        client = vc.BailianVisionClient(api_key="k", base_url="http://127.0.0.1:9999")
        assert asyncio.run(client.analyze_images(["x.png"], "prompt")) is None
        assert "post" not in called


class TestReverseEngineeringEnv:
    """P1-6：逆向子进程（rebuild）不得继承完整环境。"""

    def test_rebuild_subprocess_uses_allowlist(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import harness.modules.reverse_engineering.rebuild as rb

        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        captured: dict[str, object] = {}

        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "boom"

        def _fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ANN202
            captured["env"] = kwargs.get("env")
            return _Proc()

        monkeypatch.setattr(rb.subprocess, "run", _fake_run)
        monkeypatch.setattr(rb, "find_tool", lambda: tmp_path / "tool.py")
        monkeypatch.setattr(rb, "find_tcc", lambda: tmp_path / "tcc.exe")

        rb.run_rebuild("run-1")
        env = captured["env"]
        assert isinstance(env, dict)
        assert "OPENAI_API_KEY" not in env
        assert_env_isolated(env)


# ── P1-2 / P1-3：解压炸弹防护 ───────────────────────────────────────


class TestZipBombProtection:
    """DOCX / jar 解压上限（体检 P1-2 / P1-3）。"""

    def test_docx_rejects_too_many_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import io
        import zipfile

        from harness.modules.knowledge_base.parsers import ParseError, parse_docx

        monkeypatch.setattr(
            "harness.modules.knowledge_base.parsers.MAX_DOCX_ENTRIES", 5
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", "<w:document/>")
            for i in range(20):
                zf.writestr(f"word/x{i}.xml", "x")
        with pytest.raises(ParseError) as ei:
            parse_docx(buf.getvalue(), "assets", "doc-1")
        assert "条目数过多" in str(ei.value)

    def test_docx_rejects_oversized_uncompressed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import io
        import zipfile

        from harness.modules.knowledge_base.parsers import ParseError, parse_docx

        monkeypatch.setattr(
            "harness.modules.knowledge_base.parsers.MAX_DOCX_UNCOMPRESSED_BYTES", 1024
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("word/document.xml", "<w:document/>")
            zf.writestr("word/big.bin", "A" * 100000)
        with pytest.raises(ParseError) as ei:
            parse_docx(buf.getvalue(), "assets", "doc-1")
        assert "解压后体积超过上限" in str(ei.value)

    def test_jar_extraction_rejects_path_traversal(self, tmp_path: Path) -> None:
        """逆向 jar 解包：``../`` 条目必须被拒绝（不得写到 out_dir 之外）。"""
        import zipfile

        from harness.modules.reverse_engineering.stages import JavaDecompileStage

        out_dir = tmp_path / "java_source"
        out_dir.mkdir()
        jar = out_dir / "evil.jar"
        with zipfile.ZipFile(jar, "w") as zf:
            zf.writestr("../escaped.txt", "pwned")
        JavaDecompileStage._extract_jars(out_dir)
        assert not (tmp_path / "escaped.txt").exists(), "jar 路径穿越未被拦截！"

    def test_jar_extraction_rejects_oversized(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import zipfile

        import harness.modules.reverse_engineering.stages as st

        monkeypatch.setattr(st, "MAX_JAR_UNCOMPRESSED_BYTES", 1024)
        out_dir = tmp_path / "java_source"
        out_dir.mkdir()
        jar = out_dir / "big.jar"
        with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("A.java", "A" * 100000)
        st.JavaDecompileStage._extract_jars(out_dir)
        assert not (out_dir / "A.java").exists(), "超大 jar 未被拦截！"


# ── P1-9：run 异常后租约必须释放 ────────────────────────────────────


class TestLeaseReleaseOnFailure:
    """P1-9：一次异常不应让整个录制/回放功能永久不可用。"""

    def test_lease_released_when_runner_build_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from harness.modules.desktop_repeat.service import DesktopRepeatService

        class _DB:
            def execute(self, *a, **k):  # noqa: ANN002, ANN003
                class _Cur:
                    rowcount = 0

                return _Cur()

            def query(self, *a, **k):  # noqa: ANN002, ANN003
                return []

        class _Lease:
            def __init__(self) -> None:
                self.held = False
                self.released = 0

            def acquire(self):  # noqa: ANN201
                self.held = True
                return True, 0

            def release(self) -> None:
                self.held = False
                self.released += 1

        lease = _Lease()
        svc = DesktopRepeatService(_DB(), tmp_path / "sessions")

        def _platforms(_self):  # noqa: ANN001, ANN202
            return {"capture": None, "inspector": None, "grabber": None,
                    "injector": None, "windows": None, "env": None,
                    "lease": lease, "takeover": None}

        monkeypatch.setattr(type(svc), "_platforms", _platforms, raising=False)
        # 最小工作流：start → gui 单步，参数解析不产生 RP-800x
        wf = {
            "id": "wf-1",
            "graph": {
                "nodes": [
                    {"id": "n1", "type": "start", "data": {}},
                    {"id": "n2", "type": "gui", "data": {
                        "sid": "s01", "action": "click", "label": "s01",
                        "target": {
                            "scope": "window",
                            "app_hint": {"exe": "app.exe"},
                            "locators": [{"kind": "uia", "by": "automation_id",
                                          "value": "btn1", "confidence": 0.95}],
                        },
                        "risk": "write",
                        "policy": {"on_fail": "abort", "retries": 0, "backoff_ms": 1},
                    }},
                ],
                "edges": [{"source": "n1", "target": "n2"}],
                "meta": {"params": []},
            },
        }
        monkeypatch.setattr(svc, "_get_workflow", lambda _wid: wf)
        # 注入构建期异常
        def _boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
            raise RuntimeError("注入的构建期异常")

        monkeypatch.setattr(svc, "_start_run_record", _boom)

        import asyncio

        with pytest.raises(RuntimeError):
            asyncio.run(svc.start_run("wf-1", {}))
        assert lease.released >= 1, "异常路径未释放输入租约"
        assert not lease.held
