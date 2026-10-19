"""desktop_repeat 服务层/编译层/存储层闭环测试（§12.1 组件层）。

手工构造录制 bundle（事件+帧）→ manifest 封版 → compile 9 阶段管线 →
发布工作流 → Fake 平台回放 → 技能导出 → CLI 冒烟。

不触碰真实输入钩子/注入（那是真机回归层）；全部走 service 的可注入件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import harness.modules.desktop_repeat.service as svc_mod
from harness.modules.desktop_repeat.config import RepeatConfig
from harness.modules.desktop_repeat.errors import RepeatError
from harness.modules.desktop_repeat.interpreter.pipeline import compile_session
from harness.modules.desktop_repeat.models import (
    Env,
    MonitorInfo,
    Pos,
    RecEvent,
    UiaSnapshot,
    WindowRef,
)
from harness.modules.desktop_repeat.store.bundle import BundleLock, BundleReader, BundleWriter, new_session_id
from harness.modules.desktop_repeat.store.jsonl import read_events

# ── Fake 平台件（service 注入，定义在 tests.repeat_fakes） ───────


# ── bundle 构造器 ───────────────────────────────────────────────
def make_bundle(root: Path, events_data: list[dict]) -> str:
    sid = new_session_id()
    env = Env(os="Test", virtual_screen=(0, 0, 1920, 1080),
              monitors=[MonitorInfo(index=1, rect=(0, 0, 1920, 1080), scale=1.0)])
    cfg = RepeatConfig.load({"frame_tier": "standard", "countdown_s": 0})
    rec = svc_mod.RecordConfig(label="测试流程")
    # BundleWriter(sessions_root, sid, …)：内部自拼 sessions_root/sid
    w = BundleWriter(root, sid, cfg, rec, env)
    # exe 与 FakeWindows 作用域一致（app_hint 越界校验的行为本身由 executor 套件覆盖）
    win = WindowRef(hwnd=42, title="测试应用主窗口", cls="Frame", pid=1, exe="app.exe",
                    rect=(0, 0, 1200, 900), client=(10, 40, 1190, 860))
    for i, spec in enumerate(events_data):
        t = i * 100
        data = dict(spec.get("data", {}))
        if "eid" in spec:
            data["eid"] = spec["eid"]
        ev = RecEvent(seq=i, t=t, ts=1760000000000 + t, type=spec["type"],
                      src=spec.get("src", "hook"),
                      pos=Pos(x=spec.get("x", 100), y=spec.get("y", 100),
                              monitor=1, scale=1.0, rel=(0.5, 0.5)) if spec.get("pos") else None,
                      win=win, uia=spec.get("uia"), data=data,
                      flags=spec.get("flags", {}))
        w.emit(ev)
    w.emit(RecEvent(seq=len(events_data), t=len(events_data) * 100,
                    ts=1760000000000 + len(events_data) * 100,
                    type="session_end", src="system", data={"reason": "stop"}))
    res = w.finalize(reason="stop")
    assert res["status"] == "ready", res
    return sid


def _snap(ct="Button", name="保存", aid="btnSave", path="Window[ERP]/Pane/Button[1]", **kw):
    return UiaSnapshot(control_type=ct, name=name, automation_id=aid, path=path,
                       depth=3, patterns=kw.get("patterns", ["InvokePattern"]),
                       rect=kw.get("rect", (700, 360, 780, 390)), **{k: v for k, v in kw.items()
                                                                       if k not in ("patterns", "rect")})


# ── 编译管线 ─────────────────────────────────────────────────────
class TestCompilePipeline:
    def test_full_compile_flow(self, tmp_path: Path):
        sid = make_bundle(tmp_path, [
            {"type": "session_start", "src": "system"},
            {"type": "click", "pos": True, "x": 740, "y": 375, "uia": _snap(),
             "data": {"button": "left", "eid": 1}},
            {"type": "key", "data": {"key": "a", "down": True, "modifiers": ["ctrl"],
                                     "_t_ms": 0}, "flags": {}},
            {"type": "window_change", "src": "derived",
             "data": {"prev": {"title": "老窗口", "exe": "other.exe"},
                      "curr": {"title": "ERP 主窗口", "exe": "erp.exe", "rect": [0, 0, 1200, 900]}}},
            {"type": "click", "pos": True, "x": 300, "y": 200,
             "uia": _snap(ct="Edit", name="客户名称", aid="txtCust",
                          path="Window[ERP]/Pane/Edit[1]", is_editable=True,
                          patterns=["ValuePattern"]),
             "data": {"button": "left", "eid": 4, "post_diff": 0.05}},
            {"type": "text_input", "src": "derived",
             "uia": _snap(ct="Edit", name="客户名称", aid="txtCust"),
             "data": {"text": "ACME", "source": "ime"}},
            {"type": "key", "data": {"key": "enter", "down": True, "modifiers": [],
                                     "_t_ms": 0}, "uia": _snap()},
        ])
        reader = BundleReader(tmp_path / sid)
        reader.verify()
        events, truncated = reader.events()
        assert truncated is False
        res = compile_session(events, session_id=sid)
        assert res.stats.steps >= 5
        types = [s.action for s in res.steps]
        assert "click" in types and "type" in types
        assert "hotkey" in types          # ctrl+a
        assert "window_focus" in types    # window_change
        # 参数建议：ACME 不像日期/邮箱 → 不参数化；type 步骤 value 存在
        ts = [s for s in res.steps if s.action == "type"][0]
        assert ts.params["value"] == "ACME"
        # 定位链：automation_id 优先
        first_click = [s for s in res.steps if s.action == "click"][0]
        assert first_click.locators[0].kind == "uia"
        assert first_click.locators[0].by == "automation_id"
        assert first_click.locators[-1].kind == "coord"
        # 图草稿可回放
        from harness.modules.desktop_repeat.executor.runner import steps_from_graph
        steps = steps_from_graph(res.draft_graph)
        assert [s.sid for s in steps] == [s.sid for s in res.steps]

    def test_loop_candidate_detection(self, tmp_path: Path):
        items = []
        for i in range(4):
            items.append({"type": "click", "pos": True, "x": 100, "y": 100 + i * 30,
                          "uia": _snap(ct="ListItem", name=f"行{i+1}", aid=f"item{i+1}",
                                       path=f"Window[ERP]/List/ListItem[{i+1}]"),
                          "data": {"button": "left", "eid": 10 + i}})
        sid = make_bundle(tmp_path, items)
        events, _ = BundleReader(tmp_path / sid).events()
        res = compile_session(events, session_id=sid)
        assert res.stats.loop_candidates >= 1

    def test_password_becomes_manual_confirm(self, tmp_path: Path):
        sid = make_bundle(tmp_path, [
            {"type": "key", "data": {"key": "<masked>", "count": 5, "password": True},
             "flags": {"masked": True},
             "uia": _snap(ct="Edit", name="", aid="", is_password=True)},
        ])
        events, _ = BundleReader(tmp_path / sid).events()
        res = compile_session(events, session_id=sid)
        s = res.steps[0]
        assert s.action == "manual_confirm"
        assert s.params.get("mode") == "human"

    def test_risk_words_upgrade(self, tmp_path: Path):
        sid = make_bundle(tmp_path, [
            {"type": "click", "pos": True, "x": 100, "y": 100,
             "uia": _snap(ct="Button", name="提交支付", aid="btnPay"),
             "data": {"button": "left"}},
        ])
        events, _ = BundleReader(tmp_path / sid).events()
        res = compile_session(events, session_id=sid)
        s = res.steps[0]
        assert s.risk == "dangerous" and s.needs_review
        assert s.policy["on_fail"] == "suspend_manual"

    def test_empty_session_failed(self, tmp_path: Path):
        root = tmp_path
        sid = new_session_id()
        sdir = root / sid
        env = Env(os="T", virtual_screen=(0, 0, 100, 100), monitors=[MonitorInfo(index=1)])
        w = BundleWriter(sdir, sid, RepeatConfig.load({"countdown_s": 0}),
                         svc_mod.RecordConfig(), env)
        res = w.finalize(reason="stop")
        assert res["status"] == "failed"

    def test_manifest_tamper_blocks_compile(self, tmp_path: Path):
        sid = make_bundle(tmp_path, [
            {"type": "click", "pos": True, "x": 1, "y": 1, "uia": _snap(),
             "data": {"button": "left"}},
        ])
        reader = BundleReader(tmp_path / sid)
        (tmp_path / sid / "events.jsonl").write_text(
            (tmp_path / sid / "events.jsonl").read_text(encoding="utf-8") + "junk",
            encoding="utf-8")
        with pytest.raises(RepeatError) as ei:
            reader.verify()
        assert ei.value.code == "RP-6001"

    def test_bundle_single_writer_lock(self, tmp_path: Path):
        sid = new_session_id()
        sdir = tmp_path / sid
        sdir.mkdir()
        lock = BundleLock(sdir)
        assert lock.acquire()
        assert not lock.acquire()  # 同一目录第二写者拒绝
        lock.release()


# ── 服务闭环（编译→发布→回放→技能） ─────────────────────────────
class _WFSvc:
    """最小 WorkflowService 替身（CRUD 内存版 + graph 契约）。"""

    def __init__(self) -> None:
        self.wfs: dict[str, dict] = {}
        self.n = 0

    def create_workflow(self, name: str, description: str, graph: dict) -> dict:
        self.n += 1
        wid = f"wf-{self.n:04d}"
        self.wfs[wid] = {"id": wid, "name": name, "description": description,
                         "graph": graph, "version": 1}
        return self.wfs[wid]

    def update_workflow(self, wid: str, name: str, description: str, graph: dict) -> dict:
        wf = self.wfs[wid]
        wf.update({"name": name, "description": description, "graph": graph})
        return wf

    def get_workflow(self, wid: str) -> dict | None:
        return self.wfs.get(wid)


@pytest.fixture()
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "sessions"
    root.mkdir()
    from harness.infra.database import Database

    db = Database(db_path=str(tmp_path / "test.db"))
    # 注入 Fake 平台（service._platforms 走它）
    from harness.modules.desktop_repeat.executor.pacing import NullTakeover
    from harness.modules.desktop_repeat.models import WindowRef
    from tests.repeat_fakes import (
        FakeEnv,
        FakeInjector,
        FakeInspector,
        FakeLease,
        FakeWindows,
    )

    def fake_platforms(self):
        if self._platform is None:
            # 窗口标题必须与 make_bundle 的录制窗口一致："测试应用主窗口"。
            # 作用域按标题隔离修复（体检 P0-4）后，标题不匹配的 exe 兜底命中
            # 会被正确判为"标题未确认"并拒绝写操作——用宽容假件才能测 happy path。
            _win = WindowRef(hwnd=42, title="测试应用主窗口", cls="Frame", pid=1, exe="app.exe",
                       rect=(0, 0, 1200, 900), client=(10, 40, 1190, 860))
            self._platform = {
                "capture": None,
                "inspector": FakeInspector({"automation_id:btnSave": "hit",
                                            "automation_id:txtCust": "hit"}),
                "grabber": None,
                "injector": FakeInjector(),
                "windows": FakeWindows(_win),
                "env": FakeEnv(),
                "lease": FakeLease(),
                # 假平台无真实注入：隔离全局 OS 输入态，防误判人工接管而永久挂起
                "takeover": NullTakeover(),
            }
        return self._platform

    monkeypatch.setattr(svc_mod.DesktopRepeatService, "_platforms", fake_platforms, raising=False)
    # 技能导出落临时目录，绝不误写用户 ~/.andy-harness
    monkeypatch.setattr(svc_mod.DesktopRepeatService, "_default_user_skills_root",
                        lambda self: (tmp_path / "skills"), raising=False)
    svc = svc_mod.DesktopRepeatService(db, root, RepeatConfig.load({"retries": 0}),
                                       workflow_service=_WFSvc())
    return svc


class TestServiceLoop:
    async def _make_ready_session(self, svc, tmp_root: Path) -> str:
        sid = make_bundle(tmp_root, [
            {"type": "session_start", "src": "system"},
            {"type": "click", "pos": True, "x": 740, "y": 375, "uia": _snap(),
             "data": {"button": "left"}},
            {"type": "text_input", "src": "derived",
             "uia": _snap(ct="Edit", name="客户名称", aid="txtCust",
                          patterns=["ValuePattern"], is_editable=True),
             "data": {"text": "ACME", "source": "ime"}},
        ])
        return sid

    @pytest.mark.asyncio
    async def test_compile_publish_run_skill(self, service, tmp_path: Path):
        sid = await self._make_ready_session(service, tmp_path / "sessions")
        # 同步 DB 记录（真实流程由录制写入；这里直接喂）
        doc = service.get_session(sid)
        assert doc["status"] == "ready"
        res = await service.compile_session(sid)
        assert res.stats.steps >= 1
        wf = await service.save_as_workflow(sid, "每日导出")
        assert wf["id"]
        # 干跑：假平台命中
        dry = await service.dry_run(wf["id"])
        assert dry["env_level"] == "L0"
        assert all(s["verdict"].startswith("hit") for s in dry["steps"])
        # 真实回放（fake 注入件）
        run = await service.start_run(wf["id"], {}, unattended=True)
        assert run["status"] == "running"
        # 等完成
        import asyncio
        for _ in range(150):
            st = service.get_run(run["run_id"])
            if st["status"] in ("succeeded", "failed", "aborted"):
                break
            await asyncio.sleep(0.02)
        assert st["status"] == "succeeded", st
        # 技能导出
        sk = await service.export_skill(wf["id"], slug="daily-export")
        skill_md = Path(sk["path"]) / "SKILL.md"
        assert skill_md.exists()
        text = skill_md.read_text(encoding="utf-8")
        assert "name: repeat-daily-export" in text
        assert wf["id"] in text and "graph_sha256:" in text
        assert "repeat_run" in text
        # 敏感信息不入技能文件
        assert "ACME" not in text  # 值不进技能（只有占位符/默认值表）

    @pytest.mark.asyncio
    async def test_start_run_rejects_missing_params(self, service, tmp_path: Path):
        sid = await self._make_ready_session(service, tmp_path / "sessions")
        res = await service.compile_session(sid)
        graph = res.draft_graph
        graph["nodes"][0]["data"]["params"] = [
            {"name": "cust_name", "type": "string", "required": True,
             "source": "runtime_only", "sensitive": False, "default": ""}]
        graph["nodes"][0]["data"]["variables"] = graph["nodes"][0]["data"]["params"]
        wf = await service.save_as_workflow(sid, "带参流程")
        service._require_workflow_service().wfs[wf["id"]]["graph"] = graph
        out = await service.start_run(wf["id"], {}, unattended=True)
        assert out.get("rejected") is True
        assert out["error"]["code"] == "RP-8003" or out["error"]["code"] == "RP-8001"

    @pytest.mark.asyncio
    async def test_export_skill_version_binding(self, service, tmp_path: Path):
        sid = await self._make_ready_session(service, tmp_path / "sessions")
        wf = await service.save_as_workflow(sid, "绑定版本")
        sk = await service.export_skill(wf["id"])
        sha = sk["graph_sha256"]
        # 正确哈希可跑
        run = await service.start_run(wf["id"], {}, unattended=True,
                                      expected_graph_sha256=sha)
        assert run.get("run_id")
        # 篡改版本拒绝
        with pytest.raises(RepeatError) as ei:
            await service.start_run(wf["id"], {}, unattended=True,
                                    expected_graph_sha256="0" * 64)
        assert ei.value.code == "RP-6004"

    @pytest.mark.asyncio
    async def test_recording_exclusive_with_run(self, service, tmp_path: Path):
        # 模拟活跃录制 → 禁止回放
        service._active_session = "r-fake"
        sid = make_bundle(tmp_path / "sessions", [
            {"type": "click", "pos": True, "x": 1, "y": 1, "uia": _snap(),
             "data": {"button": "left"}}])
        wf = await service.save_as_workflow(sid, "x")
        with pytest.raises(RepeatError) as ei:
            await service.start_run(wf["id"], {})
        assert ei.value.code == "RP-5004"

    @pytest.mark.asyncio
    async def test_status_payload(self, service):
        st = service.status()
        assert set(["available", "active_session", "running_runs", "config"]) <= set(st)

    def test_cli_graph_smoke(self, tmp_path: Path):
        # CLI 编译（--json）用真实管线跑通 bundle
        root = tmp_path / "s"
        root.mkdir()
        sid = make_bundle(root, [
            {"type": "click", "pos": True, "x": 5, "y": 5, "uia": _snap(),
             "data": {"button": "left"}}])
        from harness.modules.desktop_repeat.interpreter.pipeline import compile_session as cs
        events, _ = read_events(root / sid / "events.jsonl")
        res = cs(events, session_id=sid)
        assert res.stats.steps == 1
