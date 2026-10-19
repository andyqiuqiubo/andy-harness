"""andy_repeat 插件的 10 个 Agent 工具（§10.2）。

工具描述写明三条纪律（避免模型误用）：
1. 回放为确定性本地执行，**不调用模型、零 token**；
2. 需目标应用已启动且窗口可交互（流程首步 launch_app 除外）；
3. 提权窗口（管理员运行）不可操作——UIPI 系统限制，不做绕过。
"""

from __future__ import annotations

import json
from typing import Any

from harness.kernel.contracts.tool import ToolPlugin

_DESC_COMMON = (
    "【andy-repeat 桌面录制回放】"
    "回放=确定性本地执行（定位链 + UIA Pattern），不调用模型、断网可跑、零 token。"
    "前提：目标应用已启动且窗口可交互（流程首步为 launch_app 时自动启动）；"
    "以管理员权限运行的窗口无法自动操作（系统 UIPI 限制，请勿尝试）。"
)

_UNAVAILABLE = json.dumps({"error": "andy-repeat 服务不可用：插件未激活或依赖缺失（pip install 'harness[repeat]'）"},
                          ensure_ascii=False)


def _svc(services: Any) -> Any | None:
    try:
        from harness.modules.desktop_repeat.service import DesktopRepeatService
        if services is None or not services.has(DesktopRepeatService):
            return None
        return services.get(DesktopRepeatService)
    except Exception:  # noqa: BLE001
        return None


class _Base(ToolPlugin):
    def __init__(self, services: Any) -> None:
        self._services = services

    def _err(self, msg: str) -> str:
        return json.dumps({"error": msg}, ensure_ascii=False)

    async def _call(self, fn_name: str, /, *args: Any, **kwargs: Any) -> Any:
        svc = _svc(self._services)
        if svc is None:
            return json.loads(_UNAVAILABLE)
        fn = getattr(svc, fn_name)
        return await fn(*args, **kwargs)


# ── 录制 ────────────────────────────────────────────────────────
class RecordStartTool(_Base):
    @property
    def tool_name(self) -> str:
        return "repeat_record_start"

    @property
    def risk_level(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return (_DESC_COMMON + "repeat_record_start：开始录制用户的桌面操作（鼠标/键盘/窗口切换）。"
                "调用后用户本机出现 3-2-1 倒计时并常驻红点胶囊；期间所有点击、键入（密码框除外——"
                "密码框永不记录明文）与窗口变化被记录为结构化事件流。热键：Ctrl+Alt+P 暂停/恢复"
                "（长按=跳过段落），Ctrl+Alt+X 丢弃整段，F10 打点标记。录制结束用 repeat_record_stop。"
                "录制与回放互斥：录制期间不能回放。")

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "流程名称（如「ERP 每日订单导出」）"},
                "frame_tier": {"type": "string", "enum": ["minimal", "standard", "forensic"],
                               "description": "留证档：minimal=不存帧/standard=裁剪帧（默认）/forensic=全屏帧"},
                "countdown_s": {"type": "integer", "description": "倒计时秒数 0/3/5，默认按设置"},
                "mask_windows": {"type": "array", "items": {"type": "string"},
                                 "description": "额外屏蔽窗口标题正则（命中则整窗不采）"},
            },
            "required": [],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        from harness.modules.desktop_repeat.config import RecordConfig
        from harness.modules.desktop_repeat.errors import RepeatError
        rec = RecordConfig(label=str(args.get("label", "")),
                           frame_tier=str(args.get("frame_tier", "")),
                           countdown_s=int(args.get("countdown_s", -1) or -1),
                           mask_windows=[str(x) for x in (args.get("mask_windows") or [])])
        try:
            doc = await self._call("start_recording", rec)
        except RepeatError as e:
            return self._err(f"{e.code} {e.message}")
        return json.dumps({"session_id": doc.get("session_id"), "status": "recording",
                           "next": "请让用户在目标应用中完整演示一遍流程；演示结束后调用 repeat_record_stop 封版。"},
                          ensure_ascii=False)


class RecordStopTool(_Base):
    @property
    def tool_name(self) -> str:
        return "repeat_record_stop"

    @property
    def risk_level(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return (_DESC_COMMON + "repeat_record_stop：结束当前录制并封版（校验+清单）。"
                "discard=true 丢弃本次录制（不可恢复）。成功后建议接着调用 repeat_compile 解析步骤。")

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "缺省停止当前活跃会话"},
                "discard": {"type": "boolean", "description": "true=丢弃本次录制"},
            },
            "required": [],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        from harness.modules.desktop_repeat.errors import RepeatError
        try:
            doc = await self._call("stop_recording", args.get("session_id") or None,
                                   discard=bool(args.get("discard", False)))
        except RepeatError as e:
            return self._err(f"{e.code} {e.message}")
        return json.dumps({"session_id": doc.get("session_id"), "status": doc.get("status"),
                           "counters": doc.get("counters", {}), "error": doc.get("error"),
                           "next": ("可调用 repeat_compile 解析为可编辑工作流"
                                    if doc.get("status") == "ready" else "录制未成功封版，按 error 归因")},
                          ensure_ascii=False)


class RecordStatusTool(_Base):
    @property
    def tool_name(self) -> str:
        return "repeat_record_status"

    @property
    def risk_level(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return _DESC_COMMON + "repeat_record_status：查询当前录制状态（会话/事件数/心跳）。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, args: dict[str, Any]) -> str:
        svc = _svc(self._services)
        if svc is None:
            return _UNAVAILABLE
        st = svc.status()
        sid = st.get("active_session")
        out = dict(st)
        if sid:
            try:
                pass  # 命名空间兼容
            except Exception:  # noqa: BLE001
                pass
            try:
                reader = svc.get_session(sid)
                out["session"] = {k: reader.get(k) for k in ("status", "label", "counters")}
            except Exception:  # noqa: BLE001
                pass
        return json.dumps(out, ensure_ascii=False)


# ── 编译 / 列表 ─────────────────────────────────────────────────
class CompileTool(_Base):
    @property
    def tool_name(self) -> str:
        return "repeat_compile"

    @property
    def risk_level(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return (_DESC_COMMON + "repeat_compile：把录制会话的事件流编译为结构化步骤（9 阶段确定性管线："
                "去噪→归并→窗口绑定→定位链→等待→断言→循环→参数→风险）。返回步数、警告、"
                "待确认清单；save=true 直接存为工作流（画布可编辑）。低置信步骤务必让用户确认。")

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "use_llm": {"type": "boolean", "description": "可选：大模型润色步骤命名（仅改文字不改结构）"},
                "save": {"type": "boolean", "description": "true=编译并发布为工作流"},
                "name": {"type": "string", "description": "save 时的工作流名称"},
            },
            "required": ["session_id"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        from harness.modules.desktop_repeat.errors import RepeatError
        try:
            if args.get("save"):
                wf = await self._call("save_as_workflow", args["session_id"],
                                      str(args.get("name") or "录制流程"))
                return json.dumps({"workflow_id": wf.get("id"), "name": wf.get("name")},
                                  ensure_ascii=False)
            res = await self._call("compile_session", args["session_id"],
                                   use_llm=bool(args.get("use_llm", False)))
        except RepeatError as e:
            return self._err(f"{e.code} {e.message}")
        except KeyError:
            return self._err("缺少 session_id")
        d = res.to_dict() if hasattr(res, "to_dict") else res
        out = {"stats": d.get("stats"),
               "warnings": [w for w in d.get("warnings", [])][:20],
               "steps_preview": [{"sid": s["sid"], "action": s["action"], "label": s["label"],
                                  "confidence": s.get("meta", {}).get("confidence"),
                                  "needs_review": s.get("meta", {}).get("needs_review")}
                                 for s in d.get("steps", [])[:20]]}
        return json.dumps(out, ensure_ascii=False)


class ListTool(_Base):
    @property
    def tool_name(self) -> str:
        return "repeat_list"

    @property
    def risk_level(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return _DESC_COMMON + "repeat_list：列出录制会话 / 工作流 / 运行历史。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["sessions", "workflows", "runs"]},
                "limit": {"type": "integer"},
            },
            "required": ["kind"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        kind = str(args.get("kind", "sessions"))
        limit = int(args.get("limit", 20) or 20)
        svc = _svc(self._services)
        if svc is None:
            return _UNAVAILABLE
        if kind == "sessions":
            return json.dumps(svc.list_sessions(limit=limit), ensure_ascii=False)
        if kind == "runs":
            return json.dumps(svc.list_runs(limit=limit), ensure_ascii=False)
        try:
            from harness.infra.database import Database
            from harness.modules.workflows.service import WorkflowService

            wf = WorkflowService(Database(), self._services)
            return json.dumps([{"id": w["id"], "name": w["name"],
                                "description": w.get("description", "")}
                               for w in wf.list_workflows()[:limit]], ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return self._err(f"工作流列表不可用: {e}")


# ── 运行 ────────────────────────────────────────────────────────
class DryRunTool(_Base):
    @property
    def tool_name(self) -> str:
        return "repeat_dry_run"

    @property
    def risk_level(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return (_DESC_COMMON + "repeat_dry_run：定位链体检（每步只解析不注入，零副作用）。"
                "执行/定时前应先用它验证；返回每步命中第几个定位器或失败原因。")

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {
            "workflow_id": {"type": "string"},
            "params": {"type": "object", "description": "参数值（缺参时在零副作用前返回缺失清单）"},
        }, "required": ["workflow_id"]}

    async def execute(self, args: dict[str, Any]) -> str:
        try:
            rep = await self._call("dry_run", args["workflow_id"], args.get("params") or {})
        except Exception as e:  # noqa: BLE001
            return self._err(str(e))
        return json.dumps(rep, ensure_ascii=False)


class RunTool(_Base):
    @property
    def tool_name(self) -> str:
        return "repeat_run"

    @property
    def risk_level(self) -> str:
        return "dangerous"

    @property
    def description(self) -> str:
        return (_DESC_COMMON + "repeat_run：真实回放工作流（用户桌面将被自动操作）。"
                "缺参数时会**拒绝启动并返回缺失清单**（此时不要重试，先向用户询问参数值再带 params 调用）；"
                "危险步骤默认挂起转人工。回放期间请勿移动鼠标键盘（检测到人工介入会自动暂停）。"
                "技能封装场景：技能 SKILL.md 会绑定 workflow_id+graph_sha256，"
                "本工具可传 expected_graph_sha256 校验版本。")

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {
            "workflow_id": {"type": "string"},
            "params": {"type": "object"},
            "expected_graph_sha256": {"type": "string",
                                      "description": "技能版本绑定校验（可选）"},
            "allow_blind_coord": {"type": "boolean",
                                  "description": "显式允许环境漂移后的坐标盲点（默认禁止）"},
        }, "required": ["workflow_id"]}

    async def execute(self, args: dict[str, Any]) -> str:
        from harness.modules.desktop_repeat.errors import RepeatError
        try:
            res = await self._call("start_run", args["workflow_id"], args.get("params") or {},
                                   unattended=False,
                                   allow_blind_coord=bool(args.get("allow_blind_coord", False)),
                                   expected_graph_sha256=args.get("expected_graph_sha256"))
        except RepeatError as e:
            return self._err(f"{e.code} {e.message}")
        if res.get("rejected"):
            return json.dumps({"rejected": True, "missing": res.get("error", {}),
                               "params": res.get("params", []),
                               "next": "向用户逐项询问参数值后，带 params 重新调用（不要臆造值）"},
                              ensure_ascii=False)
        # 等待收口（SSE 总线已挂起，运行本身后台进行；给调用方一个终态摘要）
        run_id = res.get("run_id", "")
        svc = _svc(self._services)
        if svc is None:
            return _UNAVAILABLE
        last: dict[str, Any] = {}
        try:
            async for ev in svc.subscribe(run_id):
                last = ev
                if ev.get("type") in ("run_done", "run_error"):
                    break
                if ev.get("type") == "run_suspended":
                    break
        except Exception:  # noqa: BLE001
            pass
        status = svc.get_run(run_id) if svc else {}
        summary = {"run_id": run_id, "status": status.get("status"),
                   "stats": status.get("stats"),
                   "diagnostics_path": status.get("diagnostics_path", ""),
                   "suspended_step": last.get("sid") if last.get("type") == "run_suspended" else None,
                   "events_seen": last.get("type", "")}
        if last.get("type") == "run_suspended":
            summary["next"] = (f"步骤 {last.get('sid')} 需人工处理：用 repeat_run_status 查看，"
                               f"处理完成后调用接口 resume（decision=retry/skip）或由用户在前端裁决")
        return json.dumps(summary, ensure_ascii=False)


class RunStatusTool(_Base):
    @property
    def tool_name(self) -> str:
        return "repeat_run_status"

    @property
    def risk_level(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return _DESC_COMMON + "repeat_run_status：查询某次回放的实时状态（当前步/统计/错误/诊断包路径）。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}

    async def execute(self, args: dict[str, Any]) -> str:
        svc = _svc(self._services)
        if svc is None:
            return _UNAVAILABLE
        return json.dumps(svc.get_run(args["run_id"]), ensure_ascii=False)


class AbortTool(_Base):
    @property
    def tool_name(self) -> str:
        return "repeat_abort"

    @property
    def risk_level(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return _DESC_COMMON + "repeat_abort：中止某次回放（等效于紧急停止；挂起中的运行同样适用）。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}

    async def execute(self, args: dict[str, Any]) -> str:
        try:
            return json.dumps(await self._call("abort_run", args["run_id"]), ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return self._err(str(e))


class ExportSkillTool(_Base):
    @property
    def tool_name(self) -> str:
        return "repeat_export_skill"

    @property
    def risk_level(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return (_DESC_COMMON + "repeat_export_skill：把工作流封装为技能目录（SKILL.md，版本绑定 graph 哈希）。"
                "封装后对话中直接说「跑一下 XXX 流程」即可零 token 回放。")

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {
            "workflow_id": {"type": "string"},
            "slug": {"type": "string", "description": "技能名 slug（可选，默认取流程名）"},
        }, "required": ["workflow_id"]}

    async def execute(self, args: dict[str, Any]) -> str:
        try:
            res = await self._call("export_skill", args["workflow_id"], slug=args.get("slug") or None)
        except Exception as e:  # noqa: BLE001
            return self._err(str(e))
        return json.dumps(res, ensure_ascii=False)


TOOL_CLASSES = [
    RecordStartTool, RecordStopTool, RecordStatusTool, CompileTool, ListTool,
    DryRunTool, RunTool, RunStatusTool, AbortTool, ExportSkillTool,
]
